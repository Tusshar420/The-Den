"""
The work pages of The Den 2.0 (static/den/DenWork.dc.html): Review runs,
Hidden tasks and the parts of the Calendar page no other endpoint gives.
The Morning briefing reads /briefing and the shared store, so it has nothing here.

- Review runs: the latest Claude Code run in each chat that changed files
  (last REVIEW_DAYS days, plus anything still marked needs_review), with its
  files, git state and checks. Whether you reviewed it is kept here in
  den_work_reviews; "changes requested" is simply a later message in the same
  chat that hasn't finished yet. The diff itself comes from /tasks/{id}/diff.
  Revert never runs git from the API: it queues a Claude Code reply in that
  run's chat asking it to undo exactly that run, so it goes through the usual
  approvals like any other task (and the result shows up here for review).
- Hidden tasks: hidden chats with why they're hidden - hidden by you, from a
  recipe or schedule that is now switched off ("recipe muted"), or snoozed
  until a time (den_work_snoozes). A snooze that is over unhides its chat the
  next time the list is read (wake_snoozed). Run now unhides it and starts it
  again: a queued one runs now, a failed or stopped one is retried, and a
  finished one is queued afresh with the same prompt.
- Calendar: skip one day of a repeating item (timeline.skip).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
import den_recipes
import timeline

REVIEW_DAYS = 7
REVIEW_MAX = 60
HIDDEN_MAX = 300
SUMMARY_MAX = 1500
REVERT_FILES_MAX = 40
ACTIVE = ("queued", "running", "rate_limited")

_ready = False


def _init() -> None:
    global _ready
    if _ready:
        return
    with db._lock:
        conn = db.get_conn()
        # state: reviewed | reverted. revert_task_id: the reply that was queued to undo it.
        conn.execute("CREATE TABLE IF NOT EXISTS den_work_reviews (task_id INTEGER PRIMARY KEY, state TEXT NOT NULL, "
                     "at TEXT NOT NULL, revert_task_id INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS den_work_snoozes (task_id INTEGER PRIMARY KEY, until TEXT NOT NULL, "
                     "at TEXT NOT NULL)")
        conn.commit()
    _ready = True


def _utc(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _task_or_404(task_id: int) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task or task["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ------------------------------------------------------------------ review runs

def _reviews() -> dict[int, dict[str, Any]]:
    _init()
    with db._lock:
        return {r["task_id"]: dict(r) for r in db.get_conn().execute("SELECT * FROM den_work_reviews").fetchall()}


def _tests_label(art: dict[str, Any]) -> tuple[str, str]:
    """(label, verdict) for the run's check chip: passed | failed | unverified | none."""
    v = art.get("validation") or {}
    st = v.get("status")
    if st == "passed":
        return (v.get("note") or "Tests passed").rstrip("."), "passed"
    if st == "failed":
        return "Tests failed", "failed"
    if st == "unverified":
        return "No tests run", "unverified"
    return "No tests", "none"


def review_runs(days: int = REVIEW_DAYS) -> list[dict[str, Any]]:
    """Newest first: per chat, its latest finished Claude Code run that left changed files."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))).isoformat()
    with db._lock:
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.lane = 'agent' AND t.kind = 'task' AND t.status IN ('done', 'needs_review') AND t.hidden = 0 "
            "AND COALESCE(p.hidden, 0) = 0 AND (t.finished_at >= ? OR t.status = 'needs_review') "
            "ORDER BY COALESCE(t.finished_at, t.created_at) DESC, t.id DESC LIMIT 600", (cutoff,)).fetchall()
        runs = [db._row_to_dict(r) for r in rows]
        # Chats with a message still to run: what "changes requested" looks like.
        busy: dict[int, int] = {}
        for r in conn.execute(f"SELECT id, COALESCE(thread_id, id) AS root FROM tasks WHERE status IN ({','.join('?' * len(ACTIVE))})",
                              ACTIVE).fetchall():
            busy[r["root"]] = max(busy.get(r["root"], 0), r["id"])
    reviews = _reviews()
    out, seen = [], set()
    for t in runs:
        root = t["thread_id"] or t["id"]
        art = t.get("artifacts") if isinstance(t.get("artifacts"), dict) else {}
        files = art.get("files") or []
        if root in seen or not files:
            continue
        seen.add(root)
        rv = reviews.get(t["id"]) or {}
        if rv.get("state") == "reverted":
            state = "reverted"
        elif busy.get(root, 0) > t["id"]:
            state = "changes"
        elif rv.get("state") == "reviewed":
            state = "reviewed"
        else:
            state = "review"
        head = db.get_task(root) if root != t["id"] else t
        git = art.get("git") or {}
        label, verdict = _tests_label(art)
        out.append({
            "id": t["id"], "thread_id": root, "title": (head or t).get("title") or (t["prompt"] or "")[:80],
            "project_id": t["project_id"], "project_name": t.get("project_name"), "model_id": t.get("model_id"),
            "lane": t["lane"], "claude_account": t.get("claude_account"), "duration_ms": t.get("duration_ms"),
            "finished_at": t.get("finished_at"), "status": t["status"], "state": state,
            "branch": git.get("branch"), "base": git.get("base"), "head": git.get("head"), "commits": git.get("commits") or [],
            "uncommitted": bool(git.get("uncommitted")), "tests": label, "verdict": verdict,
            "summary": (t.get("result_summary") or "")[:SUMMARY_MAX],
            "files": [{"path": f.get("path"), "status": f.get("status"), "add": int(f.get("additions") or 0),
                       "del": int(f.get("deletions") or 0), "untracked": bool(f.get("untracked"))} for f in files],
            "revert_task_id": rv.get("revert_task_id"),
        })
        if len(out) >= REVIEW_MAX:
            break
    return out


def set_reviewed(task_id: int, on: bool = True) -> dict[str, Any]:
    """Mark a run reviewed (a needs_review task becomes done, as POST /tasks/{id}/reviewed does),
    or take that back. A reverted run stays reverted."""
    task = _task_or_404(task_id)
    _init()
    with db._lock:
        conn = db.get_conn()
        if on:
            conn.execute("INSERT INTO den_work_reviews (task_id, state, at) VALUES (?, 'reviewed', ?) "
                         "ON CONFLICT(task_id) DO UPDATE SET state = 'reviewed', at = excluded.at "
                         "WHERE den_work_reviews.state != 'reverted'", (task_id, db._now()))
        else:
            conn.execute("DELETE FROM den_work_reviews WHERE task_id = ? AND state = 'reviewed'", (task_id,))
        conn.commit()
    if on and task["status"] == "needs_review":
        db.mark_reviewed(task_id)
    return {"ok": True, "id": task_id, "reviewed": on}


def revert_prompt(task: dict[str, Any]) -> str:
    art = task.get("artifacts") if isinstance(task.get("artifacts"), dict) else {}
    git = art.get("git") or {}
    base = git["base"]
    short = base[:12]
    commits = git.get("commits") or []
    paths = [f.get("path") for f in art.get("files") or [] if f.get("path")]
    lines = [f"Revert the work from task #{task['id']} in this project - only that work, nothing else.",
             f"It started from commit {short}" + (f" on branch {git.get('base_branch')}" if git.get("base_branch") else "") + "."]
    if commits:
        lines.append("It made these commits (newest first): " + "; ".join(commits) +
                     ". Undo them with `git revert --no-edit`, newest first - don't rewrite history.")
    else:
        lines.append("It made no commits.")
    if paths:
        more = f" (and {len(paths) - REVERT_FILES_MAX} more)" if len(paths) > REVERT_FILES_MAX else ""
        lines.append("The files it changed: " + ", ".join(paths[:REVERT_FILES_MAX]) + more + ". Any of its changes "
                     f"that were left uncommitted: put those files back as they were at {short}, and remove files it newly created.")
    lines.append(f"First check `git status` and `git log {short}..HEAD`. If something else has changed those files since "
                 f"(work that isn't from task #{task['id']}), stop and tell me instead of overwriting it.")
    lines.append("Don't push. When you're done, say exactly what you reverted.")
    return "\n".join(lines)


def revert(task_id: int) -> dict[str, Any]:
    """Queue the undo as a reply in the run's own chat (so Claude Code resumes with what it did in
    mind). Nothing runs git here; its commits and any push go through the usual approvals."""
    task = _task_or_404(task_id)
    if task["lane"] != "agent" or task["status"] not in ("done", "needs_review"):
        raise HTTPException(status_code=409, detail="Only a finished Claude Code run can be reverted")
    art = task.get("artifacts") if isinstance(task.get("artifacts"), dict) else {}
    if not (art.get("git") or {}).get("base"):
        raise HTTPException(status_code=409, detail="This run has no git starting point, so it can't be reverted safely")
    project = db.get_project(task["project_id"])
    if not db.has_folder(project):
        raise HTTPException(status_code=409, detail=db.NO_FOLDER_HINT)
    rv = _reviews().get(task_id) or {}
    old = db.get_task(rv["revert_task_id"]) if rv.get("revert_task_id") else None
    if old and old["status"] in ACTIVE:
        raise HTTPException(status_code=409, detail=f"A revert is already queued (#{old['id']})")
    root = task["thread_id"] or task["id"]
    new = db.add_task(task["project_id"], revert_prompt(task), 0, root, lane="agent", model_id=task.get("model_id") or "claude-code",
                      routed_by="direct", route_reason=f"Review: revert run #{task_id}", permission_mode=task.get("permission_mode"),
                      env_id=task.get("env_id"), source="dashboard")
    with db._lock:
        conn = db.get_conn()
        conn.execute("INSERT INTO den_work_reviews (task_id, state, at, revert_task_id) VALUES (?, 'reverted', ?, ?) "
                     "ON CONFLICT(task_id) DO UPDATE SET state = 'reverted', at = excluded.at, revert_task_id = excluded.revert_task_id",
                     (task_id, db._now(), new["id"]))
        conn.commit()
    return {"ok": True, "id": task_id, "task_id": new["id"], "thread_id": root}


# ------------------------------------------------------------------ hidden tasks

def _snoozes() -> dict[int, str]:
    _init()
    with db._lock:
        return {r["task_id"]: r["until"] for r in db.get_conn().execute("SELECT task_id, until FROM den_work_snoozes").fetchall()}


def wake_snoozed(now: Optional[datetime] = None) -> list[int]:
    """Unhide the chats whose snooze is over. Returns their ids."""
    now = now or datetime.now(timezone.utc)
    due = [tid for tid, until in _snoozes().items() if (_utc(until) or now) <= now]
    if due:
        db.set_task_hidden(due, False)
        with db._lock:
            conn = db.get_conn()
            conn.executemany("DELETE FROM den_work_snoozes WHERE task_id = ?", [(t,) for t in due])
            conn.commit()
    return due


def snooze(task_id: int, until: str) -> dict[str, Any]:
    """Hide a chat until a time; it comes back on its own once that has passed."""
    task = _task_or_404(task_id)
    when = _utc(until)
    if not when:
        raise HTTPException(status_code=400, detail="until must be an ISO time")
    if when <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="until must be in the future")
    root = task["thread_id"] or task["id"]
    db.set_task_hidden([root], True)
    _init()
    with db._lock:
        conn = db.get_conn()
        conn.execute("INSERT INTO den_work_snoozes (task_id, until, at) VALUES (?, ?, ?) "
                     "ON CONFLICT(task_id) DO UPDATE SET until = excluded.until, at = excluded.at",
                     (root, when.isoformat(), db._now()))
        conn.commit()
    return {"ok": True, "id": root, "until": when.isoformat()}


def _muted_by(task: dict[str, Any], recipes: dict[str, bool], schedules: list[dict[str, Any]]) -> Optional[str]:
    """The name of the switched-off recipe or schedule a task came from (None if it didn't, or it's still on)."""
    if task.get("routed_by") == "recipe":
        name = (task.get("route_reason") or "").removeprefix("Recipe: ").strip()
        if name in recipes and not recipes[name]:
            return name
    if task.get("routed_by") == "schedule":
        for sc in schedules:
            if sc["prompt"] == task["prompt"] and (sc.get("project_id") in (None, task["project_id"])):
                return None if sc["enabled"] else (sc.get("label") or sc["prompt"][:50])
    return None


def hidden_tasks() -> list[dict[str, Any]]:
    """Hidden chats (their first message), most recent first, each with why it's hidden."""
    wake_snoozed()
    snoozes = _snoozes()
    recipes = {r["name"]: bool(r["enabled"]) for r in den_recipes.list_own()}
    schedules = db.list_schedules()
    out = []
    for t in db.list_all_tasks(limit=HIDDEN_MAX, hidden=True):
        if t["thread_id"]:
            continue
        muted = _muted_by(t, recipes, schedules)
        why = "snoozed" if t["id"] in snoozes else "muted" if muted else "hidden"
        out.append({"id": t["id"], "title": t.get("title") or (t["prompt"] or "")[:80], "project_id": t["project_id"],
                    "project_name": t.get("project_name"), "status": t["status"], "kind": t.get("kind"), "lane": t["lane"],
                    "why": why, "until": snoozes.get(t["id"]), "recipe": muted,
                    "at": t.get("finished_at") or t.get("started_at") or t["created_at"]})
    return out


def run_now(task_id: int) -> dict[str, Any]:
    """Unhide a hidden chat and set it going. mode: now (a queued task starts right away),
    front (a queued answer-only task goes first), retry (a failed / stopped one is queued
    again as the same task), again (a finished one is queued afresh with the same prompt)."""
    task = _task_or_404(task_id)
    project = db.get_project(task["project_id"])
    if not project or not project["active"]:
        raise HTTPException(status_code=409, detail="Its project is paused - resume the project first")
    if not db.has_folder(project):
        raise HTTPException(status_code=400, detail=db.NO_FOLDER_HINT)
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="It's already running")
    root = task["thread_id"] or task["id"]
    db.set_task_hidden([root], False)
    _init()
    with db._lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM den_work_snoozes WHERE task_id = ?", (root,))
        conn.commit()
    if task["status"] == "queued":
        if task["lane"] == "agent":
            db.set_run_now(task_id)
            return {"ok": True, "mode": "now", "task_id": task_id}
        db.move_to_front(task_id)
        return {"ok": True, "mode": "front", "task_id": task_id}
    if task["status"] in db.REQUEUEABLE:
        db.requeue_task(task_id, front=True)
        return {"ok": True, "mode": "retry", "task_id": task_id}
    new = db.add_task(task["project_id"], task["prompt"], task.get("priority") or 0, None, lane=task["lane"],
                      model_id=task.get("model_id"), category=task.get("category"), routed_by="direct",
                      route_reason=f"Hidden tasks: run #{task_id} again", permission_mode=task.get("permission_mode"),
                      source="dashboard", env_id=task.get("env_id"), kind=task.get("kind"), images=task.get("images"))
    return {"ok": True, "mode": "again", "task_id": new["id"]}


# ------------------------------------------------------------------ router

class ReviewedIn(BaseModel):
    on: bool = True


class SnoozeIn(BaseModel):
    until: str          # ISO time; the chat comes back once it has passed


class SkipIn(BaseModel):
    date: str           # YYYY-MM-DD: the one day to take out of a repeating item


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/work/runs")
    def runs(days: int = REVIEW_DAYS):
        return {"runs": review_runs(days), "days": days}

    @r.post("/den/work/runs/{task_id}/reviewed")
    def reviewed(task_id: int, body: Optional[ReviewedIn] = None):
        return set_reviewed(task_id, body.on if body else True)

    @r.post("/den/work/runs/{task_id}/revert")
    def revert_run(task_id: int):
        return revert(task_id)

    @r.get("/den/work/hidden")
    def hidden():
        return {"items": hidden_tasks()}

    @r.post("/den/work/hidden/{task_id}/snooze")
    def snooze_task(task_id: int, body: SnoozeIn):
        return snooze(task_id, body.until)

    @r.post("/den/work/hidden/{task_id}/run")
    def run_hidden(task_id: int):
        return run_now(task_id)

    @r.post("/den/work/calendar/{event_id}/skip")
    def skip_day(event_id: int, body: SkipIn):
        try:
            return timeline.view(timeline.skip(event_id, body.date))
        except timeline.TimelineError as e:
            raise HTTPException(status_code=404 if "not found" in str(e) else 422, detail=str(e))

    return r
