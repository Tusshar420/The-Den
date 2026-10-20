"""
Reaching Tushar on Alexa when he isn't looking at The Den.

Every CHECK_SECONDS the loop looks for:
    - approvals waiting on a human (the same ones the dashboard's "Needs you" shows)
    - tasks he queued from Alexa that have finished (state.watch_task)
and, if there is something he hasn't been told about AND he's away from The Den (presence.py)
AND he isn't mid-conversation with the skill, sends one Alexa notification through the
Proactive Events API: the yellow ring / chime, and "You have 2 new unread messages from Octopus"
when he asks Alexa for his notifications. "Alexa, open my octopus" then reads each one out and
takes the yes / no (alexa/skill.py).

Alexa only allows fixed notification templates (AMAZON.MessageAlert.Activated here) - the details
are never sent to Amazon's notification service, only a count.

Needs ALEXA_CLIENT_ID / ALEXA_CLIENT_SECRET (developer console > your skill > Permissions >
Alexa Skill Messaging) and the notification permission switched on for the skill in the Alexa app.
"""

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import db
import presence
from alexa import state

log = logging.getLogger("the-den.alexa")

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
DEFAULT_API_ENDPOINT = "https://api.amazonalexa.com"
CHECK_SECONDS = 15
RETRY_AFTER_FAIL_SECONDS = 300
IN_SESSION_SECONDS = 45          # he spoke to the skill this recently: it tells him itself, no notification
CREATOR = "Octopus"              # "... messages from Octopus"

_token: tuple[str, float] = ("", 0.0)


class NotifyError(Exception):
    pass


def stage() -> str:
    """"development" until the skill is certified and live (then set ALEXA_STAGE=live)."""
    return "live" if os.environ.get("ALEXA_STAGE", "").strip().lower() == "live" else "development"


def configured() -> tuple[bool, str]:
    cid, secret = state.client_credentials()
    if not cid or not secret:
        return False, "ALEXA_CLIENT_ID / ALEXA_CLIENT_SECRET aren't set in the service's environment"
    st = state.get()
    if not st.get("user_id"):
        return False, "the skill hasn't been used yet - say \"Alexa, open my octopus\" once so The Den knows who to notify"
    if st.get("subscribed") is False:
        return False, "notifications are switched off for the skill in the Alexa app"
    return True, ""


def _post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "the-den/1.0", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000).decode("utf-8", "replace")
    except Exception as e:
        raise NotifyError(f"couldn't reach Amazon: {e}")


def _access_token() -> str:
    global _token
    if _token[0] and time.time() < _token[1] - 60:
        return _token[0]
    cid, secret = state.client_credentials()
    status, text = _post(TOKEN_URL, urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid, "client_secret": secret,
        "scope": "alexa::proactive_events"}).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        raise NotifyError(f"Amazon refused the skill's client id / secret ({status}): {text[:200]}")
    data = json.loads(text)
    _token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return _token[0]


def event_body(count: int, user_id: str, now: Optional[datetime] = None, locale: str = "en-US") -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.00Z"
    return {
        "timestamp": now.strftime(fmt),
        "referenceId": f"the-den-{uuid.uuid4()}",
        "expiryTime": (now + timedelta(hours=12)).strftime(fmt),
        "event": {"name": "AMAZON.MessageAlert.Activated",
                  "payload": {"state": {"status": "UNREAD", "freshness": "NEW"},
                              "messageGroup": {"creator": {"name": CREATOR}, "count": max(1, count)}}},
        "localizedAttributes": [{"locale": locale}],
        "relevantAudience": {"type": "Unicast", "payload": {"user": user_id}},
    }


def send(count: int) -> None:
    ok, why = configured()
    if not ok:
        raise NotifyError(why)
    st = state.get()
    url = (st.get("api_endpoint") or DEFAULT_API_ENDPOINT).rstrip("/") + "/v1/proactiveEvents/" + \
        ("stages/development" if stage() == "development" else "")
    body = json.dumps(event_body(count, st["user_id"], locale=st.get("locale") or "en-US")).encode()
    status, text = _post(url, body, {"Content-Type": "application/json", "Authorization": f"Bearer {_access_token()}"})
    if status == 403:
        raise NotifyError("Amazon says the skill may not notify you - allow notifications for it in the Alexa app "
                          "(Skills > Your skills > the skill > Settings > Manage permissions)")
    if status not in (200, 202):
        raise NotifyError(f"Amazon rejected the notification ({status}): {text[:200]}")


# ------------------------------------------------------------------- the loop

def unheard() -> dict[str, Any]:
    """What he hasn't been told about yet, and everything still waiting on him."""
    st, p = state.get(), state.prefs()
    approvals = state.pending_approvals() if p["notify_approvals"] else []
    finished = state.finished_watched() if p["notify_results"] else []
    return {"approvals": approvals, "finished": finished,
            "new_approvals": [a["id"] for a in approvals if a["id"] not in st.get("announced", [])],
            "new_finished": [t["id"] for t in finished if t["id"] not in st.get("announced_tasks", [])]}


def _recent(iso: Optional[str], seconds: float) -> bool:
    if not iso:
        return False
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() < seconds
    except ValueError:
        return False


def check_once() -> Optional[dict[str, Any]]:
    """One pass: sends at most one notification. Returns what happened, or None when there was
    nothing to do (or it isn't the moment)."""
    if not configured()[0] or db.sleep_mode_active():
        return None
    st = state.get()
    if st.get("retry_at") and datetime.fromisoformat(st["retry_at"]) > datetime.now(timezone.utc):
        return None                    # the last attempt failed - don't hammer Amazon
    u = unheard()
    if not u["new_approvals"] and not u["new_finished"]:
        return None
    if presence.is_present() or _recent(st.get("last_request_at"), IN_SESSION_SECONDS):
        return None                    # he's looking at The Den / talking to Alexa - try again later
    count = len(u["approvals"]) + len(u["finished"])
    try:
        send(count)
    except NotifyError as e:
        log.warning("Alexa notification failed: %s", e)
        retry = (datetime.now(timezone.utc) + timedelta(seconds=RETRY_AFTER_FAIL_SECONDS)).isoformat()
        state.update(lambda s: s.update(last_notify={"at": state.now_iso(), "ok": False, "error": str(e)}, retry_at=retry))
        return {"sent": False, "error": str(e)}

    def mark(s: dict[str, Any]) -> None:
        s["announced"] = s.get("announced", []) + u["new_approvals"]
        s["announced_tasks"] = s.get("announced_tasks", []) + u["new_finished"]
        s["last_notify"] = {"at": state.now_iso(), "ok": True, "count": count}
        s.pop("retry_at", None)
    state.update(mark)
    log.info("Alexa notified: %s approval(s), %s finished task(s)", len(u["approvals"]), len(u["finished"]))
    return {"sent": True, "count": count}


async def notifier_loop(stop_event: asyncio.Event) -> None:
    log.info("Alexa notifier started (checking every %ss)", CHECK_SECONDS)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(check_once)
        except Exception:
            log.exception("Alexa notifier pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), CHECK_SECONDS)
        except asyncio.TimeoutError:
            pass
