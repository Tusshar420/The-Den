"""
Sharing a chat (Den 2.1 Message center › Share) - view-only for now, by the owner's decision.

- Members: the people a chat was shared with (name or email + role). Only "view" is allowed today; "chat"
  (a guest messaging Octopus, which runs Claude Code on this Mac) needs a guest login and an approval step
  that don't exist yet, so it's refused here, not just hidden in the page.
- Link: a random, unguessable token that opens a read-only copy of that one chat at /share/c/{token} -
  the only route here without the API key. Turning the link off deletes the token, so old links stop
  working at once. Private notes and the run steps are never shown; everything is HTML-escaped.
- Tailscale Funnel requests never reach it (api.funnel_only_reaches_alexa), so a link works on this
  Mac's own network / tailnet, not on the open internet.
"""

import html
import json
import secrets
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import db

ROLES = ("view",)            # "chat" comes later, with guest logins
_ready = False


def _init() -> None:
    global _ready
    if _ready:
        return
    with db._lock:
        conn = db.get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS chat_shares (root_id INTEGER PRIMARY KEY, members TEXT NOT NULL DEFAULT '[]', "
                     "token TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.commit()
    _ready = True


def _root(task_id: int) -> dict[str, Any]:
    t = db.get_task(task_id)
    if not t or t["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Chat not found")
    return db.get_task(t["thread_id"]) if t.get("thread_id") else t


def _row(root_id: int) -> dict[str, Any]:
    _init()
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM chat_shares WHERE root_id = ?", (root_id,)).fetchone()
    return dict(r) if r else {"root_id": root_id, "members": "[]", "token": None}


def _save(root_id: int, members: list[dict[str, Any]], token: Optional[str]) -> None:
    now = db._now()
    with db._lock:
        conn = db.get_conn()
        conn.execute("INSERT INTO chat_shares (root_id, members, token, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(root_id) DO UPDATE SET members = excluded.members, token = excluded.token, updated_at = excluded.updated_at",
                     (root_id, json.dumps(members), token, now, now))
        conn.commit()


def view(task_id: int) -> dict[str, Any]:
    root = _root(task_id)
    r = _row(root["id"])
    return {"root_id": root["id"], "members": json.loads(r["members"]), "link": bool(r["token"]),
            "path": f"/share/c/{r['token']}" if r["token"] else None, "roles": list(ROLES)}


def all_shared() -> dict[str, int]:
    """root id -> number of members, for the list's "shared" mark."""
    _init()
    with db._lock:
        rows = db.get_conn().execute("SELECT root_id, members, token FROM chat_shares").fetchall()
    return {str(r["root_id"]): len(json.loads(r["members"])) + (1 if r["token"] else 0) for r in rows if r["token"] or r["members"] != "[]"}


class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = "view"


def add_member(task_id: int, body: MemberIn) -> dict[str, Any]:
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail="Only view access can be shared for now")
    root = _root(task_id)
    r = _row(root["id"])
    members = json.loads(r["members"])
    name = " ".join(body.name.split())
    if any(m["name"].lower() == name.lower() for m in members):
        raise HTTPException(status_code=409, detail="Already in this chat")
    members.append({"name": name, "role": body.role, "added_at": db._now()})
    _save(root["id"], members, r["token"])
    return view(root["id"])


def remove_member(task_id: int, name: str) -> dict[str, Any]:
    root = _root(task_id)
    r = _row(root["id"])
    _save(root["id"], [m for m in json.loads(r["members"]) if m["name"] != name], r["token"])
    return view(root["id"])


def set_link(task_id: int, on: bool) -> dict[str, Any]:
    root = _root(task_id)
    r = _row(root["id"])
    _save(root["id"], json.loads(r["members"]), (r["token"] or secrets.token_urlsafe(24)) if on else None)
    return view(root["id"])


def page(token: str) -> str:
    """The read-only copy of a shared chat."""
    _init()
    with db._lock:
        r = db.get_conn().execute("SELECT root_id FROM chat_shares WHERE token = ?", (token,)).fetchone() if token else None
    root = db.get_task(r["root_id"]) if r else None
    if not root or root["status"] == "deleted":
        raise HTTPException(status_code=404, detail="This link was turned off")
    p = db.get_project(root["project_id"]) or {}
    esc = lambda s: html.escape(str(s or ""))
    bubbles = []
    for t in db.get_thread(root["id"]):
        if t["status"] == "deleted":
            continue
        bubbles.append(f'<div class="you"><div class="who">Tushar · {esc((t.get("created_at") or "")[:16].replace("T", " "))}</div>{esc(t.get("prompt"))}</div>')
        reply = t.get("result_summary") or (t.get("error_message") if t["status"] in ("error", "needs_review") else None)
        if reply:
            bubbles.append(f'<div class="bot"><div class="who">Octopus</div>{esc(reply)}</div>')
        elif t["status"] in ("queued", "running"):
            bubbles.append('<div class="note">Octopus is still working on this…</div>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{esc(root.get('title') or 'Shared chat')} · The Den</title>
<style>
 body {{ margin:0; background:#0A0E13; color:#E8EEF3; font-family:'Bricolage Grotesque', system-ui, -apple-system, sans-serif; }}
 main {{ max-width:760px; margin:0 auto; padding:28px 16px 60px; display:flex; flex-direction:column; gap:12px; }}
 .eyebrow {{ font-family:'JetBrains Mono', monospace; font-size:10.5px; letter-spacing:.12em; color:#6B7A8A; }}
 h1 {{ margin:0 0 8px; font-size:22px; font-weight:750; }}
 .you, .bot {{ max-width:85%; padding:11px 14px; border-radius:16px; font-size:14.5px; line-height:1.6; white-space:pre-wrap; word-break:break-word; }}
 .you {{ align-self:flex-end; background:rgba(255,123,92,0.14); border:1px solid rgba(255,123,92,0.28); }}
 .bot {{ align-self:flex-start; background:rgba(20,28,38,0.7); border:1px solid rgba(255,255,255,0.08); }}
 .who {{ font-size:11.5px; font-weight:700; color:#93A1B0; margin-bottom:4px; white-space:normal; }}
 .note {{ font-size:12.5px; color:#93A1B0; }}
 footer {{ margin-top:18px; font-size:11.5px; color:#6B7A8A; }}
</style></head><body><main>
<div class="eyebrow">SHARED CHAT · VIEW ONLY · {esc((p.get('name') or 'General').upper())}</div>
<h1>{esc(root.get('title') or 'Chat #' + str(root['id']))}</h1>
{''.join(bubbles)}
<footer>Shared from The Den. Read-only - you can't reply here. The owner can turn this link off at any time.</footer>
</main></body></html>"""


class LinkIn(BaseModel):
    on: bool


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter()
    private = [Depends(require_api_key)]

    r.get("/den/shares", dependencies=private)(all_shared)

    @r.get("/den/chats/{task_id}/share", dependencies=private)
    def share_view(task_id: int):
        return view(task_id)

    @r.post("/den/chats/{task_id}/share/members", dependencies=private)
    def share_add(task_id: int, body: MemberIn):
        return add_member(task_id, body)

    @r.delete("/den/chats/{task_id}/share/members/{name}", dependencies=private)
    def share_remove(task_id: int, name: str):
        return remove_member(task_id, name)

    @r.put("/den/chats/{task_id}/share/link", dependencies=private)
    def share_link(task_id: int, body: LinkIn):
        return set_link(task_id, body.on)

    # The one route without the API key: the token is the permission, and it only reads one chat.
    @r.get("/share/c/{token}", response_class=HTMLResponse)
    def shared_page(token: str):
        return HTMLResponse(page(token), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex",
                                                  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    return r
