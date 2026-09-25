"""
Simple SQLite-backed storage for the Claude task queue.

One process, low throughput (one task runs at a time), so a plain
threading.Lock around a single sqlite3 connection is plenty - no need
for a heavier DB. Keeping this dependency-free (sqlite3 is stdlib).
"""

import json
import re
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
    created_at TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'work',
    -- not_started | in_progress | need_info | on_hold | completed (PROJECT_STATUSES)
    status TEXT NOT NULL DEFAULT 'not_started'
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
    current_step TEXT,
    -- The chat's short name, shown everywhere instead of the full prompt.
    -- Every message in a thread carries its root's title. title_by:
    -- text (quick cut of the prompt, a model may still rename it) | ai |
    -- recipe | user | kept (the model couldn't do better - leave it).
    title TEXT,
    title_by TEXT,
    -- Cached "Summarise" output on the root task; summary_key records the
    -- thread state it was made from, so it's only regenerated after the
    -- thread changes.
    summary TEXT,
    summary_key TEXT
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
    budget_usd REAL NOT NULL DEFAULT 20,
    -- Automation: move the project's status to in_progress when a task under it starts.
    auto_in_progress INTEGER NOT NULL DEFAULT 1
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

-- What you've told Octopus about a project: guidelines, requirements, plain
-- notes. Added one at a time from the project's Notes tab, and handed to every
-- task run for that project (see project_notes_prompt).
CREATE TABLE IF NOT EXISTS project_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    -- guideline | requirement | note
    kind TEXT NOT NULL DEFAULT 'note',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Personal assistant (see assistant/). One row per connected account. The
-- OAuth tokens themselves are NOT here - they live in the Keychain
-- (assistant/token_store.py); this is only what the dashboard may show.
-- status: connected | reauth_required | disconnected
CREATE TABLE IF NOT EXISTS connections (
    provider TEXT PRIMARY KEY,
    account TEXT,
    status TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    connected_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- Every external action the assistant took or asked to take. No message
-- bodies and no tokens: `target` is a short label (recipient, event title,
-- search query) and `ref` an opaque provider id (a draft id, an event id).
-- approval: not_required | requested | approved | denied | expired | trusted
-- result:   ok | error | pending | denied
CREATE TABLE IF NOT EXISTS assistant_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    approval TEXT NOT NULL DEFAULT 'not_required',
    result TEXT NOT NULL DEFAULT 'ok',
    error TEXT,
    task_id INTEGER,
    approval_id INTEGER,
    ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_assistant_activity_created ON assistant_activity(created_at);

-- What a conversation has been talking about (the events and emails a tool
-- just returned, with their ids), so "move it to 4" can be resolved. One row
-- per chat, keyed by the thread's root task.
CREATE TABLE IF NOT EXISTS assistant_context (
    thread_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Local reminders: fired by the scheduler loop as a push to the phone.
-- status: pending | fired | cancelled
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    fired_at TEXT,
    task_id INTEGER
);

-- Octopus's routing log: one row per request it understood - what was asked,
-- the plan it made (classification, project, agent, model, tools, permissions,
-- how each candidate model scored) and what became of it. The Routing page and
-- the plan preview read this; `plan` is the JSON built by octopus/plan.py.
-- status: proposed (waiting for Run / Cancel) | needs_input (e.g. which
-- project?) | queued (a task was made) | done (answered on the spot) |
-- cancelled | expired
CREATE TABLE IF NOT EXISTS routing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- dashboard | voice | api | shortcut | schedule | recipe
    source TEXT NOT NULL DEFAULT 'api',
    request TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    category TEXT,
    agent_id TEXT,
    model_id TEXT,
    project_id INTEGER,
    risk TEXT,
    plan TEXT NOT NULL,
    task_id INTEGER,
    -- auto (Octopus chose) | user (chose or overrode in the plan card)
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_routing_created ON routing(created_at);
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


# SQL condition on a task aliased `t`: its chat has nothing running and no
# earlier message still waiting - a chat's messages run one at a time, in the
# order they were sent, each resuming the session the one before it left.
CHAT_FREE = """NOT EXISTS (
    SELECT 1 FROM tasks c
    WHERE COALESCE(c.thread_id, c.id) = COALESCE(t.thread_id, t.id) AND c.id != t.id
      AND (c.status = 'running' OR (c.id < t.id AND c.status IN ('queued', 'rate_limited'))))"""

TITLE_MAX_CHARS = 60


def quick_title(prompt: str) -> str:
    """A readable name straight from the prompt, no model needed: its first
    sentence of real text, markdown stripped, cut at a word boundary."""
    text = ""
    for line in (prompt or "").splitlines():
        line = re.sub(r"^\s*((#+|[-*>]|\d+[.)])\s+)+", "", line)
        line = re.sub(r"[*_`#]+", "", line).strip()
        if line and not re.fullmatch(r"[-=~\s]+", line):
            text = line
            break
    # End of the first sentence - a word then . ! or ?, so "1. Work" or
    # "e.g. this" don't count.
    text = re.split(r"(?<=[A-Za-z]{2}[.!?])\s", text, maxsplit=1)[0].rstrip(".!? ")
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "Untitled task"
    if len(text) > TITLE_MAX_CHARS:
        text = text[:TITLE_MAX_CHARS].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return text[0].upper() + text[1:]


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS won't add new columns to a table that
    already exists from before this column was introduced - patch those
    in by hand, guarded so this is a no-op on a fresh or already-migrated
    database."""
    # Projects predate the Work / Personal split. Everything already
    # registered starts as 'personal' so nothing leaks into a Work view
    # until it has been sorted on purpose.
    project_cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "category" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN category TEXT NOT NULL DEFAULT 'personal'")
    if "status" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'not_started'")
    setting_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_settings)").fetchall()}
    if "auto_in_progress" not in setting_cols:
        conn.execute("ALTER TABLE project_settings ADD COLUMN auto_in_progress INTEGER NOT NULL DEFAULT 1")
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
        ("title", "TEXT"),
        ("title_by", "TEXT"),
        ("summary", "TEXT"),
        ("summary_key", "TEXT"),
        ("run_now", "INTEGER NOT NULL DEFAULT 0"),
        # 1 = held by the user: a queued task is skipped by the worker until
        # played, a running one is frozen mid-run. Status doesn't change.
        ("paused", "INTEGER NOT NULL DEFAULT 0"),
        ("permission_mode", "TEXT"),
        # Octopus (see octopus/): which agent handled it, the tools it was given
        # (JSON list of tool ids), the routing-log row that made it, where the
        # request came from, its risk read, the live plan the agent reported
        # (JSON list of {content, status}), the git state when it started
        # (JSON), and files it produced (JSON list, e.g. generated images).
        ("agent_id", "TEXT"),
        ("tools", "TEXT"),
        ("routing_id", "INTEGER"),
        ("source", "TEXT"),
        ("risk", "TEXT"),
        ("plan_steps", "TEXT"),
        ("git_base", "TEXT"),
        ("artifacts", "TEXT"),
        # Pictures the user attached to this message: JSON list of upload ids (see uploads.py).
        ("images", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
    # Tasks from before titles existed: roots get a quick name from their
    # prompt (the namer loop upgrades these), replies take their root's.
    for r in conn.execute("SELECT id, prompt FROM tasks WHERE title IS NULL AND thread_id IS NULL").fetchall():
        conn.execute("UPDATE tasks SET title = ?, title_by = 'text' WHERE id = ?", (quick_title(r["prompt"]), r["id"]))
    conn.execute(
        "UPDATE tasks SET title = (SELECT r.title FROM tasks r WHERE r.id = tasks.thread_id), title_by = 'thread' "
        "WHERE title IS NULL AND thread_id IS NOT NULL"
    )
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


# tasks columns that hold JSON (see _migrate) - handed to callers as Python values.
_JSON_COLUMNS = ("tools", "plan_steps", "git_base", "artifacts", "images")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    for k in _JSON_COLUMNS:
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                out[k] = None
    return out


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

PROJECT_CATEGORIES = ("work", "personal")

# Where a project stands, set by hand from the dashboard. The one automation:
# a project whose auto_in_progress setting is on moves to in_progress when a
# task under it starts (see _auto_in_progress).
PROJECT_STATUSES = ("not_started", "in_progress", "need_info", "on_hold", "completed")

# Claude Code --permission-mode values a task may run under (Claude Code lane
# only). None on a task means DEFAULT_PERMISSION_MODE. bypassPermissions and
# dontAsk are left out on purpose: unattended, they'd skip the phone approvals.
PERMISSION_MODES = ("acceptEdits", "plan", "auto", "manual")
DEFAULT_PERMISSION_MODE = "acceptEdits"


def add_project(name: str, path: str, category: str = "work") -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO projects (name, path, active, created_at, category) VALUES (?, ?, 1, ?, ?)",
            (name, path, _now(), category),
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


def set_project_category(project_id: int, category: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET category = ? WHERE id = ?", (category, project_id))
        conn.commit()


def set_project_status(project_id: int, status: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
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


def _reply_insert_pos(conn: sqlite3.Connection) -> float:
    """queue_pos for a reply in an existing chat: it's part of that chat,
    not a new task, so by default it's next in line: ahead of every queued
    new task, behind replies already waiting there (so replies keep the
    order they were sent). It's an ordinary queue item after that - it can
    be moved anywhere or started right away like any other."""
    rows = conn.execute(
        "SELECT queue_pos, thread_id FROM tasks WHERE status = 'queued' AND queue_pos IS NOT NULL "
        "ORDER BY queue_pos ASC"
    ).fetchall()
    prev = None
    for r in rows:
        if r["thread_id"] is None:
            return (prev + r["queue_pos"]) / 2 if prev is not None else r["queue_pos"] - 1
        prev = r["queue_pos"]
    return prev + 1 if prev is not None else 0.0


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
    title: Optional[str] = None,
    permission_mode: Optional[str] = None,
    agent_id: Optional[str] = None,
    tools: Optional[list[str]] = None,
    routing_id: Optional[int] = None,
    source: Optional[str] = None,
    risk: Optional[str] = None,
    images: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Defaults (agent lane, claude-code) are what every pre-Octopus caller -
    the plain task endpoint, todos - has always meant. A reply takes its
    thread's title; a new chat gets `title` if given (a recipe's name),
    else a quick one from the prompt that the namer loop can improve.
    agent_id / tools / routing_id / source / risk record Octopus's decision
    (see octopus/plan.py); a task made without it just leaves them empty."""
    if model_id is None and lane == "agent":
        model_id = "claude-code"
    with _lock:
        conn = get_conn()
        if thread_id is not None:
            root = conn.execute("SELECT title FROM tasks WHERE id = ?", (thread_id,)).fetchone()
            title, title_by = (root["title"] if root and root["title"] else quick_title(prompt)), "thread"
        elif title:
            title_by = "recipe"
        else:
            title, title_by = quick_title(prompt), "text"
        cur = conn.execute(
            "INSERT INTO tasks (project_id, prompt, priority, status, created_at, thread_id, "
            "lane, model_id, category, route_reason, routed_by, queue_pos, title, title_by, permission_mode, "
            "agent_id, tools, routing_id, source, risk, images) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, prompt, priority, _now(), thread_id,
             lane, model_id, category, route_reason, routed_by,
             _reply_insert_pos(conn) if thread_id is not None else _queue_insert_pos(conn, priority),
             title, title_by, permission_mode,
             agent_id, json.dumps(tools) if tools is not None else None, routing_id, source, risk,
             json.dumps(images) if images else None),
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


def move_after(task_id: int, after_id: Optional[int]) -> None:
    """Put a queued task right behind another queued one (None = first in line)."""
    with _lock:
        conn = get_conn()
        order = [r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE status = 'queued' ORDER BY queue_pos ASC, id ASC").fetchall()]
        if task_id not in order:
            return
        order.remove(task_id)
        order.insert(order.index(after_id) + 1 if after_id in order else 0, task_id)
    reorder_queue(order)


# What an edit may change on a task that hasn't started yet.
EDITABLE_TASK_FIELDS = ("prompt", "title", "title_by", "priority", "project_id",
                        "lane", "model_id", "category", "route_reason", "routed_by", "permission_mode")


def update_queued_task(task_id: int, fields: dict[str, Any]) -> bool:
    """Edit a task that's still queued. A new priority re-files it the way a
    new task of that priority would be; a new title renames the whole chat.
    False if it had already started (or is gone)."""
    fields = {k: v for k, v in fields.items() if k in EDITABLE_TASK_FIELDS}
    with _lock:
        conn = get_conn()
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task or task["status"] != "queued":
            return False
        if "priority" in fields and fields["priority"] != task["priority"]:
            conn.execute("UPDATE tasks SET queue_pos = NULL WHERE id = ?", (task_id,))  # don't count itself
            fields["queue_pos"] = _queue_insert_pos(conn, fields["priority"])
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
        if "title" in fields and task["thread_id"] is None:
            conn.execute("UPDATE tasks SET title = ? WHERE thread_id = ?", (fields["title"], task_id))
        conn.commit()
        return True


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
            "UPDATE tasks SET status = 'queued', paused = 0, error_message = NULL, resume_at = NULL, started_at = NULL, "
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
            "UPDATE tasks SET status = 'cancelled', paused = 0, finished_at = ?, error_message = 'Stopped from the dashboard' "
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


def set_thread_title(root_task_id: int, title: Optional[str], title_by: str,
                     only_if_by: Optional[str] = None) -> bool:
    """Rename a chat - the root and every reply, so lists showing any one
    message show the chat's name. title=None only changes title_by.
    only_if_by skips the rename if the chat was renamed some other way
    meanwhile (e.g. the namer loop won't overwrite a recipe's name)."""
    with _lock:
        conn = get_conn()
        guard, args = (" AND title_by = ?", (only_if_by,)) if only_if_by else ("", ())
        if title is None:
            cur = conn.execute("UPDATE tasks SET title_by = ? WHERE id = ?" + guard, (title_by, root_task_id, *args))
        else:
            cur = conn.execute("UPDATE tasks SET title = ?, title_by = ? WHERE id = ?" + guard,
                               (title, title_by, root_task_id, *args))
            if cur.rowcount:
                conn.execute("UPDATE tasks SET title = ? WHERE thread_id = ?", (title, root_task_id))
        conn.commit()
        return cur.rowcount > 0


def tasks_needing_titles(limit: int = 5) -> list[dict[str, Any]]:
    """Chats still named by a quick cut of their prompt, newest first."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, prompt FROM tasks WHERE thread_id IS NULL AND title_by = 'text' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_thread_model(root_task_id: int) -> Optional[str]:
    """The model the chat's latest message was sent to. A chat can change models
    between messages, so a reply that doesn't pick one stays on this - not on
    whatever the chat started with."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT model_id FROM tasks WHERE (id = ? OR thread_id = ?) AND model_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (root_task_id, root_task_id),
        ).fetchone()
        return row["model_id"] if row else None


def set_thread_summary(root_task_id: int, summary: str, key: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET summary = ?, summary_key = ? WHERE id = ?", (summary, key, root_task_id))
        conn.commit()


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


CHAT_STATUS_GROUPS = {
    "active": ("queued", "running", "rate_limited"),
    "done": ("done",),
    "error": ("error",),
    "cancelled": ("cancelled",),
}
_SNIPPET_MARGIN = 60


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _flat(text: Optional[str], limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _snippet(text: str, terms: list[str]) -> Optional[str]:
    """The stretch of `text` around the first search term found in it."""
    flat = " ".join(text.split())
    low = flat.lower()
    hits = [i for i in (low.find(t) for t in terms) if i >= 0]
    if not hits:
        return None
    at = min(hits)
    start, end = max(0, at - _SNIPPET_MARGIN), min(len(flat), at + _SNIPPET_MARGIN * 2)
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def list_chats(q: str = "", status: str = "all", project_ids: Optional[list[int]] = None,
               lane: Optional[str] = None, limit: int = 40, offset: int = 0) -> dict[str, Any]:
    """The message centre: one entry per chat (a root task and its replies),
    the chat with the most recent message first.

    q         every word must appear somewhere in the chat - its title, its
              project's name, or any prompt / reply / error in it. Case-blind.
    status    all | needs (waiting on an approval, failed or needing review) |
              active | done | error | cancelled - judged by the chat's latest message.
    project_ids / lane   narrow to those projects / one lane (agent, api, assistant).
    Each entry carries `preview` (the latest message) and, while searching,
    `snippet` (the newest message that matched, with a little context)."""
    terms = [w.lower() for w in q.split()][:8]
    where, args = ["1=1"], []
    for term in terms:
        where.append(
            "(r.title LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\' OR EXISTS ("
            "SELECT 1 FROM tasks x WHERE COALESCE(x.thread_id, x.id) = l.root_id AND ("
            "x.prompt LIKE ? ESCAPE '\\' OR x.result_summary LIKE ? ESCAPE '\\' OR x.error_message LIKE ? ESCAPE '\\')))"
        )
        args += [_like(term)] * 5
    if status == "needs":
        where.append(
            "(lt.status IN ('error', 'needs_review') OR EXISTS (SELECT 1 FROM approvals a JOIN tasks x ON x.id = a.task_id "
            "WHERE a.status = 'pending' AND COALESCE(x.thread_id, x.id) = l.root_id))"
        )
    elif status in CHAT_STATUS_GROUPS:
        group = CHAT_STATUS_GROUPS[status]
        where.append(f"lt.status IN ({','.join('?' * len(group))})")
        args += group
    if project_ids is not None:
        where.append(f"r.project_id IN ({','.join('?' * len(project_ids))})")
        args += project_ids
    if lane:
        where.append("r.lane = ?")
        args.append(lane)
    frm = (
        "FROM (SELECT COALESCE(thread_id, id) AS root_id, MAX(id) AS last_id, COUNT(*) AS turns "
        "      FROM tasks GROUP BY COALESCE(thread_id, id)) l "
        "JOIN tasks r ON r.id = l.root_id JOIN tasks lt ON lt.id = l.last_id JOIN projects p ON p.id = r.project_id "
        "WHERE " + " AND ".join(where)
    )
    with _lock:
        conn = get_conn()
        total = conn.execute("SELECT COUNT(*) " + frm, args).fetchone()[0]
        page = conn.execute(
            "SELECT l.root_id, l.last_id, l.turns, p.name AS project_name " + frm
            + " ORDER BY COALESCE(lt.finished_at, lt.started_at, lt.created_at) DESC, l.last_id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        if not page:
            return {"chats": [], "total": total}
        roots = [r["root_id"] for r in page]
        marks = ",".join("?" * len(roots))
        msgs = conn.execute(
            f"SELECT *, COALESCE(thread_id, id) AS root_id FROM tasks WHERE id IN ({marks}) OR thread_id IN ({marks}) ORDER BY id",
            roots + roots,
        ).fetchall()
        waiting = {
            r["task_id"] for r in conn.execute(
                "SELECT task_id FROM approvals WHERE status = 'pending' AND task_id IN "
                f"(SELECT id FROM tasks WHERE id IN ({marks}) OR thread_id IN ({marks}))", roots + roots
            ).fetchall()
        }
    by_root: dict[int, list[Any]] = {}
    for m in msgs:
        by_root.setdefault(m["root_id"], []).append(m)

    def reply_of(m) -> Optional[str]:
        if m["status"] == "done":
            return m["result_summary"] or "(done, no summary)"
        if m["status"] in ("error", "needs_review"):
            return m["error_message"] or "Something went wrong."
        return None

    chats = []
    for row in page:
        thread = by_root[row["root_id"]]
        root, last = thread[0], thread[-1]
        reply = reply_of(last)
        preview = ({"role": "reply", "text": _flat(reply)} if reply is not None
                   else {"role": "you", "text": _flat(last["prompt"])})
        snippet = None
        if terms:
            for m in reversed(thread):
                for role, text in (("reply", reply_of(m)), ("you", m["prompt"])):
                    found = _snippet(text, terms) if text else None
                    if found:
                        snippet = {"role": role, "text": found, "task_id": m["id"]}
                        break
                if snippet:
                    break
        costs = [m["cost_usd"] for m in thread if m["cost_usd"] is not None]
        chats.append({
            "id": root["id"],
            "title": root["title"] or _flat(root["prompt"], 80),
            "project_id": root["project_id"],
            "project_name": row["project_name"],
            "lane": root["lane"],
            "model_id": root["model_id"],
            "status": last["status"],
            "needs_you": last["status"] in ("error", "needs_review") or any(m["id"] in waiting for m in thread),
            "turns": row["turns"],
            "last_at": last["finished_at"] or last["started_at"] or last["created_at"],
            "preview": preview,
            "snippet": snippet,
            "cost_usd": sum(costs) if costs else None,
        })
    return {"chats": chats, "total": total}


def get_next_queued_task(lane: str = "agent", exclude_models: tuple = (),
                         hold_low_priority: bool = False) -> Optional[dict[str, Any]]:
    """Next task to run in this lane, only from active projects: a
    rate_limited task whose resume_at has passed first (it was already
    mid-run), then queued tasks in queue_pos order. exclude_models skips
    tasks for models currently known to be rate-limited, so the api lane
    doesn't keep hitting a limited model. hold_low_priority leaves Low
    (priority < 0) tasks waiting - "save the session for urgent work".
    A chat's messages run one at a time and in the order they were sent, so
    a message waits while its chat has one running or an earlier one pending
    (see CHAT_FREE)."""
    with _lock:
        conn = get_conn()
        now = _now()
        q = f"""
            SELECT t.* FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE p.active = 1
              AND t.lane = ?
              AND (
                    t.status = 'queued'
                    OR (t.status = 'rate_limited' AND t.resume_at IS NOT NULL AND t.resume_at <= ?)
                  )
              AND t.paused = 0
              AND {CHAT_FREE}
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


def _auto_in_progress(conn: sqlite3.Connection, task_id: int) -> None:
    """Call just before a task's status becomes 'running'. If this is its first
    start (a resume after a rate limit keeps started_at), and its project has
    auto_in_progress on (the default), move the project to in_progress."""
    conn.execute(
        "UPDATE projects SET status = 'in_progress' "
        "WHERE id = (SELECT project_id FROM tasks WHERE id = ? AND started_at IS NULL) "
        "AND status != 'in_progress' "
        "AND COALESCE((SELECT auto_in_progress FROM project_settings WHERE project_id = projects.id), 1) = 1",
        (task_id,),
    )


def mark_running(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        _auto_in_progress(conn, task_id)
        conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, resume_at = NULL, run_now = 0 WHERE id = ?",
            (_now(), task_id),
        )
        conn.commit()


def running_task_ids(lanes: tuple[str, ...]) -> list[int]:
    with _lock:
        rows = get_conn().execute(
            f"SELECT id FROM tasks WHERE status = 'running' AND lane IN ({','.join('?' * len(lanes))}) ORDER BY id",
            lanes,
        ).fetchall()
    return [r["id"] for r in rows]


def claim_task(task_id: int) -> bool:
    """mark_running, but only if the task is still waiting - so the queue
    and a "Proceed now" can never both start the same task."""
    with _lock:
        conn = get_conn()
        waiting = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND status IN ('queued', 'rate_limited') AND paused = 0", (task_id,)
        ).fetchone()
        if waiting:
            _auto_in_progress(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, resume_at = NULL, run_now = 0 "
            "WHERE id = ? AND status IN ('queued', 'rate_limited') AND paused = 0",
            (_now(), task_id),
        )
        conn.commit()
        return cur.rowcount > 0


def run_now_tasks() -> list[dict[str, Any]]:
    """Queued tasks the user asked to start right away, alongside whatever
    is already running (still one at a time within a chat)."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            f"SELECT t.* FROM tasks t JOIN projects p ON p.id = t.project_id "
            f"WHERE t.run_now = 1 AND t.status = 'queued' AND t.paused = 0 AND p.active = 1 AND {CHAT_FREE} ORDER BY t.id ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_run_now(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET run_now = 1, paused = 0 WHERE id = ? AND status = 'queued'", (task_id,))
        conn.commit()


def mark_done(task_id: int, result_summary: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'done', paused = 0, result_summary = ?, finished_at = ? WHERE id = ?",
            (result_summary, _now(), task_id),
        )
        conn.commit()


def mark_error(task_id: int, error_message: str, needs_review: bool = True) -> None:
    with _lock:
        conn = get_conn()
        status = "needs_review" if needs_review else "error"
        conn.execute(
            "UPDATE tasks SET status = ?, paused = 0, error_message = ?, finished_at = ? WHERE id = ?",
            (status, error_message, _now(), task_id),
        )
        conn.commit()


def mark_rate_limited(task_id: int, resume_at_iso: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'rate_limited', paused = 0, resume_at = ? WHERE id = ?",
            (resume_at_iso, task_id),
        )
        conn.commit()


PAUSABLE = ("queued", "rate_limited", "running")


def set_paused(task_id: int, paused: bool) -> bool:
    """Hold or release a task without changing its status or its place in the
    queue. False if it isn't in a state that can be held (it already finished)."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            f"UPDATE tasks SET paused = ? WHERE id = ? AND status IN ({','.join('?' * len(PAUSABLE))})",
            (int(paused), task_id, *PAUSABLE),
        )
        conn.commit()
        return cur.rowcount > 0


def cancel_task(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', paused = 0, finished_at = ? WHERE id = ? AND status IN ('queued', 'rate_limited')",
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
            "SELECT a.*, t.prompt AS task_prompt, t.title AS task_title, t.project_id, t.model_id, t.lane FROM approvals a "
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


# ------------------------------------------------------------- project notes

NOTE_KINDS = ("guideline", "requirement", "note")
_NOTE_HEADINGS = (("guideline", "Guidelines"), ("requirement", "Requirements"), ("note", "Notes"))


def list_project_notes(project_id: int) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM project_notes WHERE project_id = ? ORDER BY id ASC", (project_id,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_project_note(note_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM project_notes WHERE id = ?", (note_id,)).fetchone()
        return _row_to_dict(row) if row else None


def add_project_note(project_id: int, text: str, kind: str = "note") -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO project_notes (project_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                           (project_id, kind if kind in NOTE_KINDS else "note", text, _now()))
        conn.commit()
        return get_project_note(cur.lastrowid)


def update_project_note(note_id: int, text: Optional[str] = None, kind: Optional[str] = None) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        if text is not None:
            conn.execute("UPDATE project_notes SET text = ? WHERE id = ?", (text, note_id))
        if kind in NOTE_KINDS:
            conn.execute("UPDATE project_notes SET kind = ? WHERE id = ?", (kind, note_id))
        conn.commit()
        return get_project_note(note_id)


def delete_project_note(note_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM project_notes WHERE id = ?", (note_id,))
        conn.commit()


def project_notes_prompt(project_id: Optional[int]) -> str:
    """The project's notes as a block of instructions, grouped by kind, for
    whatever model is about to work on it. Empty when there are none."""
    notes = list_project_notes(project_id) if project_id else []
    sections = []
    for kind, heading in _NOTE_HEADINGS:
        items = [n["text"].strip() for n in notes if n["kind"] == kind]
        if items:
            sections.append(f"{heading}:\n" + "\n".join(f"- {t.replace(chr(10), chr(10) + '  ')}" for t in items))
    if not sections:
        return ""
    return ("The user has recorded these notes about this project. Treat them as standing "
            "instructions and keep them in mind for everything you do here.\n\n" + "\n\n".join(sections))


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
        # One count per chat, by its latest message - a reply is part of its
        # chat, not another task. Except 'queued', which counts queue items
        # (a waiting reply is one), so it matches the queue the dashboard lists.
        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) as n FROM tasks "
                "WHERE id IN (SELECT MAX(id) FROM tasks GROUP BY COALESCE(thread_id, id)) GROUP BY status"
            ).fetchall()
        }
        counts.pop("queued", None)
        n_queued = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued'").fetchone()["n"]
        if n_queued:
            counts["queued"] = n_queued
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
# Starter quick buttons (Home). Each fills the Ask box with `prompt`; `pic` also opens the
# picture picker. The list is edited from the dashboard and saved whole as the "quick_buttons" setting.
QUICK_BUTTON_DEFAULTS: list[dict[str, Any]] = [
    {"id": "q-sanskrit", "icon": "📜", "label": "Sanskrit exam paper", "pic": True,
     "prompt": "Convert the attached photo of a Sanskrit exam paper into clean, typed Devanagari text. "
               "Keep every question number, marks and instruction exactly as written, and lay it out like a printable question paper."},
    {"id": "q-photo-text", "icon": "📷", "label": "Photo to text", "pic": True,
     "prompt": "Read the attached photo and type out all the text in it, keeping the original layout and order."},
    {"id": "q-summarize", "icon": "📝", "label": "Summarize", "pic": False,
     "prompt": "Summarize this in a few short points:\n\n"},
    {"id": "q-reply", "icon": "💬", "label": "Write a reply", "pic": False,
     "prompt": "Write a polite, short reply to this message:\n\n"},
    {"id": "q-translate", "icon": "🌐", "label": "Translate", "pic": False,
     "prompt": "Translate this into English (or into Hindi if it is already in English):\n\n"},
    {"id": "q-fix", "icon": "✏️", "label": "Fix spelling", "pic": False,
     "prompt": "Fix the spelling and grammar in this, changing nothing else:\n\n"},
]

SETTINGS_DEFAULTS: dict[str, Any] = {
    "quick_buttons": QUICK_BUTTON_DEFAULTS,
    "notify_on_resume": False,       # ping once when a rate-limit pause ends
    "hold_low_when_busy": True,      # hold Low tasks while the 5-hour window is over 85%
    "sleep_mode": None,              # {"since", "until"} while "I'm going to sleep" is on - see sleep_mode_active
    "notify_weekly_pace": True,      # warn when the weekly limit is being used up too fast
    "monthly_budget_usd": 100.0,
    # Octopus (see octopus/autonomy.py). autonomy: assist | execute | autonomous.
    # plan_review: when the plan card is shown before anything runs -
    # risky (default: only for higher-risk requests) | always | never.
    "autonomy": "execute",
    "plan_review": "risky",
    # Voice (see octopus/voice/). Providers are ids from octopus.voice.
    "voice_stt": "browser",
    "voice_tts": "browser",
    "voice_tts_voice": "",           # a macOS voice name for tts 'macos'; "" = system default
    "voice_speak_replies": True,
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


SLEEP_DEFAULT_HOURS = 8.0


def sleep_mode_active() -> bool:
    """True while "I'm going to sleep" is on. It ends by itself at "until", so a
    forgotten toggle can't leave the queue in overnight mode for good."""
    sm = get_setting("sleep_mode")
    if not sm or not sm.get("until"):
        return False
    return datetime.fromisoformat(sm["until"]) > datetime.now(timezone.utc)


def start_sleep_mode(hours: float = SLEEP_DEFAULT_HOURS) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    sm = {"since": now.isoformat(), "until": (now + timedelta(hours=hours)).isoformat()}
    set_setting("sleep_mode", sm)
    return sm


def end_sleep_mode() -> None:
    set_setting("sleep_mode", None)


# ------------------------------------------------------- project settings

PROJECT_SETTINGS_DEFAULTS: dict[str, Any] = {
    "default_priority": 0,
    "notify_approvals": True,
    "notify_failures": True,
    "notify_completions": True,
    "quiet_hours": False,
    "allow_commands": [],
    "budget_usd": 20.0,
    "auto_in_progress": True,
}
_BOOL_SETTINGS = ("notify_approvals", "notify_failures", "notify_completions", "quiet_hours", "auto_in_progress")


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
            "WHERE t.lane IN ('api', 'assistant') AND t.cost_usd IS NOT NULL AND COALESCE(t.finished_at, t.created_at) >= ?",
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
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM tasks WHERE project_id = ? AND lane IN ('api', 'assistant') "
            "AND COALESCE(finished_at, created_at) >= ?",
            (project_id, _utc_iso(month0)),
        ).fetchone()
        return float(row["s"])


# ------------------------------------------------------- personal assistant
# Storage for assistant/: connected accounts (no tokens), the activity log,
# per-chat working memory, reminders, and the few task/approval helpers the
# assistant lane needs. Approvals themselves reuse the `approvals` table:
# tool_name 'assistant:<tool>.<action>', tool_input a JSON payload.

def get_connection(provider: str) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM connections WHERE provider = ?", (provider,)).fetchone()
        if not row:
            return None
        out = _row_to_dict(row)
        out["scopes"] = json.loads(out["scopes"] or "[]")
        return out


def save_connection(provider: str, account: Optional[str], status: str, scopes: Optional[list[str]] = None,
                    error: Optional[str] = None) -> dict[str, Any]:
    now = _now()
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO connections (provider, account, status, scopes, connected_at, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(provider) DO UPDATE SET "
            "account = COALESCE(excluded.account, account), status = excluded.status, "
            "scopes = CASE WHEN excluded.scopes = '[]' THEN scopes ELSE excluded.scopes END, "
            "connected_at = CASE WHEN excluded.status = 'connected' THEN excluded.connected_at ELSE connected_at END, "
            "last_error = excluded.last_error, updated_at = excluded.updated_at",
            (provider, account, status, json.dumps(scopes or []), now if status == "connected" else None,
             (error or "")[:300] or None, now),
        )
        conn.commit()
        return get_connection(provider)  # type: ignore[return-value]


def delete_connection(provider: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM connections WHERE provider = ?", (provider,))
        conn.commit()


def add_activity(tool: str, action: str, target: Optional[str] = None, approval: str = "not_required",
                 result: str = "ok", error: Optional[str] = None, task_id: Optional[int] = None,
                 approval_id: Optional[int] = None, ref: Optional[str] = None) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO assistant_activity (created_at, tool, action, target, approval, result, error, "
            "task_id, approval_id, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), tool, action, (target or "")[:200] or None, approval, result, (error or "")[:300] or None,
             task_id, approval_id, (ref or "")[:200] or None),
        )
        conn.commit()
        return cur.lastrowid


def list_activity(limit: int = 100, tool: Optional[str] = None, action: Optional[str] = None) -> list[dict[str, Any]]:
    q, params = "SELECT * FROM assistant_activity WHERE 1=1", []  # type: (str, list[Any])
    if tool:
        q += " AND tool = ?"
        params.append(tool)
    if action:
        q += " AND action = ?"
        params.append(action)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute(q, params).fetchall()]


def get_assistant_context(thread_id: int) -> dict[str, Any]:
    with _lock:
        row = get_conn().execute("SELECT data FROM assistant_context WHERE thread_id = ?", (thread_id,)).fetchone()
        return json.loads(row["data"]) if row else {}


def set_assistant_context(thread_id: int, data: dict[str, Any]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("INSERT INTO assistant_context (thread_id, data, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(thread_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                     (thread_id, json.dumps(data), _now()))
        conn.commit()


def add_reminder(text: str, due_at_iso: str, task_id: Optional[int] = None) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO reminders (text, due_at, status, created_at, task_id) VALUES (?, ?, 'pending', ?, ?)",
                           (text.strip()[:300], due_at_iso, _now(), task_id))
        conn.commit()
        return get_reminder(cur.lastrowid)  # type: ignore[return-value]


def get_reminder(reminder_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_reminders(statuses: Optional[list[str]] = None, limit: int = 100) -> list[dict[str, Any]]:
    q, params = "SELECT * FROM reminders WHERE 1=1", []  # type: (str, list[Any])
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        params += statuses
    q += " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, due_at ASC LIMIT ?"
    params.append(limit)
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute(q, params).fetchall()]


def cancel_reminder(reminder_id: int) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (reminder_id,))
        conn.commit()
        return cur.rowcount == 1


def due_reminders() -> list[dict[str, Any]]:
    """Pending reminders whose time has come. A reminder missed while the Mac
    slept fires once, late, on wake."""
    with _lock:
        rows = get_conn().execute("SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at ASC",
                                  (_now(),)).fetchall()
        return [_row_to_dict(r) for r in rows]


def mark_reminder_fired(reminder_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE reminders SET status = 'fired', fired_at = ? WHERE id = ? AND status = 'pending'",
                     (_now(), reminder_id))
        conn.commit()


def claim_approval(approval_id: int, approved: bool, note: Optional[str] = None) -> bool:
    """Decide a pending approval and say whether *this* call is the one that
    decided it - so an action is executed exactly once even if two clicks race."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, note = ? WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "denied", _now(), (note or "").strip()[:500] or None, approval_id),
        )
        conn.commit()
        return cur.rowcount == 1


def set_approval_input(approval_id: int, tool_input: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE approvals SET tool_input = ? WHERE id = ?", (tool_input, approval_id))
        conn.commit()


def expire_assistant_approvals(older_than_iso: str) -> list[dict[str, Any]]:
    """Time out assistant approvals nobody answered. Returns the rows expired."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' AND tool_name LIKE 'assistant:%' AND created_at < ?",
            (older_than_iso,)).fetchall()
        for r in rows:
            conn.execute("UPDATE approvals SET status = 'timed_out', decided_at = ? WHERE id = ?", (_now(), r["id"]))
        conn.commit()
        return [_row_to_dict(r) for r in rows]


def append_task_result(task_id: int, text: str) -> None:
    """Add a line to a finished task's answer (e.g. "Sent" after an approval)."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET result_summary = COALESCE(result_summary, '') || ? WHERE id = ?",
                     ("\n\n" + text.strip(), task_id))
        conn.commit()


def recent_assistant_thread(within_minutes: int = 15) -> Optional[int]:
    """Root task id of the assistant chat that was last active within the
    window - what a voice request with no thread attaches to."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    with _lock:
        row = get_conn().execute(
            "SELECT COALESCE(thread_id, id) AS root FROM tasks WHERE lane = 'assistant' AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1", (since,)).fetchone()
        return row["root"] if row else None


# ------------------------------------------------- Octopus: routing log & plans

def add_routing(request: str, plan: dict[str, Any], status: str = "proposed", source: str = "api",
                decided_by: str = "auto", task_id: Optional[int] = None) -> dict[str, Any]:
    """Log a request and the plan Octopus made for it (see octopus/plan.py)."""
    with _lock:
        conn = get_conn()
        now = _now()
        cur = conn.execute(
            "INSERT INTO routing (created_at, updated_at, source, request, status, category, agent_id, model_id, "
            "project_id, risk, plan, task_id, decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, source, request, status, plan.get("category"), (plan.get("agent") or {}).get("id"),
             (plan.get("model") or {}).get("id"), (plan.get("project") or {}).get("id"), plan.get("risk"),
             json.dumps(plan), task_id, decided_by))
        conn.commit()
        return get_routing(cur.lastrowid)


def _routing_row(row: sqlite3.Row) -> dict[str, Any]:
    out = _row_to_dict(row)
    try:
        out["plan"] = json.loads(out.get("plan") or "{}")
    except ValueError:
        out["plan"] = {}
    return out


def get_routing(routing_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM routing WHERE id = ?", (routing_id,)).fetchone()
        return _routing_row(row) if row else None


def update_routing(routing_id: int, plan: Optional[dict[str, Any]] = None, **fields: Any) -> Optional[dict[str, Any]]:
    """Change a logged plan's status / task / decided_by. A new `plan` replaces
    the stored one and refreshes the columns copied out of it."""
    allowed = {k: v for k, v in fields.items() if k in ("status", "task_id", "decided_by", "source")}
    if plan is not None:
        allowed.update(plan=json.dumps(plan), category=plan.get("category"), agent_id=(plan.get("agent") or {}).get("id"),
                       model_id=(plan.get("model") or {}).get("id"), project_id=(plan.get("project") or {}).get("id"),
                       risk=plan.get("risk"))
    if not allowed:
        return get_routing(routing_id)
    allowed["updated_at"] = _now()
    with _lock:
        conn = get_conn()
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE routing SET {sets} WHERE id = ?", (*allowed.values(), routing_id))
        conn.commit()
        return get_routing(routing_id)


def claim_routing(routing_id: int, to_status: str, from_statuses: tuple = ("proposed", "needs_input")) -> bool:
    """Move a plan out of 'waiting' exactly once - so a double-tapped Run
    (or a Run racing a Cancel) can't queue the same request twice."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            f"UPDATE routing SET status = ?, updated_at = ? WHERE id = ? AND status IN ({','.join('?' * len(from_statuses))})",
            (to_status, _now(), routing_id, *from_statuses))
        conn.commit()
        return cur.rowcount > 0


def list_routing(limit: int = 100, status: Optional[str] = None, category: Optional[str] = None) -> list[dict[str, Any]]:
    """Newest first, each row joined with what became of its task."""
    with _lock:
        q = ("SELECT r.*, t.status AS task_status, t.duration_ms, t.cost_usd, t.input_tokens, t.output_tokens, "
             "t.error_message AS task_error, t.title AS task_title, t.lane AS task_lane, "
             "SUBSTR(COALESCE(t.result_summary, ''), 1, 240) AS result_excerpt, p.name AS project_name "
             "FROM routing r LEFT JOIN tasks t ON t.id = r.task_id LEFT JOIN projects p ON p.id = r.project_id WHERE 1=1")
        params: list[Any] = []
        if status:
            q += " AND r.status = ?"
            params.append(status)
        if category:
            q += " AND r.category = ?"
            params.append(category)
        q += " ORDER BY r.id DESC LIMIT ?"
        params.append(limit)
        return [_routing_row(r) for r in get_conn().execute(q, params).fetchall()]


def pending_plan(within_minutes: int = 30) -> Optional[dict[str, Any]]:
    """The newest plan still waiting on the human (Run / Cancel, or an answer
    to "which project?") - what a spoken "yes, go ahead" refers to."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM routing WHERE status IN ('proposed', 'needs_input') AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1", (since,)).fetchone()
        return _routing_row(row) if row else None


def expire_old_plans(older_than_minutes: int = 120) -> int:
    since = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    with _lock:
        conn = get_conn()
        cur = conn.execute("UPDATE routing SET status = 'expired', updated_at = ? "
                           "WHERE status IN ('proposed', 'needs_input') AND created_at < ?", (_now(), since))
        conn.commit()
        return cur.rowcount


def _set_task_json(task_id: int, column: str, value: Any) -> None:
    assert column in _JSON_COLUMNS
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (json.dumps(value), task_id))
        conn.commit()


def set_task_plan_steps(task_id: int, steps: list[dict[str, Any]]) -> None:
    """The live plan an agent reported (Claude Code's todo list): [{content, status}]."""
    _set_task_json(task_id, "plan_steps", steps)


def set_task_git_base(task_id: int, base: dict[str, Any]) -> None:
    _set_task_json(task_id, "git_base", base)


def set_task_artifacts(task_id: int, artifacts: list[dict[str, Any]]) -> None:
    _set_task_json(task_id, "artifacts", artifacts)


def model_category_stats(days: int = 30) -> dict[str, dict[str, int]]:
    """{"model_id|category": {"n", "done", "failed"}} over the last `days` -
    the "historical success" the router weighs."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock:
        rows = get_conn().execute(
            "SELECT model_id, category, COUNT(*) AS n, SUM(status = 'done') AS done, "
            "SUM(status IN ('error', 'needs_review')) AS failed FROM tasks "
            "WHERE created_at >= ? AND model_id IS NOT NULL AND status IN ('done', 'error', 'needs_review') "
            "GROUP BY model_id, category", (since,)).fetchall()
        return {f"{r['model_id']}|{r['category']}": {"n": r["n"], "done": r["done"] or 0, "failed": r["failed"] or 0}
                for r in rows}


def recent_thread(within_minutes: int = 15, lanes: Optional[tuple] = None) -> Optional[int]:
    """Root task id of the chat that saw the most recent activity within the
    window, in any lane (or only `lanes`) - what a follow-up like "fix the
    second one" attaches to. Instant control replies never count."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    lane_sql = f" AND lane IN ({','.join('?' * len(lanes))})" if lanes else " AND lane != 'control'"
    with _lock:
        row = get_conn().execute(
            "SELECT COALESCE(thread_id, id) AS root, COALESCE(finished_at, started_at, created_at) AS at FROM tasks "
            f"WHERE COALESCE(finished_at, started_at, created_at) >= ?{lane_sql} ORDER BY at DESC, id DESC LIMIT 1",
            (since, *(lanes or ()))).fetchone()
        return row["root"] if row else None


def list_task_approvals(task_id: int) -> list[dict[str, Any]]:
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()]


def list_recent_events(limit: int = 60) -> list[dict[str, Any]]:
    """What Octopus did lately, across every task, newest first - the raw
    material for the Command Center's activity feed."""
    with _lock:
        rows = get_conn().execute(
            "SELECT e.*, t.title AS task_title, t.project_id, t.lane, t.model_id, p.name AS project_name "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id JOIN projects p ON p.id = t.project_id "
            "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
