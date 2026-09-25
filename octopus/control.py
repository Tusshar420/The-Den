"""
Instant commands about Octopus itself - no model, no queue wait.

    "what's happening?"            -> status
    "stop the current task"        -> stop (or "cancel that": a plan waiting for your OK first)
    "continue the previous task"   -> continue
    "list my projects"             -> projects
    "add fix footer to the backlog of KodeAura7" -> backlog

match() is rules only and deliberately strict: it recognises a short command
about Octopus, never a request that merely contains the word "stop" or "cancel"
("cancel my meeting" is the calendar's). run() does the thing and returns
{"text", "spoken", "ok"}. Same pipeline whether the words were typed or spoken.
"""

import re
from datetime import datetime, timezone
from typing import Any, Optional

import db

# "Octopus, ..." / "hey octopus" / "please" / "can you" are just politeness.
_LEAD = re.compile(r"^(?:(?:hey|hi|ok|okay|yo)[ ,]+)?(?:octopus[ ,:]+)?(?:(?:please|can you|could you|will you)[ ,]+)?")
_TAIL = re.compile(r"(?:[ ,]+(?:please|now|right now|for me))*[ .!?]*$")

STATUS_RE = re.compile(
    r"^(?:what(?:'s| is| are)(?: you| it| octopus)?(?: currently| now)? (?:happening|going on|up|the status|the queue|running|"
    r"the current task|doing|working on)(?: right now| currently| now)?|what are you (?:currently |now )?(?:doing|working on)|"
    r"(?:queue )?status|how(?:'s| is) it going|any updates?|show (?:me )?(?:the )?(?:queue|status)|what is the current task)$")
STOP_RE = re.compile(
    r"^(?:stop|cancel|abort|halt|kill|forget)(?: (?:the |this |that )?(?:current |running |active )?(?:task|job|one|it|that|this|work)|"
    r" (?:that|it|this)|)(?: (?:task|job))?$|^(?:never ?mind|nevermind)$|^stop (?:everything|all(?: tasks)?)$")
CONTINUE_RE = re.compile(
    r"^(?:continue|resume|carry on|go on|keep going|pick up)(?: with| where you left off on)?(?: (?:the |that |my )?(?:previous|last|earlier|paused|stopped|cancelled|canceled|same)?"
    r"(?: ?(?:task|job|one|work))?)?$")
PROJECTS_RE = re.compile(r"^(?:list|show|what are|which are)(?: me)?(?: (?:my|all|the|your))* projects$|^(?:which|what) projects(?: do (?:i|you) have| are there| are registered)?$")
BACKLOG_ADD_RE = re.compile(
    r"^add (?P<item>.+?) to (?:the |my |its )?(?:(?P<p1>.+?) )?(?:backlog|to-?do(?: list)?|todos?)(?: (?:of|for|in|on) (?:the )?(?P<p2>.+?)(?: project)?)?$")
BACKLOG_SHOW_RE = re.compile(
    r"^(?:what(?:'s| is) (?:in )?|show (?:me )?|list )(?:the |my )?(?:backlog|to-?do(?: list)?|todos?)(?: (?:of|for|in|on) (?:the )?(?P<p>.+?)(?: project)?)?$")


def _normalise(text: str) -> str:
    t = text.strip().lower()
    t = _LEAD.sub("", t)
    t = _TAIL.sub("", t)
    return t.strip()


def match(text: str) -> Optional[dict[str, Any]]:
    """{"category", "action", "reason", ...} for a control command, else None."""
    raw = text.strip()
    if not raw or len(raw) > 200 or "\n" in raw:
        return None
    t = _normalise(raw)
    if STATUS_RE.match(t):
        return {"category": "control", "action": "status", "reason": "asks what Octopus is doing"}
    if STOP_RE.match(t):
        return {"category": "control", "action": "stop", "reason": "asks to stop or cancel"}
    if CONTINUE_RE.match(t):
        return {"category": "control", "action": "continue", "reason": "asks to continue the previous task"}
    if PROJECTS_RE.match(t):
        return {"category": "project", "action": "projects", "reason": "asks which projects exist"}
    m = BACKLOG_ADD_RE.match(t)
    if m:
        # keep the original casing of the item text
        item = re.search(re.escape(m.group("item")), raw, re.IGNORECASE)
        return {"category": "project", "action": "backlog_add", "reason": "asks to add to a backlog",
                "item": (item.group(0) if item else m.group("item")).strip(" \"'"),
                "project_name": (m.group("p2") or m.group("p1") or "").strip() or None}
    m = BACKLOG_SHOW_RE.match(t)
    if m:
        return {"category": "project", "action": "backlog_show", "reason": "asks what is in a backlog",
                "project_name": (m.group("p") or "").strip() or None}
    return None


# ---------------------------------------------------------------------- run

def _reply(text: str, spoken: Optional[str] = None, ok: bool = True, **extra: Any) -> dict[str, Any]:
    return {"text": text, "spoken": spoken if spoken is not None else text.splitlines()[0][:220], "ok": ok, **extra}


def _ago(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        secs = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        return ""
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)} min ago"
    return f"{round(secs / 3600)} h ago"


def _task_line(t: dict[str, Any]) -> str:
    p = db.get_project(t["project_id"]) or {}
    name = t.get("title") or (t.get("prompt") or "")[:60]
    where = "" if db.is_general_project(p) or not p else f" in {p['name']}"
    return f"'{name}'{where}"


def status() -> dict[str, Any]:
    q = db.queue_status()
    running = q.get("running_all") or []
    queued = [t for t in db.list_tasks(statuses=["queued"])]
    approvals = db.list_pending_approvals()
    parts: list[str] = []
    if running:
        for t in running:
            step = f" - {t['current_step']}" if t.get("current_step") else ""
            parts.append(f"Working on {_task_line(t)}{step}.")
    else:
        parts.append("Nothing is running right now.")
    if q.get("earliest_rate_limit_resume"):
        t = q["earliest_rate_limit_resume"]
        parts.append(f"{_task_line(t)} is waiting for a usage limit to reset.")
    if queued:
        parts.append(f"{len(queued)} waiting in the queue.")
    if approvals:
        parts.append(f"{len(approvals)} approval{'s' if len(approvals) != 1 else ''} need{'s' if len(approvals) == 1 else ''} your attention.")
    if not queued and not approvals and not running:
        parts.append("The queue is empty.")
    text = " ".join(parts)
    return _reply(text, text, running=[t["id"] for t in running], queued=len(queued), approvals=len(approvals))


def stop() -> dict[str, Any]:
    """Cancel what is waiting on you first ("cancel that"), else stop the
    running task, else drop the next queued one."""
    import worker
    pending = db.pending_plan()
    if pending and db.claim_routing(pending["id"], "cancelled"):
        return _reply(f"Cancelled the plan for '{pending['request'][:60]}'. Nothing was run.", acted="plan", routing_id=pending["id"])
    running = [t for t in (db.queue_status().get("running_all") or [])]
    stopped, stuck = [], []
    for t in running:
        (stopped if t.get("lane") == "agent" and worker.stop_task(t["id"]) else stuck).append(t)
    if stopped:
        text = "Stopped " + " and ".join(_task_line(t) for t in stopped) + "."
        return _reply(text, acted="task", task_ids=[t["id"] for t in stopped])
    queued = db.list_tasks(statuses=["queued"])
    if stuck and not queued:
        return _reply(f"{_task_line(stuck[0])} is running in a lane that can't be interrupted; it will finish in a moment.", ok=False)
    if queued:
        t = queued[0]
        db.cancel_task(t["id"])
        return _reply(f"Cancelled {_task_line(t)} - it hadn't started.", acted="task", task_ids=[t["id"]])
    return _reply("There's nothing running or waiting to stop.", ok=False)


def continue_previous() -> dict[str, Any]:
    """Pick the last stopped / failed / paused agent task back up as a reply in its own
    chat, so it resumes the same Claude session."""
    recent = [t for t in db.list_tasks(statuses=["cancelled", "error", "needs_review", "queued", "rate_limited"])
              if t.get("lane") == "agent"]
    if not recent:
        return _reply("I don't see a stopped or paused task to continue.", ok=False)
    last = max(recent, key=lambda t: t["id"])
    if last["status"] in ("queued", "rate_limited"):
        if last.get("paused"):
            db.set_paused(last["id"], False)
            return _reply(f"Resumed {_task_line(last)}.", acted="resume", task_ids=[last["id"]])
        return _reply(f"{_task_line(last)} is already waiting its turn.", ok=False)
    root = db.get_task(last.get("thread_id") or last["id"])
    task = db.add_task(root["project_id"], "Continue this task from where it stopped.", 0, root["id"], lane=root.get("lane") or "agent",
                       model_id=root.get("model_id"), category=root.get("category"), routed_by="thread",
                       route_reason="continues the previous task", permission_mode=last.get("permission_mode"),
                       agent_id=root.get("agent_id"), source="control")
    return _reply(f"Continuing {_task_line(root)}.", acted="continue", task_ids=[task["id"]])


def projects() -> dict[str, Any]:
    real = [p for p in db.list_projects(active_only=True) if not db.is_general_project(p)]
    if not real:
        return _reply("No projects are registered yet.")
    lines = []
    for p in real:
        n = len([t for t in db.list_todos(p["id"]) if not t.get("checked")])
        lines.append(f"• {p['name']}" + (f" - {n} to-do{'s' if n != 1 else ''} in the backlog" if n else ""))
    return _reply(f"You have {len(real)} project{'s' if len(real) != 1 else ''}:\n" + "\n".join(lines),
                  f"You have {len(real)} project{'s' if len(real) != 1 else ''}: " + ", ".join(p["name"] for p in real) + ".")


def _project_for(name: Optional[str], thread_project_id: Optional[int]) -> Optional[dict[str, Any]]:
    from octopus import plan
    if name:
        return plan.find_project(name)
    if thread_project_id:
        return db.get_project(thread_project_id)
    real = plan.real_projects()
    return real[0] if len(real) == 1 else None


def backlog_show(name: Optional[str], thread_project_id: Optional[int] = None) -> dict[str, Any]:
    p = _project_for(name, thread_project_id)
    if not p:
        return _reply("Which project's backlog? Say its name.", ok=False)
    todos = [t for t in db.list_todos(p["id"]) if not t.get("checked")]
    if not todos:
        return _reply(f"{p['name']}'s backlog is empty.")
    return _reply(f"{p['name']} has {len(todos)} item{'s' if len(todos) != 1 else ''} in its backlog:\n" +
                  "\n".join(f"• {t['text']}" for t in todos[:15]),
                  f"{p['name']} has {len(todos)} item{'s' if len(todos) != 1 else ''} in its backlog.")


def backlog_add(item: str, name: Optional[str], thread_project_id: Optional[int] = None) -> dict[str, Any]:
    p = _project_for(name, thread_project_id)
    if not p:
        return _reply("Which project should I add that to? Say its name.", ok=False)
    db.add_todo(p["id"], item)
    return _reply(f"Added '{item}' to {p['name']}'s backlog. Nothing runs until you queue it.", acted="todo")


def run(found: dict[str, Any], thread_project_id: Optional[int] = None) -> dict[str, Any]:
    action = found["action"]
    if action == "status":
        return status()
    if action == "stop":
        return stop()
    if action == "continue":
        return continue_previous()
    if action == "projects":
        return projects()
    if action == "backlog_show":
        return backlog_show(found.get("project_name"), thread_project_id)
    if action == "backlog_add":
        return backlog_add(found["item"], found.get("project_name"), thread_project_id)
    return _reply("I didn't understand that command.", ok=False)
