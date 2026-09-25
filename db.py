"""
Simple SQLite-backed storage for the Claude task queue.

One process, low throughput (one task runs at a time), so a plain
threading.Lock around a single sqlite3 connection is plenty - no need
for a heavier DB. Keeping this dependency-free (sqlite3 is stdlib).
"""

import sqlite3
import threading
from datetime import datetime, timezone
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
    session_id TEXT
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
    decided_at TEXT
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
    conn.commit()


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


# ---------------------------------------------------------------- projects

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

def add_task(project_id: int, prompt: str, priority: int = 0, thread_id: Optional[int] = None) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO tasks (project_id, prompt, priority, status, created_at, thread_id) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (project_id, prompt, priority, _now(), thread_id),
        )
        conn.commit()
        return get_task(cur.lastrowid)


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


def get_next_queued_task() -> Optional[dict[str, Any]]:
    """Next task to run: queued tasks, or rate_limited tasks whose resume_at has passed,
    only from active projects. Highest priority first, then oldest first."""
    with _lock:
        conn = get_conn()
        now = _now()
        row = conn.execute(
            """
            SELECT t.* FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE p.active = 1
              AND (
                    t.status = 'queued'
                    OR (t.status = 'rate_limited' AND t.resume_at IS NOT NULL AND t.resume_at <= ?)
                  )
            ORDER BY t.priority DESC, t.created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
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
            "SELECT a.*, t.prompt AS task_prompt, t.project_id FROM approvals a "
            "JOIN tasks t ON t.id = a.task_id "
            "WHERE a.status = 'pending' ORDER BY a.created_at ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def decide_approval(approval_id: int, approved: bool) -> Optional[dict[str, Any]]:
    """No-op (returns the row unchanged) if this approval was already decided -
    e.g. it timed out server-side right as the dashboard's click came in."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "denied", _now(), approval_id),
        )
        conn.commit()
        return get_approval(approval_id)


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
        running = conn.execute("SELECT * FROM tasks WHERE status = 'running' LIMIT 1").fetchone()
        next_up = get_next_queued_task()
        rate_limited = conn.execute(
            "SELECT * FROM tasks WHERE status = 'rate_limited' ORDER BY resume_at ASC LIMIT 1"
        ).fetchone()
        return {
            "counts": counts,
            "running": _row_to_dict(running) if running else None,
            "next_up": next_up,
            "earliest_rate_limit_resume": _row_to_dict(rate_limited) if rate_limited else None,
        }
