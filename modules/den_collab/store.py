"""
What the sharing module remembers, in its own tables of the Den's database (no core change):

  collab_members   a person invited to share: name, email, a hash of their private link token, last seen
  collab_shares    what they were given: a whole project or one chat, and their role on it
  collab_authors   which person wrote which message/task (the Den itself only knows "a task")
  collab_events    a short "who did what" feed per project ("Priya added a task")

Roles: viewer (reads), editor (adds tasks, chats), admin (also invites/removes people on that project).
The token is shown once in the invite link and only its SHA-256 is stored, like the dashboard login does.
"""

import hashlib
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import db

ROLES = ("viewer", "editor", "admin")
KINDS = ("project", "chat")
_RANK = {r: i for i, r in enumerate(ROLES)}
_ready = False
_ready_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def at_least(role: str, need: str) -> bool:
    return _RANK.get(role, -1) >= _RANK[need]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def init() -> None:
    global _ready
    with _ready_lock:
        if _ready:
            return
        with db._lock:
            c = db.get_conn()
            c.execute("CREATE TABLE IF NOT EXISTS collab_members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                      "email TEXT NOT NULL DEFAULT '', token_hash TEXT NOT NULL UNIQUE, hold INTEGER NOT NULL DEFAULT 0, "
                      "revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_seen TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS collab_shares (id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL, "
                      "kind TEXT NOT NULL, ref_id INTEGER NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL, "
                      "UNIQUE(member_id, kind, ref_id))")
            c.execute("CREATE TABLE IF NOT EXISTS collab_authors (task_id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, "
                      "name TEXT NOT NULL, created_at TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS collab_events (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, "
                      "who TEXT NOT NULL, what TEXT NOT NULL, created_at TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS collab_events_project ON collab_events(project_id, id)")
            c.commit()
        _ready = True


def _rows(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with db._lock:
        return [dict(r) for r in db.get_conn().execute(sql, args).fetchall()]


def _one(sql: str, args: tuple = ()) -> Optional[dict[str, Any]]:
    r = _rows(sql, args)
    return r[0] if r else None


def _exec(sql: str, args: tuple = ()) -> int:
    with db._lock:
        c = db.get_conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid or 0


# ------------------------------------------------------------------ members

def create_member(name: str, email: str = "", hold: bool = False) -> tuple[dict[str, Any], str]:
    """A new person and their one private token (returned once; only its hash is kept)."""
    init()
    token = secrets.token_urlsafe(24)
    mid = _exec("INSERT INTO collab_members (name, email, token_hash, hold, created_at) VALUES (?, ?, ?, ?, ?)",
                (name.strip()[:60], email.strip()[:120], _hash(token), 1 if hold else 0, now()))
    return get_member(mid), token


def reset_token(member_id: int) -> Optional[str]:
    """Give a person a fresh link (the old one stops working at once)."""
    init()
    if not get_member(member_id):
        return None
    token = secrets.token_urlsafe(24)
    _exec("UPDATE collab_members SET token_hash = ?, revoked = 0 WHERE id = ?", (_hash(token), member_id))
    return token


def get_member(member_id: int) -> Optional[dict[str, Any]]:
    init()
    return _one("SELECT id, name, email, hold, revoked, created_at, last_seen FROM collab_members WHERE id = ?", (member_id,))


def by_token(token: str) -> Optional[dict[str, Any]]:
    init()
    if not token or len(token) > 200:
        return None
    return _one("SELECT id, name, email, hold, revoked, created_at, last_seen FROM collab_members "
                "WHERE token_hash = ? AND revoked = 0", (_hash(token),))


def touch(member_id: int) -> None:
    _exec("UPDATE collab_members SET last_seen = ? WHERE id = ?", (now(), member_id))


def update_member(member_id: int, name: Optional[str] = None, email: Optional[str] = None, hold: Optional[bool] = None) -> None:
    if name is not None and name.strip():
        _exec("UPDATE collab_members SET name = ? WHERE id = ?", (name.strip()[:60], member_id))
    if email is not None:
        _exec("UPDATE collab_members SET email = ? WHERE id = ?", (email.strip()[:120], member_id))
    if hold is not None:
        _exec("UPDATE collab_members SET hold = ? WHERE id = ?", (1 if hold else 0, member_id))


def remove_member(member_id: int) -> None:
    init()
    _exec("DELETE FROM collab_shares WHERE member_id = ?", (member_id,))
    _exec("DELETE FROM collab_members WHERE id = ?", (member_id,))


def list_members() -> list[dict[str, Any]]:
    init()
    out = _rows("SELECT id, name, email, hold, revoked, created_at, last_seen FROM collab_members ORDER BY id")
    for m in out:
        m["shares"] = shares_of(m["id"])
    return out


# ------------------------------------------------------------------- shares

def set_share(member_id: int, kind: str, ref_id: int, role: str) -> None:
    if kind not in KINDS or role not in ROLES:
        raise ValueError("kind or role isn't valid")
    init()
    _exec("INSERT INTO collab_shares (member_id, kind, ref_id, role, created_at) VALUES (?, ?, ?, ?, ?) "
          "ON CONFLICT(member_id, kind, ref_id) DO UPDATE SET role = excluded.role",
          (member_id, kind, int(ref_id), role, now()))


def remove_share(member_id: int, kind: str, ref_id: int) -> None:
    init()
    _exec("DELETE FROM collab_shares WHERE member_id = ? AND kind = ? AND ref_id = ?", (member_id, kind, int(ref_id)))


def shares_of(member_id: int) -> list[dict[str, Any]]:
    init()
    return _rows("SELECT kind, ref_id, role FROM collab_shares WHERE member_id = ? ORDER BY id", (member_id,))


def members_of_project(project_id: int) -> list[dict[str, Any]]:
    init()
    return _rows("SELECT m.id, m.name, m.last_seen, s.role FROM collab_shares s JOIN collab_members m ON m.id = s.member_id "
                 "WHERE s.kind = 'project' AND s.ref_id = ? AND m.revoked = 0 ORDER BY m.name", (project_id,))


# ------------------------------------------------------------------ authors

def set_author(task_id: int, member: dict[str, Any]) -> None:
    init()
    _exec("INSERT OR REPLACE INTO collab_authors (task_id, member_id, name, created_at) VALUES (?, ?, ?, ?)",
          (task_id, member["id"], member["name"], now()))


def authors(task_ids: list[int]) -> dict[int, str]:
    init()
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    return {r["task_id"]: r["name"] for r in _rows(f"SELECT task_id, name FROM collab_authors WHERE task_id IN ({marks})", tuple(task_ids))}


def held_by_guests() -> list[dict[str, Any]]:
    """Tasks a guest wrote that are paused waiting for the owner's OK."""
    init()
    return _rows("SELECT a.task_id, a.name, t.project_id, t.prompt, t.created_at FROM collab_authors a JOIN tasks t ON t.id = a.task_id "
                 "WHERE t.paused = 1 AND t.status IN ('queued', 'rate_limited') AND a.name != '' ORDER BY a.task_id")


# ------------------------------------------------------------------- events

def log_event(project_id: int, who: str, what: str) -> None:
    init()
    _exec("INSERT INTO collab_events (project_id, who, what, created_at) VALUES (?, ?, ?, ?)", (project_id, who[:60], what[:200], now()))
    _exec("DELETE FROM collab_events WHERE project_id = ? AND id NOT IN (SELECT id FROM collab_events WHERE project_id = ? ORDER BY id DESC LIMIT 300)",
          (project_id, project_id))


def events(project_id: int, limit: int = 40) -> list[dict[str, Any]]:
    init()
    return _rows("SELECT who, what, created_at FROM collab_events WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit))
