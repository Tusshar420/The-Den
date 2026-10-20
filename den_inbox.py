"""
The Inbox of The Den 2.0: Gmail inbox mail, chats with an answer you haven't
read, and work waiting on a review - one list (approvals come from /approvals).

Gmail is connected read + compose only, so archive / snooze / read are kept
here in inbox_state, keyed "e:<gmail id>", "c:<chat root id>", "a:<approval id>".
Replies are sent from the account the message arrived on, quoting the original.
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db
import providers as llm
import router as model_router
from assistant import connections, mailutil
from assistant import providers as mail_providers

MAIL_QUERY = "in:inbox newer_than:14d"
MAIL_MAX = 20
MAIL_CACHE_SECONDS = 90
CHAT_DAYS = 7

SUMMARY_PROMPT = ("Summarize this email for its busy recipient in one or two short sentences: what the sender wants "
                  "and anything to act on, with dates. If it is routine (a receipt, a notification), say so in a few words. "
                  "Plain text, no preamble.")
DRAFT_PROMPT = ("Write a short, friendly reply to this email on behalf of its recipient, {me}. Answer what was asked; "
                "where you would need facts you don't have, say you'll check. Plain text only: the greeting, the body and "
                "a sign-off with the name {me}. No subject line, no quoted original, no preamble.")

_cache: dict[str, Any] = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()
_ready = False


def _init() -> None:
    global _ready
    if _ready:
        return
    with db._lock:
        conn = db.get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS inbox_state (key TEXT PRIMARY KEY, archived_at TEXT, snooze_until TEXT, "
                     "read_at TEXT, replied_at TEXT, summary TEXT)")
        conn.commit()
    _ready = True


def _states() -> dict[str, dict[str, Any]]:
    _init()
    with db._lock:
        rows = db.get_conn().execute("SELECT * FROM inbox_state").fetchall()
    return {r["key"]: dict(r) for r in rows}


def _set(key: str, **fields: Any) -> None:
    _init()
    cols = ", ".join(fields)
    with db._lock:
        conn = db.get_conn()
        conn.execute(f"INSERT INTO inbox_state (key, {cols}) VALUES (?, {', '.join('?' * len(fields))}) "
                     f"ON CONFLICT(key) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in fields)}",
                     [key, *fields.values()])
        conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hidden(st: Optional[dict[str, Any]]) -> bool:
    if not st:
        return False
    if st.get("archived_at"):
        return True
    until = st.get("snooze_until")
    if until:
        try:
            at = datetime.fromisoformat(until.replace("Z", "+00:00"))
            return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) > _now()
        except ValueError:
            return False
    return False


# ------------------------------------------------------------------ mail

def _mail_connected() -> bool:
    try:
        return bool(connections.connected_with("email"))
    except Exception:
        return False


def _mail(force: bool = False) -> dict[str, Any]:
    """The inbox's recent messages (metadata only), cached briefly - the page polls."""
    if not _mail_connected():
        return {"connected": False, "messages": [], "error": None}
    with _cache_lock:
        if not force and _cache["data"] is not None and time.time() - _cache["at"] < MAIL_CACHE_SECONDS:
            return _cache["data"]
    try:
        found = mail_providers.email().search(MAIL_QUERY, MAIL_MAX)
        data = {"connected": True, "messages": found, "error": None}
    except Exception as e:  # a lapsed token or network trouble shouldn't take the page down
        data = {"connected": True, "messages": [], "error": str(e)[:300]}
    with _cache_lock:
        _cache.update(at=time.time(), data=data)
    return data


def _get_mail(message_id: str) -> dict[str, Any]:
    if not _mail_connected():
        raise HTTPException(status_code=409, detail="No email account is connected")
    try:
        return mail_providers.email().get(message_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't read the message: {str(e)[:200]}")


def _ask(system: str, text: str) -> str:
    model = model_router.cheapest_api_model()
    if not model:
        raise HTTPException(status_code=503, detail="No answer-only model is available")
    try:
        out = llm.complete(model, [{"role": "user", "content": text[:12000]}], system, 45)
    except llm.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    db.log_usage("inbox", model["id"], out.input_tokens, out.output_tokens,
                 llm.estimate_cost(model, out.input_tokens, out.output_tokens))
    return out.text.strip()


def _as_text(m: dict[str, Any]) -> str:
    return (f"From: {m.get('from_name') or ''} <{m.get('from_email') or ''}>\nSubject: {m.get('subject') or ''}\n"
            f"Date: {m.get('date_text') or ''}\n\n{m.get('body') or m.get('snippet') or ''}")


# ------------------------------------------------------------------ the list

def inbox(refresh: bool = False) -> dict[str, Any]:
    states = _states()
    items: list[dict[str, Any]] = []
    mail = _mail(force=refresh)
    for m in mail["messages"]:
        key = f"e:{m['id']}"
        st = states.get(key)
        if _hidden(st):
            continue
        items.append({"key": key, "kind": "email", "id": m["id"], "from": m.get("from_name") or m.get("from_email") or "Unknown",
                      "addr": m.get("from_email") or "", "subject": m.get("subject") or "(no subject)",
                      "preview": m.get("snippet") or "", "at": m.get("date"), "account": m.get("account"),
                      "unread": bool(m.get("unread")) and not (st or {}).get("read_at"),
                      "replied": bool((st or {}).get("replied_at")), "summary": (st or {}).get("summary") or "",
                      # Mail needs you while it's unread and unanswered; the rest is for reference.
                      "needs": bool(m.get("unread")) and not (st or {}).get("read_at") and not (st or {}).get("replied_at")})

    since = (_now() - timedelta(days=CHAT_DAYS)).isoformat()
    seen: set[int] = set()

    def add_chat(c: dict[str, Any], kind: str, unread: bool) -> None:
        if c["id"] in seen or (c.get("last_at") or "") < since:
            return
        seen.add(c["id"])
        key = f"c:{c['id']}"
        if _hidden(states.get(key)):
            return
        items.append({"key": key, "kind": kind, "id": c["id"], "from": f"Octopus · {c.get('project_name') or 'General'}",
                      "addr": f"#{c['id']}", "subject": c.get("title") or f"Chat #{c['id']}",
                      "preview": (c.get("preview") or {}).get("text") or "", "at": c.get("last_at"),
                      "project_id": c.get("project_id"), "status": c.get("status"),
                      "unread": unread and not (states.get(key) or {}).get("read_at"), "needs": kind == "review"})

    for c in db.list_chats(status="needs", limit=40)["chats"]:
        # A chat waiting only on an approval shows up as that approval instead.
        if c.get("status") in ("error", "needs_review"):
            add_chat(c, "review", True)
        elif c.get("awaiting_reply"):          # Octopus asked a question and waits for the answer
            add_chat(c, "chat", True)
            items[-1]["needs"] = True if items and items[-1]["key"] == f"c:{c['id']}" else False
    for c in db.list_chats(kind="message", unread=True, limit=40)["chats"]:
        add_chat(c, "chat", True)

    items.sort(key=lambda x: x.get("at") or "", reverse=True)
    hidden_approvals = [k[2:] for k, st in states.items() if k.startswith("a:") and _hidden(st)]
    return {"items": items, "mail": {"connected": mail["connected"], "error": mail["error"]},
            "hidden_approvals": hidden_approvals}


# ------------------------------------------------------------------ one message

class SnoozeIn(BaseModel):
    until: Optional[str] = None    # ISO time; tomorrow 09:00 local when left out


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    reply_all: bool = False


def _tomorrow_nine() -> str:
    local = datetime.now().astimezone()
    return (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()


def _key(key: str) -> str:
    if len(key) < 3 or key[1] != ":" or key[0] not in "eca":
        raise HTTPException(status_code=400, detail="Unknown inbox item")
    return key


def mail_detail(message_id: str) -> dict[str, Any]:
    m = _get_mail(message_id)
    st = _states().get(f"e:{message_id}") or {}
    _set(f"e:{message_id}", read_at=st.get("read_at") or db._now())
    return {"id": m["id"], "from": m.get("from_name") or m.get("from_email"), "addr": m.get("from_email"),
            "to": m.get("to") or [], "cc": m.get("cc") or [], "subject": m.get("subject"), "at": m.get("date"),
            "date_text": m.get("date_text"), "body": m.get("body") or m.get("snippet") or "",
            "attachments": [a.get("filename") for a in m.get("attachments") or [] if a.get("filename")],
            "account": m.get("account"), "summary": st.get("summary") or "", "replied": bool(st.get("replied_at"))}


def mail_summary(message_id: str) -> dict[str, Any]:
    text = _ask(SUMMARY_PROMPT, _as_text(_get_mail(message_id)))
    _set(f"e:{message_id}", summary=text[:1500])
    return {"summary": text}


def _owner() -> str:
    try:
        return (db.get_settings().get("owner_name") or "").strip() or "Tushar"
    except Exception:
        return "Tushar"


def mail_draft(message_id: str) -> dict[str, Any]:
    return {"text": _ask(DRAFT_PROMPT.format(me=_owner()), _as_text(_get_mail(message_id)))}


def mail_reply(message_id: str, body: ReplyIn) -> dict[str, Any]:
    """The owner typed this reply and pressed Send - that press is the go-ahead."""
    provider = mail_providers.email()
    original = _get_mail(message_id)
    try:
        composed = mailutil.compose_reply(original, body.body, original.get("account") or provider.account(), body.reply_all)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        sent = provider.send(composed["to"], composed["subject"], composed["body"], composed["cc"] or None, message_id)
    except Exception as e:
        db.add_activity("email", "reply", ", ".join(composed["to"]), approval="approved", result="error", error=str(e), ref=message_id)
        raise HTTPException(status_code=502, detail=f"Couldn't send: {str(e)[:200]}")
    db.add_activity("email", "reply", ", ".join(composed["to"]), approval="approved", ref=message_id)
    _set(f"e:{message_id}", replied_at=db._now(), read_at=db._now())
    return {"ok": True, "to": composed["to"], "subject": composed["subject"], "id": (sent or {}).get("id")}


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/inbox")
    def den_inbox(refresh: bool = False):
        return inbox(refresh)

    @r.post("/den/inbox/{key}/archive")
    def archive(key: str):
        _set(_key(key), archived_at=db._now())
        return {"ok": True}

    @r.post("/den/inbox/{key}/snooze")
    def snooze(key: str, body: Optional[SnoozeIn] = None):
        until = (body.until if body else None) or _tomorrow_nine()
        try:
            datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="until must be an ISO time")
        _set(_key(key), snooze_until=until, archived_at=None)
        return {"ok": True, "until": until}

    @r.post("/den/inbox/{key}/restore")
    def restore(key: str):
        _set(_key(key), snooze_until=None, archived_at=None)
        return {"ok": True}

    @r.post("/den/inbox/{key}/read")
    def mark_read(key: str):
        _set(_key(key), read_at=db._now())
        return {"ok": True}

    @r.get("/den/inbox/mail/{message_id}")
    def detail(message_id: str):
        return mail_detail(message_id)

    @r.post("/den/inbox/mail/{message_id}/summary")
    def summary(message_id: str):
        return mail_summary(message_id)

    @r.post("/den/inbox/mail/{message_id}/draft")
    def draft(message_id: str):
        return mail_draft(message_id)

    @r.post("/den/inbox/mail/{message_id}/reply")
    def reply(message_id: str, body: ReplyIn):
        return mail_reply(message_id, body)

    return r
