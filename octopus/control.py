"""
Instant commands about Octopus itself - no model, no queue wait.

    "what's happening?"            -> status
    "stop the current task"        -> stop (or "cancel that": a plan waiting for your OK first)
    "continue the previous task"   -> continue
    "list my projects"             -> projects
    "add fix footer to the backlog of KodeAura7" -> backlog
    "good morning" / "what's on today"             -> briefing (timeline.briefing)
    "what deadlines are coming up?"                -> timeline
    "schedule a client demo for KodeAura7 on Oct 3 at 3pm" -> timeline (adds it)
    "start phase 2 of KodeAura7" / "start the next phase" -> phase_start (phases.py)
    "pause the phase of KodeAura7" / "how are the phases going" -> phase_pause / phase_status

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
_LEAD_ANY_CASE = re.compile(_LEAD.pattern, re.IGNORECASE)

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
# The "Good morning" briefing (timeline.briefing). Never "...my calendar today" - that's the calendar's.
BRIEFING_RE = re.compile(
    r"^(?:good ?morning|morning|gm|(?:my |the |a )?(?:morning |daily )?briefing|brief me|(?:give me )?(?:my|a) (?:daily |morning )?(?:summary|rundown)|"
    r"what(?:'s| is) (?:on )?(?:for )?(?:my plate )?today|what(?:'s| is) on (?:for )?today|what do i have (?:on |going on )?today|"
    r"what(?:'s| does) (?:my day|today) look(?:s|ing)? like|how(?:'s| does) (?:my day|today) look(?:ing)?|plan my day)$")
TIMELINE_SHOW_RE = re.compile(
    r"^(?:(?:what|which)(?:'s| is| are)? (?:the )?(?:deadlines?|milestones?)(?: (?:are |do i have ))?(?:coming up|due|next|ahead)?|"
    r"what(?:'s| is| are) (?:coming up|due|overdue|next)|(?:any|upcoming|my|the|all) (?:upcoming )?(?:deadlines?|milestones?)|"
    r"(?:show|list)(?: me)?(?: the| my| all)?(?: upcoming)? (?:deadlines?|milestones?|timeline|project calendar|calendar of projects))"
    r"(?: (?:for|of|in|on) (?:the )?(?P<p>.+?)(?: project)?)?(?: (?:this|next) (?:week|month))?$")
# Adding to a timeline by voice: a verb, a kind of item, a registered project and a day (see _timeline_add).
PHASE_START_RE = re.compile(r"^(?:start|begin|kick ?off|resume|run|go (?:on )?with)\b(?!.*\bphase out\b)(?=.*\bphase\b)")
PHASE_PAUSE_RE = re.compile(r"^(?:pause|hold)\b(?=.*\b(?:phases?|autopilot)\b)(?!.*\bphase out\b)")
PHASE_STATUS_RE = re.compile(
    r"^(?:(?:what|which) phase (?:is|are) .+|how(?:'s| is| are) (?:the )?phases?\b.*|(?:show|list)(?: me)?(?: the| my)? phases\b.*|"
    r"phase status\b.*|what(?:'s| is) the phase status\b.*)$")
TIMELINE_ADD_RE = re.compile(r"^(?:add|schedule|set|put|create|book|plan|mark down|note)\b")
_KIND_WORDS = [
    (r"deadline|due date", "deadline"), (r"milestone|launch|release|go-?live", "milestone"),
    (r"client presentation|presentation|pitch", "presentation"), (r"demo", "demo"),
    (r"meeting|call|sync|stand-?up|catch-?up|review meeting", "meeting"),
]
# "every other day", "every weekday", "weekly"... on a meeting make it repeat (timeline.rule_of).
_REPEATS = [
    (re.compile(r"(?i)[\s,]*\b(?:every (?:other|second|2nd|2|two) days?|on alternate days|alternate days)\b"), {"freq": "days", "every": 2}),
    (re.compile(r"(?i)[\s,]*\b(?:every|each) (?:business day|weekday|work ?day)s?\b|[\s,]*\bon weekdays\b"), {"freq": "weekdays"}),
    (re.compile(r"(?i)[\s,]*\b(?:every day|each day|daily)\b"), {"freq": "days", "every": 1}),
    (re.compile(r"(?i)[\s,]*\b(?:every|each) other week\b|[\s,]*\bbi-?weekly\b|[\s,]*\bfortnightly\b"), {"freq": "weeks", "every": 2}),
    (re.compile(r"(?i)[\s,]*\b(?:every|each) week\b|[\s,]*\bweekly\b"), {"freq": "weeks", "every": 1}),
    (re.compile(r"(?i)[\s,]*\b(?:every|each) month\b|[\s,]*\bmonthly\b"), {"freq": "months", "every": 1}),
]
# Words that mean the personal calendar (Google) or a reminder, not a project timeline.
_NOT_TIMELINE = re.compile(r"\b(?:my calendar|google calendar|the calendar|invite|invitation|remind)\b|@")


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
    if PHASE_START_RE.match(t) and len(t) <= 90:
        return {"category": "project", "action": "phase_start", "reason": "asks to start a project phase", "text": t}
    if PHASE_PAUSE_RE.match(t) and len(t) <= 90:
        return {"category": "project", "action": "phase_pause", "reason": "asks to pause a project phase", "text": t}
    if PHASE_STATUS_RE.match(t) and "phase" in t and len(t) <= 90:
        return {"category": "project", "action": "phase_status", "reason": "asks how the project phases are going", "text": t}
    if BRIEFING_RE.match(t):
        return {"category": "project", "action": "briefing", "reason": "asks for the daily briefing"}
    m = TIMELINE_SHOW_RE.match(t)
    if m:
        return {"category": "project", "action": "timeline_show", "reason": "asks what is coming up on the project timelines",
                "project_name": (m.group("p") or "").strip() or None}
    return _timeline_add(raw, t)


def _timeline_add(raw: str, t: str) -> Optional[dict[str, Any]]:
    """"Schedule a client demo for KodeAura7 on October 3 at 3pm" -> a timeline
    item. Strict: it needs a verb, a kind of item, a registered project and a
    real day - otherwise it's left for the calendar / planner."""
    if not TIMELINE_ADD_RE.match(t) or _NOT_TIMELINE.search(t):
        return None
    kind = next((k for words, k in _KIND_WORDS if re.search(rf"\b(?:{words})s?\b", t)), None)
    if not kind:
        return None
    repeat, again = next(((rule, rx) for rx, rule in _REPEATS if rx.search(t)), (None, None))
    if repeat and kind != "meeting":
        return None
    from assistant import timeutil
    from octopus import plan
    raw = _LEAD_ANY_CASE.sub("", raw.strip())
    if again:
        raw = again.sub(" ", raw).strip()
    project = plan.find_project(raw)
    if not project:
        return None
    body, _, after = raw.partition(":")
    when = timeutil.parse_when(body)
    if not when.day or when.days or when.relative:
        return None
    title = after.strip()
    if not title:
        rest = timeutil.strip_spans(body, when.spans)
        rest = re.sub(r"(?i)\b(?:for|of|in|on|to)\s+(?:the\s+)?" + _name_regex(project["name"]) + r"(?:'s)?(?:\s+project)?\b", " ", rest)
        rest = re.sub(r"(?i)" + _name_regex(project["name"]) + r"(?:'s)?", " ", rest)
        rest = re.sub(r"(?i)^\s*(?:add|schedule|set|put|create|book|plan|mark down|note)\s+(?:(?:a|an|the|our|my)\s+)?(?:new\s+)?", "", rest)
        rest = re.sub(r"(?i)\s+(?:to|on|in)\s+(?:the\s+)?(?:timeline|schedule)\b", " ", rest)
        title = re.sub(r"\s+", " ", rest).strip(" ,.-")
    if not title:
        return None
    return {"category": "project", "action": "timeline_add", "reason": "asks to put a dated item on a project timeline",
            "kind": kind, "title": title[0].upper() + title[1:], "project_id": project["id"], "date": when.day.isoformat(),
            "time": when.start.strftime("%H:%M") if when.start else None, "repeat_rule": repeat,
            "repeat_days": repeat["every"] if repeat and repeat["freq"] == "days" else None}


def _name_regex(name: str) -> str:
    words = [w for w in re.split(r"[\s_\-./]+", name.strip()) if w]
    return r"[\s_\-./]?".join(re.escape(w) for w in words)


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


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}


def _phase_in(text: str, phase_list: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The phase a sentence names: by number ("phase 2", "the second phase"), by name, or "the next one"."""
    m = re.search(r"\bphase (\d+)\b|\b(\d+)(?:st|nd|rd|th) phase\b", text)
    num = int(m.group(1) or m.group(2)) if m else next(
        (n for w, n in _ORDINALS.items() if re.search(rf"\b{w} phase\b", text)), None)
    if num:
        return phase_list[num - 1] if 0 < num <= len(phase_list) else None
    named = [p for p in phase_list if p["name"].lower() in text]
    if named:
        return max(named, key=lambda p: len(p["name"]))
    if re.search(r"\b(?:next|upcoming)\b", text):
        return next((p for p in phase_list if p["status"] in ("planned", "paused")), None)
    return None


def _phase_project(text: str, thread_project_id: Optional[int]) -> Optional[dict[str, Any]]:
    from octopus import plan
    return plan.find_project(text) or _project_for(None, thread_project_id)


def phase_start(text: str, thread_project_id: Optional[int] = None) -> dict[str, Any]:
    import phases
    p = _phase_project(text, thread_project_id)
    if not p:
        return _reply("Which project's phase? Say its name, like 'start the next phase of KodeAura7'.", ok=False)
    phase_list = db.list_phases(p["id"])
    if not phase_list:
        return _reply(f"{p['name']} has no phases yet. Add them from its Phases tab.", ok=False)
    phase = _phase_in(text, phase_list)
    if not phase:
        names = ", ".join(f"{i + 1}. {x['name']}" for i, x in enumerate(phase_list))
        return _reply(f"Which phase of {p['name']}? It has: {names}.", ok=False)
    try:
        phases.start(phase["id"])
    except phases.PhaseError as e:
        return _reply(f"I couldn't start '{phase['name']}': {e}.", ok=False)
    import worker
    worker.wake_agent_lane()
    return _reply(f"Started '{phase['name']}' on {p['name']}. I'll work through its list one item at a time and tell you if one needs you.",
                  f"Started {phase['name']} on {p['name']}.", acted="phase")


def phase_pause(text: str, thread_project_id: Optional[int] = None) -> dict[str, Any]:
    import phases
    p = _phase_project(text, thread_project_id)
    if not p:
        return _reply("Which project should I pause? Say its name.", ok=False)
    active = next((x for x in db.list_phases(p["id"]) if x["status"] == "active"), None)
    if not active:
        return _reply(f"{p['name']} has no phase in progress.", ok=False)
    phases.pause(active["id"])
    return _reply(f"Paused '{active['name']}' on {p['name']}. Anything already queued finishes; nothing new is added.",
                  f"Paused {active['name']}.", acted="phase")


def phase_status(text: str, thread_project_id: Optional[int] = None) -> dict[str, Any]:
    import phases
    p = _phase_project(text, thread_project_id)
    if not p:
        return _reply("Which project's phases? Say its name.", ok=False)
    v = phases.project_view(p["id"])
    if not v["phases"]:
        return _reply(f"{p['name']} has no phases yet.")
    lines = []
    for i, ph in enumerate(v["phases"]):
        extra = f" - {ph['blocked_note']}" if ph.get("blocked_note") else ""
        lines.append(f"{i + 1}. {ph['name']}: {ph['status'].replace('_', ' ')}, {ph['counts']['done']}/{ph['total']} done{extra}")
    a = v["autopilot"]
    mode = {"manual": "on your command", "timeline": "by the timeline", "capacity": "when the limit allows"}[a["mode"]]
    active = next((ph for ph in v["phases"] if ph["status"] == "active"), None)
    spoken = (f"{p['name']} is on {active['name']}, {active['progress']} percent done." if active
              else f"{p['name']} has {len(v['phases'])} phases and none is running.")
    return _reply(f"{p['name']} · autopilot {mode}\n" + "\n".join(lines), spoken)


def briefing() -> dict[str, Any]:
    import timeline
    b = timeline.briefing()
    return _reply(b["text"], b["spoken"], briefing=True)


def timeline_show(name: Optional[str], thread_project_id: Optional[int] = None) -> dict[str, Any]:
    """Overdue items and the next two weeks, for one project or all of them."""
    import timeline
    from datetime import timedelta
    p = None
    if name:
        p = _project_for(name, None)
        if not p:
            return _reply(f"I don't know a project called '{name}'.", ok=False)
    end = (timeline.today() + timedelta(days=14)).isoformat()
    evs = sorted(timeline.views(db.list_project_events(p["id"] if p else None, end=end, statuses=["open"])),
                 key=lambda e: (e["date"], e.get("time") or "", e["id"]))
    evs = [e for e in evs if e["overdue"] or e["days_left"] >= 0]
    whose = f"{p['name']}'s timeline" if p else "the project timelines"
    if not evs:
        return _reply(f"Nothing is coming up on {whose} in the next two weeks.")
    lines = []
    for e in evs:
        state = "OVERDUE - was due " if e["overdue"] else ""
        where = "" if p else f" ({e['project_name']})"
        tail = timeline.outstanding_words(e["outstanding"]) if e.get("outstanding") else ""
        lines.append(f"• {e['kind_label']}: {e['title']}{where} - {state}{e['when']}" + (f". {tail}" if tail else ""))
    overdue = sum(1 for e in evs if e["overdue"])
    head = f"{len(evs)} item{'s' if len(evs) != 1 else ''} on {whose}" + (f", {overdue} overdue" if overdue else "") + ":"
    first = evs[0]
    spoken = head.rstrip(":") + f". First: {first['title']}, {first['when']}."
    return _reply(head + "\n" + "\n".join(lines), spoken)


def timeline_add(found: dict[str, Any]) -> dict[str, Any]:
    import timeline
    try:
        ev = timeline.view(timeline.create(found["project_id"], found["kind"], found["title"], found["date"], found.get("time"),
                                           source="octopus", repeat_rule=found.get("repeat_rule")))
    except timeline.TimelineError as e:
        return _reply(f"I couldn't add that: {e}.", ok=False)
    leads = timeline.remind_days(ev)
    # The reminders still to come (the ones it is already inside of were marked sent on creation).
    ahead = [("on the day" if d == 0 else "the day before" if d == 1 else f"{d} days before") for d in leads if d < ev["days_left"]]
    text = (f"Added to {ev['project_name']}'s timeline: {ev['kind_label'].lower()} '{ev['title']}', {ev['when']}"
            + (f", then {ev['repeat']}." if ev.get("repeat") else ".")
            + (f" I'll remind you {', '.join(ahead)}." if ahead else "")
            + " (It's on the project calendar, not your Google Calendar.)")
    return _reply(text, text.split(" (It's")[0], acted="timeline", event_id=ev["id"])


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
    if action == "phase_start":
        return phase_start(found["text"], thread_project_id)
    if action == "phase_pause":
        return phase_pause(found["text"], thread_project_id)
    if action == "phase_status":
        return phase_status(found["text"], thread_project_id)
    if action == "briefing":
        return briefing()
    if action == "timeline_show":
        return timeline_show(found.get("project_name"), thread_project_id)
    if action == "timeline_add":
        return timeline_add(found)
    return _reply("I didn't understand that command.", ok=False)
