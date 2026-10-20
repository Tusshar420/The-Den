"""
What The Den remembers about Alexa, in two settings rows:

    alexa        the link: which Amazon user this skill belongs to (the first one to use it,
                 or ALEXA_USER_ID), where to send notifications (apiEndpoint / locale), whether
                 notifications are allowed, and what has already been announced
    alexa_prefs  your choices from the Voice page: which things reach Alexa, and the optional
                 spoken approval code
"""

import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable

import db

PREFS_DEFAULTS: dict[str, Any] = {
    "notify_approvals": True,     # an approval waits while you're away from The Den -> Alexa notification
    "notify_results": True,       # a task you queued from Alexa finished while you were away -> Alexa notification
    "approval_pin": "",           # digits; when set, approving by voice needs it ("approve with code 4321")
}
KEEP_IDS = 300                    # announced / told id lists are trimmed to the newest this many

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get() -> dict[str, Any]:
    return dict(db.get_setting("alexa") or {})


def update(fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read-modify-write the link state under one lock (requests and the notifier both write it)."""
    with _lock:
        st = get()
        fn(st)
        for key in ("announced", "announced_tasks", "told", "watch"):
            if key in st:
                st[key] = st[key][-KEEP_IDS:]
        db.set_setting("alexa", st)
        return st


def prefs() -> dict[str, Any]:
    return {**PREFS_DEFAULTS, **(db.get_setting("alexa_prefs") or {})}


def set_prefs(changes: dict[str, Any]) -> dict[str, Any]:
    p = {**prefs(), **{k: v for k, v in changes.items() if k in PREFS_DEFAULTS}}
    db.set_setting("alexa_prefs", p)
    return p


# ----------------------------------------------------------------- config

def skill_id() -> str:
    return os.environ.get("ALEXA_SKILL_ID", "").strip()


def client_credentials() -> tuple[str, str]:
    """The skill's "Alexa Skill Messaging" client id / secret (developer console > Permissions)."""
    return os.environ.get("ALEXA_CLIENT_ID", "").strip(), os.environ.get("ALEXA_CLIENT_SECRET", "").strip()


def allowed_user(user_id: str) -> bool:
    """One owner. ALEXA_USER_ID pins it; otherwise the first Amazon account to use the skill is paired
    and every other account is refused (a development skill is only on your account anyway)."""
    pinned = os.environ.get("ALEXA_USER_ID", "").strip()
    if pinned:
        return user_id == pinned
    linked = get().get("user_id")
    return not linked or linked == user_id


# ------------------------------------------------------------ watch list

def watch_task(task_id: int) -> None:
    """A task queued from Alexa: tell Tushar how it went (next time he opens the skill, and by
    notification if he's away when it finishes)."""
    def fn(st: dict[str, Any]) -> None:
        if task_id not in st.setdefault("watch", []):
            st["watch"].append(task_id)
    update(fn)


def mark_told(task_ids: list[int]) -> None:
    def fn(st: dict[str, Any]) -> None:
        st["watch"] = [t for t in st.get("watch", []) if t not in task_ids]
        st["told"] = st.get("told", []) + [t for t in task_ids if t not in st.get("told", [])]
    if task_ids:
        update(fn)


FINISHED = ("done", "error", "needs_review", "cancelled")


def finished_watched() -> list[dict[str, Any]]:
    """Watched tasks that have finished (a thread counts as finished when its latest message has)."""
    out = []
    for tid in get().get("watch", []):
        task = db.get_task(tid)
        if not task:
            continue
        latest = (db.get_thread(tid) or [task])[-1]
        if latest["status"] in FINISHED:
            out.append({**task, "final": latest})
    return out


def pending_approvals() -> list[dict[str, Any]]:
    """Approvals waiting on a human, skipping projects whose approval notifications are off."""
    out = []
    for a in db.list_pending_approvals():
        try:
            if not db.get_project_settings(a["project_id"])["notify_approvals"]:
                continue
        except Exception:
            pass
        out.append(a)
    return out
