"""
Simple SQLite-backed storage for the Claude task queue.

One process, low throughput (one task runs at a time), so a plain
threading.Lock around a single sqlite3 connection is plenty - no need
for a heavier DB. Keeping this dependency-free (sqlite3 is stdlib).
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).parent / "queue.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    prompt TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    -- queued | running | done | error | needs_review | rate_limited | cancelled
    status TEXT NOT NULL DEFAULT 'queued',
    result_summary TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    resume_at TEXT,
    -- NULL on a root task (the first message of a new chat). A reply sets
    -- this to the root task's own id, so every message in a chat -
    -- root or reply - carries the same thread_id once resolved (see
    -- get_thread_session_id) even though the column itself is only set
    -- on replies.
    thread_id INTEGER REFERENCES tasks(id),
    -- The Claude Code session this exact task ran (or resumed) in. The
    -- next reply in the same thread resumes whichever task in that
    -- thread most recently captured one - see get_thread_session_id.
    session_id TEXT,
    -- Octopus routing (see router.py / models.json). lane 'agent' = the
    -- original worker (Claude Code in the project folder); 'api' = a plain
    -- model call handled by worker.api_worker_loop. A reply always inherits
    -- its thread root's lane and model_id so a conversation never hops models.
    lane TEXT NOT NULL DEFAULT 'agent',
    model_id TEXT,
    category TEXT,
    route_reason TEXT,
    -- classifier | rules | manual | thread | direct
    routed_by TEXT,
    -- Usage, filled in when the task finishes. cost_usd is an estimate.
    duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    -- Where this task sits in the shared run queue: lowest runs first. Set
    -- on insert (after everything of equal or higher priority), rewritten
    -- by a manual reorder / move-to-top. Kept on cancel so Undo can put
    -- the task back exactly where it was.
    queue_pos REAL,
    -- The last thing the agent reported doing ("Running npm test"), for
    -- the dashboard's live progress line. Full history is in task_events.
    current_step TEXT
);

-- What an agent task did, step by step (one row per tool call, parsed
-- from Claude Code's stream-json output), plus approval/decision notes.
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    -- tool | approval | note
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);

-- Saved prompts. target: 'any' (whichever project you run it in), 'all'
-- (every active project), or a project id as text.
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'any',
    created_at TEXT NOT NULL
);

-- A prompt queued automatically at a time of day. project_id NULL means
-- every active project. cadence: once | daily | weekdays | weekly
-- (weekday 0 = Monday). next_run_at is UTC; the worker fires anything due,
-- so a schedule missed while the Mac slept runs once as soon as it wakes.
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    prompt TEXT NOT NULL,
    project_id INTEGER REFERENCES projects(id),
    cadence TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    weekday INTEGER,
    model TEXT NOT NULL DEFAULT 'auto',
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);

-- Per-project preferences from the dashboard's Settings tab. A project
-- without a row uses PROJECT_SETTINGS_DEFAULTS.
CREATE TABLE IF NOT EXISTS project_settings (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    default_priority INTEGER NOT NULL DEFAULT 0,
    notify_approvals INTEGER NOT NULL DEFAULT 1,
    notify_failures INTEGER NOT NULL DEFAULT 1,
    notify_completions INTEGER NOT NULL DEFAULT 1,
    quiet_hours INTEGER NOT NULL DEFAULT 0,
    -- JSON list of command prefixes the approval server allows without asking
    allow_commands TEXT NOT NULL DEFAULT '[]',
    -- Monthly cap on real (api lane) spend; hitting it pauses the project.
    budget_usd REAL NOT NULL DEFAULT 20
);

-- Small global key/value store (JSON values): dashboard toggles, the last
-- Claude usage snapshot, one-time migration markers.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per-model availability that outlives a single task: set when a provider
-- answers "rate limited" so the router can skip that model until then.
CREATE TABLE IF NOT EXISTS model_state (
    model_id TEXT PRIMARY KEY,
    rate_limited_until TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- A tool call (usually Bash) that needed permission while a task was
-- running, raised via approval_server.py (the --permission-prompt-tool
-- MCP server), and the human decision on it made from the dashboard.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    tool_name TEXT NOT NULL,
    tool_input TEXT NOT NULL,
    -- pending | approved | denied | timed_out
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    -- Optional reason typed when denying, passed back to Claude.
    note TEXT
);

-- A per-project backlog item. Unlike `tasks` (the actual run queue), a
-- todo just sits here until you check it and hit "Run selected" - only
-- then does it become a real task (see run_checked_todos). `position`
-- is the order you arrange them in and the order they're queued in.
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    text TEXT NOT NULL,
    position INTEGER NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0,
    -- pending | queued | done | error | needs_review | rate_limited | cancelled
    -- ('queued' onward mirrors the linked task's status - see list_todos)
    status TEXT NOT NULL DEFAULT 'pending',
    task_id INTEGER REFERENCES tasks(id),
    created_at TEXT NOT NULL
);
"""

# Every query - reads included - must go through this lock. FastAPI runs
# sync route handlers in a threadpool, so without it two threads can call
# into the same sqlite3 connection at once; the C extension releases the
# GIL mid-call, and concurrent access corrupts the SQL parser's internal
# state and segfaults the process (RLock so a locked function can safely
# call another, e.g. add_task -> get_task).
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS won't add new columns to a table that
    already exists from before this column was introduced - patch those
    in by hand, guarded so this is a no-op on a fresh or already-migrated
    database."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "thread_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN thread_id INTEGER REFERENCES tasks(id)")
    if "session_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
    for name, decl in (
        ("lane", "TEXT NOT NULL DEFAULT 'agent'"),
        ("model_id", "TEXT"),
        ("category", "TEXT"),
        ("route_reason", "TEXT"),
        ("routed_by", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("cost_usd", "REAL"),
        ("queue_pos", "REAL"),
        ("current_step", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
    approval_cols = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    if "note" not in approval_cols:
        conn.execute("ALTER TABLE approvals ADD COLUMN note TEXT")
    # Everything queued before Octopus ran on Claude Code - say so, so usage
    # stats don't lump it under "unknown".
    conn.execute("UPDATE tasks SET model_id = 'claude-code' WHERE model_id IS NULL AND lane = 'agent'")
    # Queued tasks from before queue_pos existed keep the order the worker
    # would have run them in.
    unplaced = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('queued', 'rate_limited') AND queue_pos IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    if unplaced:
        start = conn.execute("SELECT COALESCE(MAX(queue_pos), -1) + 1 AS n FROM tasks").fetchone()["n"]
        for i, r in enumerate(unplaced):
            conn.execute("UPDATE tasks SET queue_pos = ? WHERE id = ?", (start + i, r["id"]))
    # The worker used to file every failure as needs_review; failures are
    # 'error' now (needs_review is left for work that finished but wants a
    # human look). Relabel the old failures once.
    if not _get_setting(conn, "migrated_failures_to_error"):
        conn.execute("UPDATE tasks SET status = 'error' WHERE status = 'needs_review' AND error_message IS NOT NULL")
        _set_setting(conn, "migrated_failures_to_error", True)
    if not _get_setting(conn, "recipes_seeded"):
        if not conn.execute("SELECT 1 FROM recipes LIMIT 1").fetchone():
            for name, prompt, target in DEFAULT_RECIPES:
                conn.execute("INSERT INTO recipes (name, prompt, target, created_at) VALUES (?, ?, ?, ?)",
                             (name, prompt, target, _now()))
        _set_setting(conn, "recipes_seeded", True)
    conn.commit()


# Starter recipes, seeded once into an empty recipes table (delete or edit
# them freely afterwards - they're never re-added).
DEFAULT_RECIPES = [
    ("Run tests", "Run the full test suite. Fix anything trivially broken and report the rest.", "any"),
    ("Morning status check", "Summarize git status, open TODOs and failing tests in five bullets.", "all"),
    ("Bump dependencies", "Update minor and patch deps, run tests, revert anything that breaks.", "any"),
    ("Changelog since last tag", "Write a CHANGELOG entry for every commit since the last tag.", "any"),
]


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.commit()
            _migrate(_conn)
        return _conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


# ---------------------------------------------------------------- projects

# The built-in project for requests that don't belong to a repo (see
# api._general_project). Identified by name - it's created on demand.
GENERAL_PROJECT_NAME = "General"


def is_general_project(project: Optional[dict[str, Any]]) -> bool:
    return bool(project) and project["name"] == GENERAL_PROJECT_NAME

def add_project(name: str, path: str) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO projects (name, path, active, created_at) VALUES (?, ?, 1, ?)",
            (name, path, _now()),
        )
        conn.commit()
        return get_project(cur.lastrowid)


def list_projects(active_only: bool = False) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        q = "SELECT * FROM projects"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY name"
        return [_row_to_dict(r) for r in conn.execute(q).fetchall()]


def get_project(project_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_project_by_name(name: str) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return _row_to_dict(row) if row else None


def set_project_active(project_id: int, active: bool) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET active = ? WHERE id = ?", (1 if active else 0, project_id))
        conn.commit()


# ------------------------------------------------------------------- tasks

def _queue_insert_pos(conn: sqlite3.Connection, priority: int) -> float:
    """queue_pos for a new task: after every queued task of the same or
    higher priority, ahead of anything lower - so priority still decides
    by default, and a manual reorder is only ever a starting point."""
    rows = conn.execute(
        "SELECT queue_pos, priority FROM tasks WHERE status = 'queued' AND queue_pos IS NOT NULL "
        "ORDER BY queue_pos ASC"
    ).fetchall()
    prev = None
    for r in rows:
        if r["priority"] < priority:
            return (prev + r["queue_pos"]) / 2 if prev is not None else r["queue_pos"] - 1
        prev = r["queue_pos"]
    return prev + 1 if prev is not None else 0.0


def _queue_front_pos(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT MIN(queue_pos) AS m FROM tasks WHERE status = 'queued'").fetchone()
    return row["m"] - 1 if row["m"] is not None else 0.0


def add_task(
    project_id: int,
    prompt: str,
    priority: int = 0,
    thread_id: Optional[int] = None,
    lane: str = "agent",
    model_id: Optional[str] = None,
    category: Optional[str] = None,
    route_reason: Optional[str] = None,
    routed_by: Optional[str] = None,
) -> dict[str, Any]:
    """Defaults (agent lane, claude-code) are what every pre-Octopus caller -
    the plain task endpoint, todos - has always meant."""
    if model_id is None and lane == "agent":
        model_id = "claude-code"
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO tasks (project_id, prompt, priority, status, created_at, thread_id, "
            "lane, model_id, category, route_reason, routed_by, queue_pos) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, prompt, priority, _now(), thread_id,
             lane, model_id, category, route_reason, routed_by, _queue_insert_pos(conn, priority)),
        )
        conn.commit()
        return get_task(cur.lastrowid)


def queue_position(task_id: int) -> Optional[int]:
    """1-based place in line among queued tasks, or None if it isn't queued."""
    with _lock:
        conn = get_conn()
        task = conn.execute("SELECT status, queue_pos FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task or task["status"] != "queued":
            return None
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued' AND "
            "(queue_pos < ? OR (queue_pos = ? AND id < ?))",
            (task["queue_pos"], task["queue_pos"], task_id),
        ).fetchone()
        return row["n"] + 1


def reorder_queue(ordered_ids: list[int]) -> None:
    """Set the run order of queued tasks. ids not in the list (e.g. queued
    after the dashboard last refreshed) keep their relative order after them."""
    with _lock:
        conn = get_conn()
        queued = [r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE status = 'queued' ORDER BY queue_pos ASC, id ASC").fetchall()]
        wanted = [i for i in ordered_ids if i in set(queued)]
        order = wanted + [i for i in queued if i not in set(wanted)]
        for pos, task_id in enumerate(order):
            conn.execute("UPDATE tasks SET queue_pos = ? WHERE id = ?", (float(pos), task_id))
        conn.commit()


def move_to_front(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET queue_pos = ? WHERE id = ? AND status = 'queued'",
                     (_queue_front_pos(conn), task_id))
        conn.commit()


REQUEUEABLE = ("error", "needs_review", "cancelled", "rate_limited")


def requeue_task(task_id: int, front: bool = True) -> Optional[dict[str, Any]]:
    """Put a finished/stuck task back in the queue as the same task (same
    thread, so an agent task resumes its session). front=True is Retry /
    "Retry now"; front=False is Undo on a cancel and restores the old spot."""
    with _lock:
        conn = get_conn()
        task = get_task(task_id)
        if not task or task["status"] not in REQUEUEABLE:
            return task
        pos = task["queue_pos"]
        if front or pos is None:
            pos = _queue_front_pos(conn)
        conn.execute(
            "UPDATE tasks SET status = 'queued', error_message = NULL, resume_at = NULL, started_at = NULL, "
            "finished_at = NULL, current_step = NULL, queue_pos = ? WHERE id = ?",
            (pos, task_id),
        )
        conn.commit()
        return get_task(task_id)


def mark_reviewed(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ? AND status = 'needs_review'", (task_id,))
        conn.commit()


def mark_stopped(task_id: int) -> None:
    """A running task you stopped from the dashboard."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ?, error_message = 'Stopped from the dashboard' "
            "WHERE id = ?",
            (_now(), task_id),
        )
        conn.commit()


# ------------------------------------------------------------- task events

def add_task_event(task_id: int, kind: str, text: str) -> None:
    text = text.strip()[:500]
    if not text:
        return
    with _lock:
        conn = get_conn()
        conn.execute("INSERT INTO task_events (task_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                     (task_id, kind, text, _now()))
        conn.execute("UPDATE tasks SET current_step = ? WHERE id = ?", (text[:200], task_id))
        conn.commit()


def list_task_events(task_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not task_ids:
        return {}
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            f"SELECT * FROM task_events WHERE task_id IN ({','.join('?' * len(task_ids))}) ORDER BY id ASC",
            task_ids,
        ).fetchall()
        out: dict[int, list[dict[str, Any]]] = {i: [] for i in task_ids}
        for r in rows:
            out[r["task_id"]].append(_row_to_dict(r))
        return out


def get_thread(task_id: int) -> list[dict[str, Any]]:
    """Every message in the chat this task belongs to, oldest first."""
    with _lock:
        conn = get_conn()
        task = get_task(task_id)
        if not task:
            return []
        root = task["thread_id"] or task["id"]
        rows = conn.execute(
            "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.id = ? OR t.thread_id = ? ORDER BY t.id ASC",
            (root, root),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_thread_history(root_task_id: int, before_task_id: int) -> list[dict[str, Any]]:
    """Earlier messages of a thread (root included), oldest first - what an
    api-lane model is shown as the conversation so far."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE (id = ? OR thread_id = ?) AND id < ? ORDER BY id ASC",
            (root_task_id, root_task_id, before_task_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_task_usage(task_id: int, duration_ms: Optional[int] = None, input_tokens: Optional[int] = None,
                   output_tokens: Optional[int] = None, cost_usd: Optional[float] = None) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET duration_ms = ?, input_tokens = ?, output_tokens = ?, cost_usd = ? WHERE id = ?",
            (duration_ms, input_tokens, output_tokens, cost_usd, task_id),
        )
        conn.commit()


def get_thread_session_id(root_task_id: int) -> Optional[str]:
    """The session to --resume for the next message in this thread: whichever
    task in it (the root, or any reply) most recently captured a session_id.
    None means the thread hasn't produced one yet - the next task starts a
    fresh conversation."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT session_id FROM tasks "
            "WHERE (id = ? OR thread_id = ?) AND session_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (root_task_id, root_task_id),
        ).fetchone()
        return row["session_id"] if row else None


def set_task_session(task_id: int, session_id: Optional[str]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id))
        conn.commit()


def get_task(task_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_tasks(project_id: Optional[int] = None, statuses: Optional[list[str]] = None) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        q = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if project_id is not None:
            q += " AND project_id = ?"
            params.append(project_id)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            q += f" AND status IN ({placeholders})"
            params.extend(statuses)
        q += " ORDER BY priority DESC, created_at ASC"
        return [_row_to_dict(r) for r in conn.execute(q, params).fetchall()]


def list_all_tasks(
    limit: int = 200,
    statuses: Optional[list[str]] = None,
    project_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Most recent tasks, newest first, for the dashboard - across all projects
    unless project_id narrows it to one."""
    with _lock:
        conn = get_conn()
        q = """
            SELECT t.*, p.name AS project_name
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE 1=1
        """
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            q += f" AND t.status IN ({placeholders})"
            params.extend(statuses)
        if project_id is not None:
            q += " AND t.project_id = ?"
            params.append(project_id)
        q += " ORDER BY t.id DESC LIMIT ?"
        params.append(limit)
        return [_row_to_dict(r) for r in conn.execute(q, params).fetchall()]


def get_next_queued_task(lane: str = "agent", exclude_models: tuple = (),
                         hold_low_priority: bool = False) -> Optional[dict[str, Any]]:
    """Next task to run in this lane, only from active projects: a
    rate_limited task whose resume_at has passed first (it was already
    mid-run), then queued tasks in queue_pos order. exclude_models skips
    tasks for models currently known to be rate-limited, so the api lane
    doesn't keep hitting a limited model. hold_low_priority leaves Low
    (priority < 0) tasks waiting - "save the session for urgent work"."""
    with _lock:
        conn = get_conn()
        now = _now()
        q = """
            SELECT t.* FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE p.active = 1
              AND t.lane = ?
              AND (
                    t.status = 'queued'
                    OR (t.status = 'rate_limited' AND t.resume_at IS NOT NULL AND t.resume_at <= ?)
                  )
        """
        params: list[Any] = [lane, now]
        if exclude_models:
            q += f" AND COALESCE(t.model_id, '') NOT IN ({','.join('?' * len(exclude_models))})"
            params.extend(exclude_models)
        if hold_low_priority:
            q += " AND NOT (t.status = 'queued' AND t.priority < 0)"
        q += " ORDER BY (t.status = 'rate_limited') DESC, t.queue_pos ASC, t.id ASC LIMIT 1"
        row = conn.execute(q, params).fetchone()
        return _row_to_dict(row) if row else None


def mark_running(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, resume_at = NULL WHERE id = ?",
            (_now(), task_id),
        )
        conn.commit()


def mark_done(task_id: int, result_summary: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'done', result_summary = ?, finished_at = ? WHERE id = ?",
            (result_summary, _now(), task_id),
        )
        conn.commit()


def mark_error(task_id: int, error_message: str, needs_review: bool = True) -> None:
    with _lock:
        conn = get_conn()
        status = "needs_review" if needs_review else "error"
        conn.execute(
            "UPDATE tasks SET status = ?, error_message = ?, finished_at = ? WHERE id = ?",
            (status, error_message, _now(), task_id),
        )
        conn.commit()


def mark_rate_limited(task_id: int, resume_at_iso: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'rate_limited', resume_at = ? WHERE id = ?",
            (resume_at_iso, task_id),
        )
        conn.commit()


def cancel_task(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ? AND status IN ('queued', 'rate_limited')",
            (_now(), task_id),
        )
        conn.commit()


# -------------------------------------------------------------- approvals

def create_approval(task_id: int, tool_name: str, tool_input: str) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO approvals (task_id, tool_name, tool_input, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (task_id, tool_name, tool_input, _now()),
        )
        conn.commit()
        return get_approval(cur.lastrowid)


def get_approval(approval_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_pending_approvals() -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT a.*, t.prompt AS task_prompt, t.project_id, t.model_id, t.lane FROM approvals a "
            "JOIN tasks t ON t.id = a.task_id "
            "WHERE a.status = 'pending' ORDER BY a.created_at ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def decide_approval(approval_id: int, approved: bool, note: Optional[str] = None) -> Optional[dict[str, Any]]:
    """No-op (returns the row unchanged) if this approval was already decided -
    e.g. it timed out server-side right as the dashboard's click came in."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, note = ? WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "denied", _now(), (note or "").strip()[:500] or None, approval_id),
        )
        conn.commit()
        return get_approval(approval_id)


def expire_pending_approvals(task_id: int) -> None:
    """A task that was stopped or killed can't use an answer any more."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE approvals SET status = 'timed_out', decided_at = ? WHERE task_id = ? AND status = 'pending'",
            (_now(), task_id),
        )
        conn.commit()


def timeout_approval(approval_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE approvals SET status = 'timed_out', decided_at = ? WHERE id = ? AND status = 'pending'",
            (_now(), approval_id),
        )
        conn.commit()


# -------------------------------------------------------------------- todos

_TODO_SELECT = """
    SELECT t.*, tk.status AS task_status, tk.result_summary AS task_result_summary,
           tk.error_message AS task_error_message
    FROM todos t LEFT JOIN tasks tk ON tk.id = t.task_id
"""


def add_todo(project_id: int, text: str) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        next_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM todos WHERE project_id = ?",
            (project_id,),
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO todos (project_id, text, position, checked, status, created_at) "
            "VALUES (?, ?, ?, 0, 'pending', ?)",
            (project_id, text, next_pos, _now()),
        )
        conn.commit()
        return get_todo(cur.lastrowid)


def get_todo(todo_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute(_TODO_SELECT + " WHERE t.id = ?", (todo_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_todos(project_id: int) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            _TODO_SELECT + " WHERE t.project_id = ? ORDER BY t.position ASC",
            (project_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_todo(todo_id: int, text: Optional[str] = None, checked: Optional[bool] = None) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        todo = get_todo(todo_id)
        if not todo:
            return None

        if text is not None:
            conn.execute("UPDATE todos SET text = ? WHERE id = ?", (text, todo_id))

        if checked is not None and bool(checked) != bool(todo["checked"]):
            conn.execute("UPDATE todos SET checked = ? WHERE id = ?", (1 if checked else 0, todo_id))
            # Re-checking a todo that already ran (either way) stages a
            # fresh run next time "Run selected" is pressed, instead of
            # silently doing nothing because its status is no longer pending.
            if checked and todo["status"] != "pending":
                conn.execute(
                    "UPDATE todos SET status = 'pending', task_id = NULL WHERE id = ?",
                    (todo_id,),
                )

        conn.commit()
        return get_todo(todo_id)


def delete_todo(todo_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        conn.commit()


def reorder_todos(project_id: int, ordered_ids: list[int]) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        for position, todo_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE todos SET position = ? WHERE id = ? AND project_id = ?",
                (position, todo_id, project_id),
            )
        conn.commit()
        return list_todos(project_id)


def run_checked_todos(project_id: int) -> list[dict[str, Any]]:
    """Convert every checked, not-yet-run todo (in position order) into a
    real queued task - see add_task. Already queued/done/error ones are
    left alone; re-check one (see update_todo) to stage it for a rerun."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM todos WHERE project_id = ? AND checked = 1 AND status = 'pending' "
            "ORDER BY position ASC",
            (project_id,),
        ).fetchall()
        for row in rows:
            task = add_task(row["project_id"], row["text"])
            conn.execute(
                "UPDATE todos SET status = 'queued', task_id = ? WHERE id = ?",
                (task["id"], row["id"]),
            )
        conn.commit()
        return list_todos(project_id)


def queue_status() -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        counts = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) as n FROM tasks GROUP BY status").fetchall()
        }
        # "running" stays the agent lane's own task for older clients;
        # running_all adds whatever the api lane is doing alongside it.
        running = conn.execute(
            "SELECT * FROM tasks WHERE status = 'running' AND lane = 'agent' LIMIT 1"
        ).fetchone()
        running_all = conn.execute(
            "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC"
        ).fetchall()
        next_up = get_next_queued_task("agent") or get_next_queued_task("api")
        rate_limited = conn.execute(
            "SELECT * FROM tasks WHERE status = 'rate_limited' ORDER BY resume_at ASC LIMIT 1"
        ).fetchone()
        return {
            "counts": counts,
            "running": _row_to_dict(running) if running else None,
            "running_all": [_row_to_dict(r) for r in running_all],
            "next_up": next_up,
            "earliest_rate_limit_resume": _row_to_dict(rate_limited) if rate_limited else None,
        }


# -------------------------------------------------------------- model state

def set_model_rate_limited(model_id: str, until_iso: str, error: str = "") -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO model_state (model_id, rate_limited_until, last_error, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET rate_limited_until = excluded.rate_limited_until, "
            "last_error = excluded.last_error, updated_at = excluded.updated_at",
            (model_id, until_iso, error[:500], _now()),
        )
        conn.commit()


def clear_model_rate_limit(model_id: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE model_state SET rate_limited_until = NULL, updated_at = ? WHERE model_id = ?",
            (_now(), model_id),
        )
        conn.commit()


def model_rate_limited_until(model_id: str) -> Optional[str]:
    """ISO time this model is known to be rate-limited until, or None if it
    isn't (or the limit has already passed). For claude-code this is derived
    from the agent lane's own rate_limited tasks, which is where that limit shows up."""
    with _lock:
        conn = get_conn()
        now = _now()
        if model_id == "claude-code":
            row = conn.execute(
                "SELECT MAX(resume_at) AS until FROM tasks "
                "WHERE status = 'rate_limited' AND lane = 'agent' AND resume_at > ?",
                (now,),
            ).fetchone()
            return row["until"] if row and row["until"] else None
        row = conn.execute(
            "SELECT rate_limited_until FROM model_state WHERE model_id = ? AND rate_limited_until > ?",
            (model_id, now),
        ).fetchone()
        return row["rate_limited_until"] if row else None


def usage_summary(days: int = 30) -> dict[str, Any]:
    """Per-model and per-category totals over the last `days` - the data
    future routing decisions can learn from (success rate, cost, speed)."""
    with _lock:
        conn = get_conn()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def grouped(col: str) -> list[dict[str, Any]]:
            rows = conn.execute(
                f"""
                SELECT COALESCE({col}, 'unknown') AS key,
                       COUNT(*) AS tasks,
                       SUM(status = 'done') AS done,
                       SUM(status IN ('error', 'needs_review')) AS failed,
                       SUM(status = 'rate_limited') AS rate_limited,
                       AVG(duration_ms) AS avg_duration_ms,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cost_usd) AS cost_usd
                FROM tasks WHERE created_at >= ?
                GROUP BY key ORDER BY tasks DESC
                """,
                (since,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return {"days": days, "by_model": grouped("model_id"), "by_category": grouped("category")}


# ---------------------------------------------------------------- settings

# Global toggles the dashboard can change. monthly_budget_usd is the
# "of $100 this month" line - real (api lane) spend across all projects.
SETTINGS_DEFAULTS: dict[str, Any] = {
    "notify_on_resume": False,       # ping once when a rate-limit pause ends
    "hold_low_when_busy": True,      # hold Low tasks while the 5-hour window is over 85%
    "monthly_budget_usd": 100.0,
}


def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        return _get_setting(get_conn(), key, SETTINGS_DEFAULTS.get(key, default))


def set_setting(key: str, value: Any) -> None:
    with _lock:
        conn = get_conn()
        _set_setting(conn, key, value)
        conn.commit()


def get_settings() -> dict[str, Any]:
    return {k: get_setting(k) for k in SETTINGS_DEFAULTS}


# ------------------------------------------------------- project settings

PROJECT_SETTINGS_DEFAULTS: dict[str, Any] = {
    "default_priority": 0,
    "notify_approvals": True,
    "notify_failures": True,
    "notify_completions": True,
    "quiet_hours": False,
    "allow_commands": [],
    "budget_usd": 20.0,
}
_BOOL_SETTINGS = ("notify_approvals", "notify_failures", "notify_completions", "quiet_hours")


def get_project_settings(project_id: int) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM project_settings WHERE project_id = ?", (project_id,)).fetchone()
        out = dict(PROJECT_SETTINGS_DEFAULTS, allow_commands=[])
        if row:
            for k in PROJECT_SETTINGS_DEFAULTS:
                out[k] = row[k]
            out["allow_commands"] = json.loads(row["allow_commands"] or "[]")
            for k in _BOOL_SETTINGS:
                out[k] = bool(out[k])
        out["project_id"] = project_id
        return out


def update_project_settings(project_id: int, **fields: Any) -> dict[str, Any]:
    fields = {k: v for k, v in fields.items() if k in PROJECT_SETTINGS_DEFAULTS and v is not None}
    with _lock:
        conn = get_conn()
        conn.execute("INSERT OR IGNORE INTO project_settings (project_id) VALUES (?)", (project_id,))
        for k, v in fields.items():
            if k == "allow_commands":
                v = json.dumps([str(c).strip() for c in v if str(c).strip()])
            elif k in _BOOL_SETTINGS:
                v = 1 if v else 0
            conn.execute(f"UPDATE project_settings SET {k} = ? WHERE project_id = ?", (v, project_id))
        conn.commit()
        return get_project_settings(project_id)


def add_allowed_command(project_id: int, command: str) -> dict[str, Any]:
    current = get_project_settings(project_id)["allow_commands"]
    if command not in current:
        current = current + [command]
    return update_project_settings(project_id, allow_commands=current)


# ----------------------------------------------------------------- recipes

def list_recipes() -> list[dict[str, Any]]:
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute("SELECT * FROM recipes ORDER BY id ASC").fetchall()]


def get_recipe(recipe_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        return _row_to_dict(row) if row else None


def add_recipe(name: str, prompt: str, target: str = "any") -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO recipes (name, prompt, target, created_at) VALUES (?, ?, ?, ?)",
                           (name, prompt, target, _now()))
        conn.commit()
        return get_recipe(cur.lastrowid)


def delete_recipe(recipe_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        conn.commit()


# --------------------------------------------------------------- schedules

CADENCES = ("once", "daily", "weekdays", "weekly")


def next_run_after(cadence: str, time_of_day: str, weekday: Optional[int] = None,
                   after: Optional[datetime] = None, on_date: Optional[str] = None) -> datetime:
    """Next local-time occurrence strictly after `after` (default now), as an
    aware datetime. on_date ('YYYY-MM-DD') pins a one-off to that day, as long
    as that moment is still in the future."""
    now = (after or datetime.now()).astimezone()
    hh, mm = (int(x) for x in time_of_day.split(":"))
    if cadence == "once" and on_date:
        y, mo, d = (int(x) for x in on_date.split("-"))
        pinned = datetime(y, mo, d, hh, mm).astimezone()
        if pinned > now:
            return pinned
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    for _ in range(8):
        if cadence == "weekdays" and cand.weekday() >= 5:
            cand += timedelta(days=1)
            continue
        if cadence == "weekly" and weekday is not None and cand.weekday() != weekday:
            cand += timedelta(days=1)
            continue
        break
    # Rebuild from the naive wall-clock time so a DST change in between
    # doesn't shift it by an hour.
    return datetime(cand.year, cand.month, cand.day, hh, mm).astimezone()


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


_SCHEDULE_SELECT = "SELECT s.*, p.name AS project_name FROM schedules s LEFT JOIN projects p ON p.id = s.project_id"


def list_schedules() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            _SCHEDULE_SELECT + " ORDER BY s.enabled DESC, s.next_run_at IS NULL, s.next_run_at ASC, s.id ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_schedule(schedule_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute(_SCHEDULE_SELECT + " WHERE s.id = ?", (schedule_id,)).fetchone()
        return _row_to_dict(row) if row else None


def add_schedule(label: str, prompt: str, project_id: Optional[int], cadence: str, time_of_day: str,
                 weekday: Optional[int] = None, model: str = "auto", priority: int = 0,
                 on_date: Optional[str] = None) -> dict[str, Any]:
    nxt = next_run_after(cadence, time_of_day, weekday, on_date=on_date)
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO schedules (label, prompt, project_id, cadence, time_of_day, weekday, model, priority, "
            "enabled, next_run_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (label, prompt, project_id, cadence, time_of_day, weekday, model, priority, _utc_iso(nxt), _now()),
        )
        conn.commit()
        return get_schedule(cur.lastrowid)


def set_schedule_enabled(schedule_id: int, enabled: bool) -> Optional[dict[str, Any]]:
    sc = get_schedule(schedule_id)
    if not sc:
        return None
    nxt = _utc_iso(next_run_after(sc["cadence"], sc["time_of_day"], sc["weekday"])) if enabled else None
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE schedules SET enabled = ?, next_run_at = ? WHERE id = ?",
                     (1 if enabled else 0, nxt, schedule_id))
        conn.commit()
    return get_schedule(schedule_id)


def delete_schedule(schedule_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        conn.commit()


def due_schedules() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            _SCHEDULE_SELECT + " WHERE s.enabled = 1 AND s.next_run_at IS NOT NULL AND s.next_run_at <= ?",
            (_now(),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def mark_schedule_fired(schedule_id: int) -> None:
    """Advance past now (not past the missed time), so a schedule that was
    due several times while the Mac slept only fires once on wake."""
    sc = get_schedule(schedule_id)
    if not sc:
        return
    with _lock:
        conn = get_conn()
        if sc["cadence"] == "once":
            conn.execute("UPDATE schedules SET enabled = 0, next_run_at = NULL, last_run_at = ? WHERE id = ?",
                         (_now(), schedule_id))
        else:
            nxt = next_run_after(sc["cadence"], sc["time_of_day"], sc["weekday"])
            conn.execute("UPDATE schedules SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                         (_utc_iso(nxt), _now(), schedule_id))
        conn.commit()


# ------------------------------------------------------------------- spend
# "Spend" is real money: api-lane tasks (OpenAI, Gemini...). Claude Code's
# own cost_usd is only what a run *would* cost at API prices - it's shown on
# the task itself as an estimate, but a subscription doesn't bill it, so it
# stays out of spend totals and budgets.

def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def spend_summary(days: int = 7) -> dict[str, Any]:
    now = datetime.now().astimezone()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = today0.replace(day=1)
    week0 = today0 - timedelta(days=days - 1)
    since = min(month0, week0)
    with _lock:
        rows = get_conn().execute(
            "SELECT t.project_id, p.name AS project_name, t.cost_usd, COALESCE(t.finished_at, t.created_at) AS at "
            "FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.lane = 'api' AND t.cost_usd IS NOT NULL AND COALESCE(t.finished_at, t.created_at) >= ?",
            (_utc_iso(since),),
        ).fetchall()
    by_day = {(week0 + timedelta(days=i)).date().isoformat(): 0.0 for i in range(days)}
    by_project: dict[int, dict[str, Any]] = {}
    today = month = 0.0
    for r in rows:
        at = _parse_iso(r["at"]).astimezone()
        cost = r["cost_usd"] or 0.0
        key = at.date().isoformat()
        if key in by_day:
            by_day[key] += cost
        if at >= today0:
            today += cost
        if at >= month0:
            month += cost
            bp = by_project.setdefault(r["project_id"], {"project_id": r["project_id"],
                                                          "name": r["project_name"], "cost_usd": 0.0})
            bp["cost_usd"] += cost
    return {
        "today_usd": today,
        "month_usd": month,
        "monthly_budget_usd": get_setting("monthly_budget_usd"),
        "days": [{"date": d, "cost_usd": c} for d, c in by_day.items()],
        "by_project": sorted(by_project.values(), key=lambda x: -x["cost_usd"]),
    }


def project_month_spend(project_id: int) -> float:
    month0 = datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with _lock:
        row = get_conn().execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM tasks WHERE project_id = ? AND lane = 'api' "
            "AND COALESCE(finished_at, created_at) >= ?",
            (project_id, _utc_iso(month0)),
        ).fetchone()
        return float(row["s"])
