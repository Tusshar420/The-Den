"""
Email from the Den: restarts and project changes go to your inbox, sent through the Google
account already connected under Personal > Connections (the default email account there, the
same Gmail the assistant uses - no extra password or setup). Wording lives in email_templates.py (fixed templates - no model
is ever asked to write an email).

Sending never raises and never blocks the caller: it runs on a background thread, and a
failure (Google not connected, offline) is only logged. Events can be switched off one by one
(setting "email_events_off"); the recipient is the setting "email_to".
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

import db
import email_templates
from assistant import connections
from assistant.providers.base import ProviderError
from assistant.providers.google import GmailProvider

log = logging.getLogger("the-den.mailer")

DEFAULT_TO = "tusshartg420@gmail.com"
# Written when a restart is asked for, read by the next start to say "restart completed".
RESTART_MARK = Path(__file__).parent / "restart_requested.json"


def recipient() -> str:
    return (db.get_setting("email_to") or DEFAULT_TO).strip()


def sender() -> Optional[str]:
    """The Google account mail goes out from: the default email account, or the next one
    if that needs signing in again. None when none can send."""
    for conn_id in connections.connected_with("email"):
        row = db.get_connection(conn_id)
        if (row and row["provider"] == "google" and row["status"] == connections.CONNECTED
                and any(s.endswith("gmail.compose") for s in row["scopes"])):
            return conn_id
    return None


def configured() -> bool:
    """True when a Google account is connected with permission to send."""
    return sender() is not None


def enabled(event: str) -> bool:
    return event not in (db.get_setting("email_events_off") or [])


def _deliver(event: str, fields: dict[str, Any]) -> bool:
    subject, plain, page = email_templates.render(event, fields)
    try:
        GmailProvider(sender() or "google").send([recipient()], f"[The Den] {subject}", plain, html=page)
    except ProviderError as e:  # a mail problem must never touch what triggered it
        log.warning("Email '%s' not sent: %s", event, e)
        return False
    except Exception:
        log.exception("Email '%s' not sent", event)
        return False
    log.info("Email '%s' sent to %s", event, recipient())
    return True


def send(event: str, wait: bool = False, force: bool = False, **fields: Any) -> bool:
    """Email `event` (a key of email_templates.EVENTS). Returns True when it was sent (with
    `wait`) or handed to a background thread (without). `force` skips the per-event switch
    (the test email)."""
    if event not in email_templates.EVENTS:
        raise KeyError(event)
    if not force and not enabled(event):
        return False
    if not configured():
        return False
    if wait:
        return _deliver(event, fields)
    threading.Thread(target=_deliver, args=(event, fields), daemon=True, name="den-mail").start()
    return True


# ------------------------------------------------------------------ restarts

def note_restart_requested(hard: bool, busy: list[int]) -> None:
    """A restart was asked for: remember it for the next start, and say so."""
    try:
        RESTART_MARK.write_text(json.dumps({"at": db._now(), "hard": hard}))
    except OSError:
        pass
    if hard:
        detail = "Stopping what is running now and restarting within seconds."
    elif busy:
        detail = f"Waiting for {len(busy)} running task(s) to finish ({', '.join(f'#{i}' for i in busy)}), then restarting."
    else:
        detail = "Nothing is running, so it restarts right away."
    send("restart_requested", mode="hard" if hard else "normal", detail=detail)


def on_started(started_at: str, released: int = 0) -> None:
    """Call once after a start: "restart completed" if one was asked for, else the service came
    back by itself (crash / reboot)."""
    try:
        mark = json.loads(RESTART_MARK.read_text())
        RESTART_MARK.unlink()
    except (OSError, ValueError):
        mark = None
    note = f"{released} held task(s) were let go again." if released else ""
    if mark:
        send("restart_completed", started_at=started_at, released=note,
             took=f" (asked at {mark.get('at', '')[:19].replace('T', ' ')})" if mark.get("at") else "")
    else:
        send("unexpected_restart", started_at=started_at)


# ------------------------------------------------------- "needs a restart"

_ROOTS = (".", "octopus", "assistant", "alexa")
QUIET_SECONDS = 600   # code must stop changing this long first, so an editing session sends one email


def _changed_sources(since_ts: float) -> list[tuple[float, str]]:
    base = Path(__file__).parent
    found = []
    for root in _ROOTS:
        for path in (base / root).glob("*.py"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > since_ts:
                found.append((mtime, path.relative_to(base).as_posix()))
    return sorted(found, reverse=True)


_notified_mtime = 0.0


def check_needs_restart(started_ts: float, now_ts: float) -> bool:
    """Scheduler hook: email once per batch of code changes made after the service started."""
    global _notified_mtime
    changed = _changed_sources(started_ts)
    if not changed:
        return False
    newest = changed[0][0]
    if newest <= _notified_mtime or now_ts - newest < QUIET_SECONDS:
        return False
    names = [n for _, n in changed]
    shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
    if send("restart_needed", changed=shown):
        _notified_mtime = newest
        return True
    return False


# ------------------------------------------------------------------ projects

def project_event(event: str, project: dict[str, Any]) -> None:
    send(event, name=project.get("name", ""), category=project.get("category", ""),
         folder=project.get("path") or "none (CRM-only)")

