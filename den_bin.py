"""
The Recycle bin of The Den 2.0: one list for everything that can be deleted -
tasks/chats (db.list_bin), projects (projects.deleted_at), and notes, recipes
and schedules (a snapshot in bin_items, put back as it was on restore).
Everything is erased for good after db.BIN_DAYS, or right away with Delete forever.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException

import db
import documents
import mailer
import project_images

TABLES = {"note": "pad_notes", "recipe": "recipes", "schedule": "schedules", "den_recipe": "den_recipes"}


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=db.BIN_DAYS)).isoformat()


def _days_left(deleted_at: Optional[str]) -> int:
    try:
        at = datetime.fromisoformat(deleted_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return db.BIN_DAYS
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0, db.BIN_DAYS - (datetime.now(timezone.utc) - at).days)


# ------------------------------------------------------------------ putting things in

def snapshot(kind: str, row: dict[str, Any], title: str, dest: str, preview: str) -> None:
    """Keep a row about to be deleted so it can be restored exactly."""
    with db._lock:
        conn = db.get_conn()
        conn.execute("INSERT INTO bin_items (kind, title, dest, preview, data, deleted_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (kind, title[:200] or "Untitled", dest, (preview or "")[:2000], json.dumps(row), db._now()))
        conn.commit()


def delete_project(project_id: int) -> dict[str, Any]:
    p = db.get_project(project_id)
    if not p or p.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Project not found")
    if db.is_general_project(p):
        raise HTTPException(status_code=400, detail="General can't be deleted")
    with db._lock:
        conn = db.get_conn()
        # Paused too, so its queued work waits while it's in the bin (active comes back on restore).
        conn.execute("UPDATE projects SET deleted_at = ?, active = 0 WHERE id = ?", (db._now(), project_id))
        conn.commit()
    return {"ok": True, "id": project_id, "keep_days": db.BIN_DAYS}


# ------------------------------------------------------------------ the list

def _erase_project(p: dict[str, Any]) -> None:
    for doc in db.list_project_documents(p["id"], with_stored=True):
        documents.remove(doc)
    project_images.remove(p.get("image"))
    db.delete_project(p["id"])
    mailer.project_event("project_deleted", p)


def purge_expired() -> None:
    cut = _cutoff()
    with db._lock:
        conn = db.get_conn()
        old = [db._row_to_dict(r) for r in conn.execute("SELECT * FROM projects WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cut,)).fetchall()]
        conn.execute("DELETE FROM bin_items WHERE deleted_at < ?", (cut,))
        conn.commit()
    for p in old:
        _erase_project(p)


def list_bin() -> dict[str, Any]:
    purge_expired()
    items: list[dict[str, Any]] = []
    for t in db.list_bin():
        is_chat = (t.get("replies") or 0) > 0
        items.append({"kind": "task", "type": "Chat" if is_chat else "Task", "id": t["id"], "title": f"#{t['id']} {t.get('title') or (t.get('prompt') or '')[:70]}",
                      "deleted_at": t.get("deleted_at"), "dest": f"{t.get('project_name') or 'General'} · {'Chats' if is_chat else 'Up next' if t.get('deleted_from') == 'queued' else 'History'}",
                      "preview": (t.get("prompt") or "")[:1500] + (f"\n\n{t['replies']} more message(s) in this chat." if is_chat else "")})
    with db._lock:
        conn = db.get_conn()
        projects = [db._row_to_dict(r) for r in conn.execute("SELECT * FROM projects WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC").fetchall()]
        counts = {r["project_id"]: (r["chats"], r["done"]) for r in conn.execute(
            "SELECT project_id, SUM(thread_id IS NULL) AS chats, SUM(status = 'done') AS done FROM tasks GROUP BY project_id").fetchall()}
        rows = [db._row_to_dict(r) for r in conn.execute("SELECT * FROM bin_items ORDER BY deleted_at DESC").fetchall()]
    for p in projects:
        chats, done = counts.get(p["id"], (0, 0))
        items.append({"kind": "project", "type": "Project", "id": p["id"], "title": p["name"], "deleted_at": p["deleted_at"], "dest": "Projects",
                      "preview": f"{p.get('path') or 'no folder'} · {chats or 0} chats · {done or 0} tasks done.\nFiles on disk were never touched."})
    for r in rows:
        items.append({"kind": r["kind"], "type": {"note": "Note", "recipe": "Recipe", "schedule": "Recipe", "den_recipe": "Recipe"}.get(r["kind"], "Recipe"), "id": r["id"],
                      "title": r["title"], "deleted_at": r["deleted_at"], "dest": r["dest"] or "", "preview": r["preview"] or ""})
    for it in items:
        it["days_left"] = _days_left(it["deleted_at"])
    items.sort(key=lambda x: x.get("deleted_at") or "", reverse=True)
    return {"items": items, "keep_days": db.BIN_DAYS}


# ------------------------------------------------------------------ restore / erase

def restore(kind: str, item_id: int) -> dict[str, Any]:
    if kind == "task":
        ids = db.restore_task(item_id)
        if not ids:
            raise HTTPException(status_code=404, detail="Not in the recycle bin")
        return {"restored": ids}
    if kind == "project":
        with db._lock:
            conn = db.get_conn()
            cur = conn.execute("UPDATE projects SET deleted_at = NULL, active = 1 WHERE id = ? AND deleted_at IS NOT NULL", (item_id,))
            conn.commit()
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Not in the recycle bin")
        return {"restored": [item_id]}
    if kind not in TABLES:
        raise HTTPException(status_code=400, detail="Unknown kind")
    with db._lock:
        conn = db.get_conn()
        r = conn.execute("SELECT * FROM bin_items WHERE id = ? AND kind = ?", (item_id, kind)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Not in the recycle bin")
        row = json.loads(r["data"])
        table = TABLES[kind]
        cols = {c["name"] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        # Lists kept in a JSON column (a note's pictures) go back as the JSON text they were stored as.
        row = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items() if k in cols}
        if row.get("id") is not None and conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row["id"],)).fetchone():
            row.pop("id")          # its old id was reused - come back under a new one
        conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.execute("DELETE FROM bin_items WHERE id = ?", (item_id,))
        conn.commit()
    return {"restored": [item_id]}


def erase(kind: str, item_id: int) -> dict[str, Any]:
    if kind == "task":
        if not db.purge_task(item_id):
            raise HTTPException(status_code=404, detail="Not in the recycle bin")
        return {"erased": 1}
    if kind == "project":
        p = db.get_project(item_id)
        if not p or not p.get("deleted_at"):
            raise HTTPException(status_code=404, detail="Not in the recycle bin")
        _erase_project(p)
        return {"erased": 1}
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute("DELETE FROM bin_items WHERE id = ? AND kind = ?", (item_id, kind))
        conn.commit()
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="Not in the recycle bin")
    return {"erased": 1}


def empty() -> dict[str, Any]:
    n = db.purge_bin()
    with db._lock:
        conn = db.get_conn()
        projects = [db._row_to_dict(r) for r in conn.execute("SELECT * FROM projects WHERE deleted_at IS NOT NULL").fetchall()]
        n += conn.execute("DELETE FROM bin_items").rowcount
        conn.commit()
    for p in projects:
        _erase_project(p)
    return {"erased": n + len(projects)}


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])
    r.get("/den/bin")(list_bin)

    @r.post("/den/bin/{kind}/{item_id}/restore")
    def bin_restore(kind: str, item_id: int):
        return restore(kind, item_id)

    @r.delete("/den/bin/{kind}/{item_id}")
    def bin_erase(kind: str, item_id: int):
        return erase(kind, item_id)

    r.delete("/den/bin")(empty)

    @r.post("/den/projects/{project_id}/delete")
    def project_to_bin(project_id: int):
        return delete_project(project_id)

    return r
