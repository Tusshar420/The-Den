"""The dashboard's two Restart buttons (worker.request_restart) and what happens on every start.

Restart (normal): hold every waiting task, let what's running finish, restart, then let go
of exactly the tasks it held. Tasks you had paused yourself stay paused.
Hard restart: stop what's running now ("Stopped by hard restart" - Retry continues it), hold
the waiting tasks the same way, and restart within a few seconds.

On every start: a task still marked running lost its process with the old service (a hard
restart, a crash, launchctl kickstart), so it's marked interrupted instead of showing
"running" forever with nothing behind it.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import db
import mailer

log = logging.getLogger("the-den.restart")

# The tasks a restart held, so the next start knows which ones to let go of.
HELD_FILE = Path(__file__).parent / "restart_held.json"

WAITING = ("queued", "rate_limited")
STOPPED_NOTE = "Stopped by hard restart - Retry continues it"
INTERRUPTED_NOTE = "Interrupted by a restart - Retry continues it"


def _read_held() -> list[int]:
    try:
        return [int(i) for i in json.loads(HELD_FILE.read_text()).get("held", [])]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def hold_waiting() -> list[int]:
    """Pause every waiting task that isn't paused already and remember it. Safe to call
    more than once: a task queued meanwhile (a reply, a schedule) is held too."""
    with db._lock:
        rows = db.get_conn().execute(
            f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(WAITING))}) AND paused = 0 ORDER BY id",
            WAITING,
        ).fetchall()
    held = [r["id"] for r in rows if db.set_paused(r["id"], True)]
    if held:
        everything = sorted(set(_read_held()) | set(held))
        HELD_FILE.write_text(json.dumps({"held": everything}))
        log.info("Restart: holding %s", ", ".join(f"#{i}" for i in held))
    return held


def held_ids() -> list[int]:
    return _read_held()


def mark_stopped(task_ids: list[int]) -> None:
    """The tasks a hard restart killed: cancelled with a note, so Retry puts them back. Their
    lane may already have written its own verdict on the killed run (an error); this wins,
    unless the run really finished first."""
    with db._lock:
        conn = db.get_conn()
        for task_id in task_ids:
            conn.execute(
                "UPDATE tasks SET status = 'cancelled', paused = 0, finished_at = ?, error_message = ? "
                "WHERE id = ? AND status NOT IN ('done', 'deleted')",
                (db._now(), STOPPED_NOTE, task_id),
            )
        conn.commit()
    for task_id in task_ids:
        db.expire_pending_approvals(task_id)


def on_startup() -> int:
    """Before any lane starts: clear tasks left running by the old process, then let go of
    the tasks the restart held (only those - anything you paused yourself stays paused)."""
    with db._lock:
        orphans = [r["id"] for r in db.get_conn().execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()]
    for task_id in orphans:
        db.mark_error(task_id, INTERRUPTED_NOTE, needs_review=False)
        db.expire_pending_approvals(task_id)
    if orphans:
        log.warning("Startup: %s had no process any more - marked interrupted", ", ".join(f"#{i}" for i in orphans))

    held = _read_held()
    released = [i for i in held if db.set_paused(i, False)]
    try:
        HELD_FILE.unlink()
    except FileNotFoundError:
        pass
    if released:
        log.info("Startup: resumed %s (held by the restart)", ", ".join(f"#{i}" for i in released))
    return len(released)


# ------------------------------------------------------- "Restart needed" tags

EDIT_SLACK = 5   # seconds either side of a run that still count as its edit


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def needs_restart(started_ts: float) -> list[int]:
    """Tasks on the Den's own folder that edited its code after this process started (the
    same source files mailer.check_needs_restart watches): their change only loads on the
    next restart. Tasks don't record what they touched, so it goes by time - a source file
    changed while the task was running."""
    changed = [m for m, _ in mailer._changed_sources(started_ts)]
    if not changed:
        return []
    here = os.path.realpath(Path(__file__).parent)
    since = datetime.fromtimestamp(min(changed) - EDIT_SLACK, timezone.utc).isoformat()
    now = time.time()
    out = []
    for t in db.tasks_run_since(since):
        if not t["path"] or os.path.realpath(t["path"]) != here:
            continue
        start, end = _ts(t["started_at"]), _ts(t["finished_at"]) if t["finished_at"] else now
        if any(start - EDIT_SLACK <= m <= end + EDIT_SLACK for m in changed):
            out.append(t["id"])
    return out
