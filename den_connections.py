"""
The Connections page of The Den 2.0: what the Den is really hooked up to (Google / Microsoft
accounts, GitHub, Tailscale, phones with push), the smart devices that can drive it, and your
own spoken commands.

Spoken commands ({id, phrase, action, on, confirm} in settings.voice_commands) are checked by
/voice/command before a request goes to Octopus (see run_command). Their actions are the safe
built-ins below - nothing that approves, pushes or deletes.
"""

import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db
from assistant import connections

ACTIONS = {
    "queue": "Read queue summary",
    "pause": "Pause the queue",
    "resume": "Resume the queue",
    "calendar": "Read today's calendar",
    "sleep": "Sleep mode",
    "wake": "End sleep mode",
}
NIGHT_OK = {"pause", "sleep"}      # what still works in voice quiet hours
QUIET_FROM, QUIET_TO = 23, 7
CLI_CACHE_SECONDS = 300

_cli: dict[str, tuple[float, Any]] = {}
_cli_lock = threading.Lock()


def _cached(name: str, fn: Callable[[], Any]) -> Any:
    with _cli_lock:
        hit = _cli.get(name)
        if hit and time.time() - hit[0] < CLI_CACHE_SECONDS:
            return hit[1]
    value = fn()
    with _cli_lock:
        _cli[name] = (time.time(), value)
    return value


def _run(args: list[str], timeout: float = 5) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------ services

def _tailscale() -> dict[str, Any]:
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    out = _run([exe, "status", "--json"])
    if not out or out.returncode != 0:
        return {"id": "tailscale", "name": "Tailscale", "on": False, "sub": "Not running on this Mac",
                "desc": "Reach the Den from anywhere without opening ports."}
    try:
        d = json.loads(out.stdout)
    except ValueError:
        d = {}
    me = d.get("Self") or {}
    ips = me.get("TailscaleIPs") or []
    running = d.get("BackendState") == "Running"
    host = (me.get("DNSName") or "").rstrip(".") or me.get("HostName") or ""
    return {"id": "tailscale", "name": "Tailscale", "on": running,
            "sub": " · ".join(x for x in (host.split(".")[0], ips[0] if ips else "") if x) if running else (d.get("BackendState") or "Stopped"),
            "desc": "Reach the Den from anywhere without opening ports.", "url": f"http://{ips[0]}:8787" if ips and running else None}


def _github() -> dict[str, Any]:
    desc = "Branches, CI status and pull requests. Pushes always ask first."
    if not shutil.which("gh"):
        name = _run(["git", "config", "--global", "user.name"])
        who = (name.stdout.strip() if name and name.returncode == 0 else "")
        return {"id": "github", "name": "GitHub", "on": bool(who), "sub": (f"git as {who}" if who else "Not set up") + " · GitHub CLI not installed",
                "desc": desc, "hint": "brew install gh, then gh auth login"}
    out = _run(["gh", "auth", "status", "--hostname", "github.com"])
    text = ((out.stdout if out else "") + (out.stderr if out else ""))
    m = re.search(r"account (\S+)", text) or re.search(r"as (\S+)", text)
    ok = bool(out and out.returncode == 0)
    return {"id": "github", "name": "GitHub", "on": ok, "sub": (m.group(1) if m and ok else "Signed out"), "desc": desc,
            "hint": "" if ok else "Run gh auth login in Terminal"}


def _oauth() -> list[dict[str, Any]]:
    out = []
    for st in connections.list_status():
        accounts = [a for a in st.get("accounts") or [] if a["status"] in (connections.CONNECTED, connections.REAUTH)]
        for service, name, desc in (("email", "Gmail" if st["id"] == "google" else "Outlook mail",
                                     "Reads mail for the inbox and drafts replies. Sends only when you press Send or approve."),
                                    ("calendar", "Google Calendar" if st["id"] == "google" else "Outlook calendar",
                                     "Meetings, deadlines and the morning briefing."),
                                    ("drive", "Google Drive", "Keeps a copy of each database backup in a Drive folder (only files the Den creates).")):
            if service not in (st.get("services") or []):
                continue
            mine = [a for a in accounts if service in a["services_granted"]]
            reauth = next((a for a in mine if a["status"] == connections.REAUTH), None)
            out.append({"id": f"{st['id']}:{service}", "provider": st["id"], "service": service, "name": name, "desc": desc,
                        "on": any(a["status"] == connections.CONNECTED for a in mine), "sub": ", ".join(a["account"] for a in mine) if mine else ("Not connected" if st.get("configured") else "Needs setting up first"),
                        "warn": "Sign in again - access has lapsed" if reauth else "", "reauth": reauth["id"] if reauth else None,
                        "accounts": [{"id": a["id"], "account": a["account"], "default": service in (a.get("default_for") or [])} for a in mine],
                        "can_add": bool(st.get("can_add")), "available": st.get("status") != connections.UNAVAILABLE,
                        "note": st.get("note") or ("" if st.get("configured") else "Add the client id and secret first: " + " and ".join(st.get("env") or []))})
    return out


NOT_BUILT = [
    ("telegram", "Telegram", "Approve, deny and ask Octopus from your phone."),
    ("slack", "Slack", "Post task results to a channel, reply to threads."),
    ("whatsapp", "WhatsApp", "Morning briefing and approvals over WhatsApp."),
]


def services() -> list[dict[str, Any]]:
    items = _oauth()
    items.append(_cached("github", _github))
    items.append(_cached("tailscale", _tailscale))
    subs = db.list_push_subscriptions()
    items.append({"id": "push", "name": "Push notifications", "on": bool(subs), "sub": f"{len(subs)} device(s)" if subs else "No device yet",
                  "desc": "Approvals and results on your phone, with Approve / Deny on the notification."})
    for sid, name, desc in NOT_BUILT:
        items.append({"id": sid, "name": name, "desc": desc, "on": False, "sub": "Not built yet", "soon": True})
    return items


# ------------------------------------------------------------------ devices

def _device_name(ua: str) -> str:
    ua = ua or ""
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"), ("Windows", "Windows PC")):
        if key in ua:
            return name
    return "Browser"


def devices() -> list[dict[str, Any]]:
    out = []
    try:
        from alexa.api import status as alexa_status
        a = alexa_status()
        ready = a["skill_id_set"]
        out.append({"id": "alexa", "name": "Amazon Alexa", "on": bool(a["paired"]), "ready": ready,
                    "status": ("Paired" + (f" · last used {a['last_request_at'][:16].replace('T', ' ')}" if a.get("last_request_at") else "")) if a["paired"]
                    else ("Waiting for the first request from your Echo" if ready else "Skill not set up"),
                    "desc": "Ask about the queue, hear results and approve with your code on Echo devices.",
                    "chips": [c for c, on in (("Approvals", a["prefs"]["notify_approvals"]), ("Results", a["prefs"]["notify_results"]),
                                              ("Approval code", a["prefs"]["approval_pin_set"])) if on],
                    "prefs": a["prefs"], "last": a.get("last_request_at")})
    except Exception:
        pass
    subs = db.list_push_subscriptions()
    names = [_device_name(s.get("user_agent") or "") for s in subs]
    out.append({"id": "push", "name": "Phones & browsers", "on": bool(subs), "ready": True,
                "status": f"{len(subs)} getting notifications" if subs else "Open the Den on your phone and allow notifications",
                "desc": "Approve or deny right from the notification. Add the Den to your home screen for the full app.",
                "chips": sorted(set(names))})
    out.append({"id": "siri", "name": "Siri & Shortcuts", "on": True, "ready": True, "status": "Works with your API key",
                "desc": "Run Den commands from iPhone, Mac and HomePod with a Shortcut that calls the links below.", "chips": ["iPhone", "Mac"]})
    for did, name, desc in (("ghome", "Google Home", "Link with Google Assistant for Nest speakers and Android."),
                            ("homekit", "HomeKit scenes", "“Going to sleep” dims lights and pauses Octopus."),
                            ("deck", "Stream Deck", "Physical buttons: approve, pause queue, open Den."),
                            ("watch", "Apple Watch", "Approval taps and queue complications on your wrist.")):
        out.append({"id": did, "name": name, "on": False, "ready": False, "status": "Not built yet", "desc": desc, "chips": [], "soon": True})
    return out


# ------------------------------------------------------------------ spoken commands

class CommandIn(BaseModel):
    phrase: str = Field(min_length=2, max_length=80)
    action: str
    on: bool = True
    confirm: bool = False


class CommandPatch(BaseModel):
    phrase: Optional[str] = Field(None, min_length=2, max_length=80)
    action: Optional[str] = None
    on: Optional[bool] = None
    confirm: Optional[bool] = None


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", (text or "").lower()).split())


def _check_action(action: str) -> str:
    if action in ACTIONS or (action.startswith("recipe:") and action[7:]):
        return action
    raise HTTPException(status_code=400, detail="Unknown action")


def commands() -> list[dict[str, Any]]:
    return list(db.get_setting("voice_commands") or [])


def _save(cmds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    db.set_setting("voice_commands", cmds)
    return cmds


def action_label(action: str) -> str:
    if action.startswith("recipe:"):
        try:
            import den_recipes
            r = next((x for x in den_recipes.list_all() if str(x.get("id")) == action[7:]), None)
            return "Run recipe · " + (r["name"] if r else action[7:])
        except Exception:
            return "Run recipe"
    return ACTIONS.get(action, action)


def _quiet_now() -> bool:
    h = datetime.now().hour
    return bool(db.get_setting("voice_quiet")) and (h >= QUIET_FROM or h < QUIET_TO)


def _queue_text() -> str:
    try:
        from alexa.skill import queue_summary
        return queue_summary()
    except Exception:
        return f"{len(db.list_tasks(statuses=['running']))} running and {len(db.list_tasks(statuses=['queued']))} waiting."


def _calendar_text() -> str:
    try:
        from assistant import providers, timeutil
        from datetime import timedelta
        now = timeutil.now()
        start = timeutil.at(now.date(), tz=now.tzinfo)
        events = providers.calendar().list_events(start, start + timedelta(days=1), max_results=20)
    except Exception:
        return "I couldn't read your calendar."
    if not events:
        return "Nothing on your calendar today."
    parts = []
    for e in events[:6]:
        when = "all day" if e.get("all_day") else (e.get("start") or "")[11:16]
        parts.append(f"{when} {e.get('title') or 'busy'}")
    return f"{len(events)} today: " + "; ".join(parts) + "."


def perform(action: str) -> str:
    """Do one command action; returns what to say back."""
    if action == "queue":
        return _queue_text()
    if action == "pause":
        db.set_setting("queue_paused", True)
        return "Paused. Nothing new starts until you say resume. Running work finishes."
    if action == "resume":
        db.set_setting("queue_paused", False)
        return "Resumed. Octopus is picking up the queue again."
    if action == "calendar":
        return _calendar_text()
    if action == "sleep":
        db.start_sleep_mode(8)
        return "Good night. I'll keep working through the queue for eight hours."
    if action == "wake":
        db.end_sleep_mode()
        return "Good morning. Sleep mode is off."
    if action.startswith("recipe:"):
        import den_recipes
        den_recipes.run_now(action[7:])
        return "Started " + action_label(action)[len("Run recipe · "):] + "."
    raise HTTPException(status_code=400, detail="Unknown action")


def run_command(text: str, confirmed: bool = False) -> Optional[dict[str, Any]]:
    """If `text` is one of your spoken commands, run it and return the reply; else None (it goes to Octopus).
    A command marked "confirm" (or every command when read-back is on) first answers with what it will do."""
    said = _norm(text)
    wake = _norm(db.get_setting("voice_wake") or "")
    if wake and (said == wake or said.startswith(wake + " ")):
        said = said[len(wake):].strip()
    if not said:
        return None
    for c in commands():
        if not c.get("on") or _norm(c.get("phrase", "")) != said:
            continue
        if _quiet_now() and c["action"] not in NIGHT_OK:
            return {"command": c["id"], "ran": False, "spoken": "It's quiet hours, so only pause and going to sleep work now."}
        if (c.get("confirm") or db.get_setting("voice_readback")) and not confirmed:
            return {"command": c["id"], "ran": False, "confirm": True,
                    "spoken": f"That will {action_label(c['action']).lower()}. Say it again with yes to go ahead."}
        spoken = perform(c["action"])
        db.add_activity("voice", c["action"], c.get("phrase"))
        return {"command": c["id"], "ran": True, "spoken": spoken}
    return None


def confirm_prefix(text: str) -> tuple[str, bool]:
    """"yes, pause octopus" -> ("pause octopus", True)."""
    m = re.match(r"^\s*(yes|yeah|yep|confirm|go ahead)[\s,.!]+(.+)$", text or "", re.I)
    return (m.group(2), True) if m else (text, False)


def log(limit: int = 12) -> list[dict[str, Any]]:
    rows = db.list_activity(limit, "voice")
    return [{"at": r["created_at"], "text": f"“{r['target'] or ''}” → {action_label(r['action'])}", "dev": "Voice"} for r in rows]


def overview() -> dict[str, Any]:
    cmds = [{**c, "label": action_label(c["action"])} for c in commands()]
    try:
        import den_recipes
        recipes = [{"v": f"recipe:{r['id']}", "l": "Run recipe · " + r["name"]} for r in den_recipes.list_all()]
    except Exception:
        recipes = []
    return {"services": services(), "devices": devices(), "commands": cmds,
            "actions": [{"v": k, "l": v} for k, v in ACTIONS.items()] + recipes,
            "queue_paused": bool(db.get_setting("queue_paused")), "voice_quiet": bool(db.get_setting("voice_quiet")),
            "voice_readback": bool(db.get_setting("voice_readback")), "log": log(), "used_today": _used_today()}


def _used_today() -> int:
    today = datetime.now().astimezone().date().isoformat()
    return sum(1 for r in db.list_activity(200, "voice") if (r["created_at"] or "")[:10] == today)


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/connections")
    def den_connections(refresh: bool = False):
        if refresh:
            with _cli_lock:
                _cli.clear()
        return overview()

    @r.post("/den/voice-commands")
    def add_command(body: CommandIn):
        cmds = commands()
        if any(_norm(c["phrase"]) == _norm(body.phrase) for c in cmds):
            raise HTTPException(status_code=409, detail="There's already a command with that phrase")
        new = {"id": max([c["id"] for c in cmds] or [0]) + 1, "phrase": body.phrase.strip(), "action": _check_action(body.action),
               "on": body.on, "confirm": body.confirm}
        return _save(cmds + [new])

    @r.patch("/den/voice-commands/{cmd_id}")
    def edit_command(cmd_id: int, body: CommandPatch):
        cmds = commands()
        c = next((x for x in cmds if x["id"] == cmd_id), None)
        if not c:
            raise HTTPException(status_code=404, detail="No such command")
        ch = body.model_dump(exclude_none=True)
        if "action" in ch:
            _check_action(ch["action"])
        c.update(ch)
        return _save(cmds)

    @r.delete("/den/voice-commands/{cmd_id}")
    def delete_command(cmd_id: int):
        return _save([c for c in commands() if c["id"] != cmd_id])

    @r.post("/den/voice-commands/{cmd_id}/test")
    def test_command(cmd_id: int):
        c = next((x for x in commands() if x["id"] == cmd_id), None)
        if not c:
            raise HTTPException(status_code=404, detail="No such command")
        spoken = perform(c["action"])
        db.add_activity("voice", c["action"], c.get("phrase"))
        return {"spoken": spoken}

    return r
