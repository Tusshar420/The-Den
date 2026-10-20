"""To-do checklist: quick tick-off items for the right sidebar and the project pages.

Not the same thing as a project's Backlog (those are parked prompts that become real tasks
when queued). A checklist item is just a line you tick off yourself. It can sit under a
project (project_id) or stay unfiled. Pasting several lines makes one item per line.
Each item is "todo", "doing" (in progress) or "done"; the done column mirrors status == "done".

An item can be handed to Octopus (see send): to Up next (task_id, a queued task) or to the
project's Backlog (todo_id). It stays here and shows how that work is going (its `link`); when the
work finishes it ticks itself off, once (link_state remembers the last state seen).
"""

import re
from typing import Any, Optional

import db

_TABLE = """
CREATE TABLE IF NOT EXISTS checklist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    project_id INTEGER REFERENCES projects(id),
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT,
    status TEXT NOT NULL DEFAULT 'todo'
)
"""
_ready = False
MAX_LEN = 500
STATUSES = ("todo", "doing", "done")

# A leading list marker pasted from notes or a doc: "- ", "* ", "• ", "1. ", "2) ", "[ ] ", "[x] ".
_MARKER = re.compile(r"^\s*(?:[-*•–]\s+|\d{1,3}[.)]\s+)?(?:\[[ xX]?\]\s*)?")


def _conn():
    global _ready
    conn = db.get_conn()
    if not _ready:
        with db._lock:
            conn.execute(_TABLE)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(checklist_items)")}
            if "status" not in cols:   # lists made before "in progress" existed
                conn.execute("ALTER TABLE checklist_items ADD COLUMN status TEXT NOT NULL DEFAULT 'todo'")
                conn.execute("UPDATE checklist_items SET status = 'done' WHERE done = 1")
            # Den 2.0 To-do: a priority flag (0 none, 1 low, 2 medium, 3 high) and a due day (YYYY-MM-DD).
            if "priority" not in cols:
                conn.execute("ALTER TABLE checklist_items ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "due_date" not in cols:
                conn.execute("ALTER TABLE checklist_items ADD COLUMN due_date TEXT")
            for name, decl in (("task_id", "INTEGER"), ("todo_id", "INTEGER"), ("link_state", "TEXT")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE checklist_items ADD COLUMN {name} {decl}")
            conn.commit()
        _ready = True
    return conn


def split_lines(text: str) -> list[str]:
    """One item per non-empty line, list markers stripped, each capped at MAX_LEN."""
    out = []
    for line in (text or "").splitlines():
        line = _MARKER.sub("", line, count=1).strip()
        if line:
            out.append(line[:MAX_LEN])
    return out


def list_items(project_id: Optional[int] = None) -> list[dict[str, Any]]:
    with db._lock:
        conn = _conn()
        if project_id is None:
            rows = conn.execute("SELECT * FROM checklist_items ORDER BY done ASC, position ASC, id ASC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM checklist_items WHERE project_id = ? ORDER BY done ASC, position ASC, id ASC",
                                (project_id,)).fetchall()
    return _with_links([dict(r) for r in rows])


def get_item(item_id: int) -> Optional[dict[str, Any]]:
    with db._lock:
        r = _conn().execute("SELECT * FROM checklist_items WHERE id = ?", (item_id,)).fetchone()
    return _with_links([dict(r)])[0] if r else None


# ------------------------------------------------------------------ handed to Octopus

SEND_TO = ("queue", "backlog")
# Where the linked work can stand (the link's `state`). "done" ticks the item off.
_TASK_STATES = {"queued": "queued", "running": "running", "rate_limited": "paused", "done": "done",
                "error": "error", "needs_review": "needs_review", "cancelled": "cancelled"}


def _question(summary: Optional[str]) -> str:
    """The question a run ended on: its last line (as the worker's push says it)."""
    return next((l.strip() for l in reversed((summary or "").strip().splitlines()) if l.strip()), "")[:300]


def _thread_link(conn, root_id: int) -> Optional[dict[str, Any]]:
    """How a queued item's chat stands, by its newest message - a reply moves it on."""
    msgs = conn.execute("SELECT id, status, awaiting_reply, result_summary, error_message, finished_at, created_at "
                        "FROM tasks WHERE (id = ? OR thread_id = ?) AND status != 'deleted' ORDER BY id",
                        (root_id, root_id)).fetchall()
    if not msgs:
        return None   # deleted since
    last = msgs[-1]
    state = _TASK_STATES.get(last["status"], last["status"])
    asked = ""
    if conn.execute("SELECT 1 FROM approvals a JOIN tasks x ON x.id = a.task_id WHERE a.status = 'pending' "
                    "AND (x.id = ? OR x.thread_id = ?) LIMIT 1", (root_id, root_id)).fetchone():
        state = "approval"
    elif last["status"] == "done" and last["awaiting_reply"]:
        state, asked = "waiting", _question(last["result_summary"])
    return {"state": state, "task_id": root_id, "last_id": last["id"], "turns": len(msgs), "question": asked or None,
            "error": (last["error_message"] or "")[:300] or None if state in ("error", "needs_review") else None,
            "at": last["finished_at"] or last["created_at"]}


def _with_links(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each linked item's `link` ({via, state, task_id, todo_id, question, ...}) and tick off the ones
    whose work just finished. Only a change of state does that, so unticking one by hand sticks."""
    linked = [it for it in items if it.get("task_id") or it.get("todo_id")]
    if not linked:
        return items
    changed = []
    with db._lock:
        conn = _conn()
        for it in linked:
            link = None
            if it.get("todo_id"):
                todo = conn.execute("SELECT id, project_id, status, task_id FROM todos WHERE id = ?", (it["todo_id"],)).fetchone()
                if todo:
                    link = _thread_link(conn, todo["task_id"]) if todo["task_id"] else None
                    if link is None and todo["status"] == "pending":
                        link = {"state": "backlog", "task_id": None}
                    if link is not None:
                        link.update(via="backlog", todo_id=todo["id"], project_id=todo["project_id"])
            elif it.get("task_id"):
                link = _thread_link(conn, it["task_id"])
                if link is not None:
                    link.update(via="queue", todo_id=None)
            it["link"] = link
            state = link["state"] if link else "gone"
            if state != it.get("link_state"):
                changed.append((it, state))
        if changed:
            now = db._now()
            for it, state in changed:
                if state == "done" and it.get("status") != "done":
                    conn.execute("UPDATE checklist_items SET link_state = ?, status = 'done', done = 1, done_at = ? WHERE id = ?",
                                 (state, now, it["id"]))
                    it.update(status="done", done=1, done_at=now, auto_done=True)
                else:
                    conn.execute("UPDATE checklist_items SET link_state = ? WHERE id = ?", (state, it["id"]))
                it["link_state"] = state
            conn.commit()
    return items


def send(ids: list[int], to: str, project_id: Optional[int] = None) -> dict[str, Any]:
    """Hand items to Octopus: to = "queue" (a task in its project's Up next) or "backlog" (a to-do on
    the project's Backlog). An unfiled item goes to project_id if given, else General. Items already
    done, or already handed over and not finished, are skipped (with why). Returns {sent, skipped}."""
    if to not in SEND_TO:
        raise ValueError("Send it to 'queue' or 'backlog'")
    from octopus.plan import general_project   # not at import time: octopus pulls in a lot
    sent, skipped = [], []
    for iid in dict.fromkeys(int(i) for i in ids):
        it = get_item(iid)
        if not it:
            skipped.append({"id": iid, "reason": "Not found"})
            continue
        link = it.get("link")
        if it["status"] == "done":
            skipped.append({"id": iid, "reason": "Already done"})
            continue
        if link and link["state"] not in ("done", "cancelled", "error"):
            skipped.append({"id": iid, "reason": "Already in Up next" if link["state"] != "backlog" else "Already on the backlog"})
            continue
        pid = it["project_id"] or project_id
        p = db.get_project(pid) if pid else general_project()
        if not p:
            skipped.append({"id": iid, "reason": "Project not found"})
            continue
        if to == "queue":
            if not db.has_folder(p) and not db.is_general_project(p):
                skipped.append({"id": iid, "reason": db.NO_FOLDER_HINT + f" ({p['name']})"})
                continue
            mode = db.get_project_settings(p["id"]).get("default_mode")
            task = db.add_task(p["id"], it["text"], 1 if it.get("priority") == 3 else 0, routed_by="direct",
                               route_reason="To-do: Up next", permission_mode=mode, source="todo")
            fields = {"task_id": task["id"], "todo_id": None}
        else:
            todo = db.add_todo(p["id"], it["text"])
            if it.get("priority") == 3:
                db.update_todo(todo["id"], settings={"important": True})
            fields = {"task_id": None, "todo_id": todo["id"]}
        with db._lock:
            conn = _conn()
            conn.execute("UPDATE checklist_items SET task_id = ?, todo_id = ?, link_state = NULL, project_id = ? WHERE id = ?",
                         (fields["task_id"], fields["todo_id"], it["project_id"] or (None if db.is_general_project(p) else p["id"]), iid))
            conn.commit()
        sent.append(get_item(iid))
    return {"sent": sent, "skipped": skipped}


def add_items(text: str, project_id: Optional[int] = None, priority: int = 0, due_date: Optional[str] = None,
              items: Optional[list[dict[str, Any]]] = None, first: bool = False) -> list[dict[str, Any]]:
    """Add one item per line of text - or `items` ({text, project_id, priority, due_date, done}, each
    line already parsed by the dashboard); returns the new items in order. first: put them at the top."""
    rows = [{"text": line, "project_id": project_id, "priority": priority, "due_date": due_date, "done": False}
            for line in split_lines(text)] if items is None else \
        [{**it, "text": str(it.get("text") or "").strip()[:MAX_LEN]} for it in items if str(it.get("text") or "").strip()]
    now = db._now()
    ids = []
    with db._lock:
        conn = _conn()
        if first:
            pos = conn.execute("SELECT COALESCE(MIN(position), 0) AS n FROM checklist_items").fetchone()["n"] - len(rows)
        else:
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM checklist_items").fetchone()["n"]
        for i, r in enumerate(rows):
            done = bool(r.get("done"))
            cur = conn.execute(
                "INSERT INTO checklist_items (text, done, status, done_at, project_id, position, created_at, priority, due_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["text"], 1 if done else 0, "done" if done else "todo", now if done else None, r.get("project_id"), pos + i, now,
                 _prio(r.get("priority")), _day(r.get("due_date"))))
            ids.append(cur.lastrowid)
        conn.commit()
    return [get_item(i) for i in ids]


def _prio(v: Any) -> int:
    try:
        return max(0, min(3, int(v or 0)))
    except (TypeError, ValueError):
        return 0


def _day(v: Any) -> Optional[str]:
    v = str(v or "").strip()
    return v if re.match(r"^\d{4}-\d{2}-\d{2}$", v) else None


def update_item(item_id: int, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """status wins over done; done=True / False alone means "done" / "todo"."""
    sets, args = [], []
    if "text" in fields:
        sets.append("text = ?"); args.append(str(fields["text"]).strip()[:MAX_LEN])
    if "project_id" in fields:
        sets.append("project_id = ?"); args.append(fields["project_id"])
    if "priority" in fields:
        sets.append("priority = ?"); args.append(_prio(fields["priority"]))
    if "due_date" in fields:
        sets.append("due_date = ?"); args.append(_day(fields["due_date"]))
    status = fields.get("status") or (("done" if fields["done"] else "todo") if "done" in fields else None)
    if status:
        if status not in STATUSES:
            raise ValueError(f"Unknown status: {status}")
        sets += ["status = ?", "done = ?", "done_at = ?"]
        args += [status, 1 if status == "done" else 0, db._now() if status == "done" else None]
    if sets:
        with db._lock:
            conn = _conn()
            conn.execute(f"UPDATE checklist_items SET {', '.join(sets)} WHERE id = ?", args + [item_id])
            conn.commit()
    return get_item(item_id)


def delete_item(item_id: int) -> None:
    with db._lock:
        conn = _conn()
        conn.execute("DELETE FROM checklist_items WHERE id = ?", (item_id,))
        conn.commit()


def clear_done(project_id: Optional[int] = None) -> int:
    """Delete every ticked item (of one project, or of all); returns how many."""
    with db._lock:
        conn = _conn()
        if project_id is None:
            cur = conn.execute("DELETE FROM checklist_items WHERE done = 1")
        else:
            cur = conn.execute("DELETE FROM checklist_items WHERE done = 1 AND project_id = ?", (project_id,))
        conn.commit()
        return cur.rowcount


_APPLY_FIELDS = ("text", "status", "project_id", "position", "created_at", "done_at", "priority", "due_date",
                 "task_id", "todo_id", "link_state")


def apply(upsert: list[dict[str, Any]], delete: list[int]) -> None:
    """Put items back the way a snapshot had them (undo / redo, drag to reorder).

    Each upsert has an id plus any of _APPLY_FIELDS: an existing item gets just those fields,
    a missing one (deleted since) is put back under the same id. A project that has gone since
    leaves the item unfiled. All or nothing.
    """
    now = db._now()
    for it in upsert:
        if "status" in it and it["status"] not in STATUSES:
            raise ValueError(f"Unknown status: {it['status']}")
        if "text" in it and not str(it["text"] or "").strip():
            raise ValueError("Text can't be empty")
    with db._lock:
        conn = _conn()
        projects = {r["id"] for r in conn.execute("SELECT id FROM projects")}
        try:
            for it in upsert:
                iid = int(it["id"])
                f = {k: it[k] for k in _APPLY_FIELDS if k in it}
                if "text" in f:
                    f["text"] = str(f["text"]).strip()[:MAX_LEN]
                if f.get("project_id") is not None and f["project_id"] not in projects:
                    f["project_id"] = None
                exists = conn.execute("SELECT 1 FROM checklist_items WHERE id = ?", (iid,)).fetchone()
                if not exists:
                    if not f.get("text"):
                        continue   # nothing to rebuild it from
                    f.setdefault("status", "todo")
                    f.setdefault("created_at", now)
                    if "position" not in f:
                        f["position"] = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM checklist_items").fetchone()["n"]
                if "status" in f:
                    f["done"] = 1 if f["status"] == "done" else 0
                    if "done_at" not in f:
                        f["done_at"] = now if f["done"] else None
                if exists:
                    if f:
                        conn.execute(f"UPDATE checklist_items SET {', '.join(k + ' = ?' for k in f)} WHERE id = ?", [*f.values(), iid])
                else:
                    f["id"] = iid
                    conn.execute(f"INSERT INTO checklist_items ({', '.join(f)}) VALUES ({', '.join('?' for _ in f)})", list(f.values()))
            for iid in delete:
                conn.execute("DELETE FROM checklist_items WHERE id = ?", (int(iid),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
