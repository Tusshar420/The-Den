"""
Project phases and autopilot: ordered stages of work, each holding the todos
to do in it, which Octopus works through on its own.

This module owns the rules; db.py only stores rows.

- A phase is planned -> active -> done (or paused). Todos are assigned to a
  phase (todos.phase_id). Only one phase per project is active at a time.
- Octopus feeds an active phase's todos to the queue ONE AT A TIME: the next
  is queued only when the last one finished. A failed or needs-review todo
  stops the phase (blocked) and pushes you, instead of piling on more work.
- What starts a phase is the project's autopilot mode (project settings):
    manual    only when you say so (Start on the phase, or tell Octopus)
    timeline  a planned phase starts on its start date, in order
    capacity  phases run back to back whenever the Claude usage limit has room
  In timeline / capacity mode nothing is fed once the weekly usage passes the
  project's ceiling (or the account is waiting out a limit); it carries on when
  the room is back. A phase you start yourself ignores the ceiling for its
  first todo - your command wins.
- tick: what the scheduler runs every half minute; returns the alerts to push.
- prompt_for: the phase in progress as context for the model working on it.
- report: a markdown progress report on the project or some of its phases.

Dates are local calendar days (assistant.timeutil's zone).
"""

import re
from datetime import date, datetime, timezone
from typing import Any, Optional

import accounts
import db
import providers
import router
from assistant import timeutil

MODES = ("manual", "timeline", "capacity")
STATUSES = db.PHASE_STATUSES
NAME_MAX = 120
GOAL_MAX = 2000
DESCRIPTION_MAX = 4000
MAX_GENERATED = 15
# The 5-hour window is never fed past this, whatever the project's weekly ceiling.
FIVE_HOUR_CEILING = 0.90
MIN_CEILING, MAX_CEILING = 10, 100

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A todo in one of these is still on its way: it is queued, running, or waiting out a limit.
IN_FLIGHT = ("queued", "running", "rate_limited")
FAILED = ("error", "needs_review")


class PhaseError(ValueError):
    pass


# ------------------------------------------------------------------- basics

def _today(now: Optional[datetime] = None) -> date:
    return (now or timeutil.now()).astimezone(timeutil.local_tz()).date()


def _clean_date(value: Optional[str], label: str) -> Optional[str]:
    if value in (None, ""):
        return None
    if not _DATE_RE.match(value):
        raise PhaseError(f"{label} must be a date as YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise PhaseError(f"{label} must be a real date")
    return value


def _clean_text(value: Optional[str], label: str, limit: int, required: bool = False) -> Optional[str]:
    text = re.sub(r"[ \t]+", " ", (value or "").strip())
    if required and not text:
        raise PhaseError(f"A {label} is required")
    if len(text) > limit:
        raise PhaseError(f"The {label} is too long (max {limit} characters)")
    return text or None


def _check_dates(start: Optional[str], end: Optional[str]) -> None:
    if start and end and end < start:
        raise PhaseError("A phase can't end before it starts")


def todo_state(todo: dict[str, Any]) -> str:
    """pending | queued | running | rate_limited | done | error | needs_review | cancelled."""
    if todo["status"] == "pending":
        return "pending"
    state = todo.get("task_status") or todo["status"]
    return "cancelled" if state == "deleted" else state


# --------------------------------------------------------------- operations

def mode_of(project_id: int) -> str:
    mode = db.get_project_settings(project_id)["autopilot"]
    return mode if mode in MODES else "manual"


def create(project_id: int, name: str, goal: Optional[str] = None, start_date: Optional[str] = None,
           end_date: Optional[str] = None, todos: Optional[list[str]] = None,
           description: Optional[str] = None, env_id: Optional[int] = None) -> dict[str, Any]:
    if not db.get_project(project_id):
        raise PhaseError("Project not found")
    name = _clean_text(name, "name", NAME_MAX, required=True)
    start, end = _clean_date(start_date, "The start date"), _clean_date(end_date, "The end date")
    _check_dates(start, end)
    phase = db.add_phase(project_id, name, _clean_text(goal, "goal", GOAL_MAX), start, end,
                         _clean_text(description, "description", DESCRIPTION_MAX), env_id=env_id)
    for text in todos or []:
        text = (text or "").strip()
        if text:
            db.set_todo_phase(db.add_todo(project_id, text)["id"], phase["id"])
    return db.get_phase(phase["id"])


def update(phase_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    phase = db.get_phase(phase_id)
    if not phase:
        raise PhaseError("Phase not found")
    fields: dict[str, Any] = {}
    if "name" in changes:
        fields["name"] = _clean_text(changes["name"], "name", NAME_MAX, required=True)
    if "goal" in changes:
        fields["goal"] = _clean_text(changes["goal"], "goal", GOAL_MAX)
    if "description" in changes:
        fields["description"] = _clean_text(changes["description"], "description", DESCRIPTION_MAX)
    for key, label in (("start_date", "The start date"), ("end_date", "The end date")):
        if key in changes:
            fields[key] = _clean_date(changes[key], label)
    _check_dates(fields.get("start_date", phase["start_date"]), fields.get("end_date", phase["end_date"]))
    return db.update_phase(phase_id, **fields)


GENERATE_SYSTEM_PROMPT = (
    "You break one phase of a software project into concrete tasks for an AI coding agent that works in the "
    "project's folder, one task at a time, top to bottom. Each task must be a self-contained instruction a "
    "developer could finish in one sitting, ordered so earlier tasks unblock later ones. Stay inside the phase's "
    f"goal and description; do not repeat tasks already listed. Give at most {MAX_GENERATED} tasks. "
    "Return ONLY the tasks, one per line, each starting with '- '. No headings, numbering or commentary."
)


def generation_models() -> list[dict[str, Any]]:
    """Models that can write a task list: any text model with a direct completer (not image or assistant)."""
    out = []
    for m in router.models_status():
        if m.get("provider") in providers.COMPLETERS and m.get("modality") != "image":
            out.append(m)
    return out


def _parse_tasks(text: str) -> list[str]:
    tasks = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", line).strip()
        if line and len(line) <= 1000:
            tasks.append(line)
    return tasks[:MAX_GENERATED]


def generate_tasks(phase_id: int, model_id: Optional[str] = None) -> dict[str, Any]:
    """Ask a model (the one picked, else the cheapest usable) for this phase's tasks and add them to
    the end of its to-do list. Returns the phase view, the tasks added and the model used."""
    phase = db.get_phase(phase_id)
    if not phase:
        raise PhaseError("Phase not found")
    if phase["status"] == "done":
        raise PhaseError("That phase is already done")
    if not (phase.get("goal") or phase.get("description")):
        raise PhaseError("Give the phase a goal or description first - that is what the tasks are made from")
    if model_id:
        model = router.get_model(model_id)
        if not model or model.get("provider") not in providers.COMPLETERS or model.get("modality") == "image":
            raise PhaseError("That model can't write a task list")
        ok, why, _ = router.availability(model)
        if not ok:
            raise PhaseError(f"{model.get('label') or model['id']} isn't available: {why}")
    else:
        model = router.cheapest_api_model()
        if not model:
            raise PhaseError("No model is available to write with")
    project = db.get_project(phase["project_id"])
    siblings = db.list_phases(phase["project_id"])
    lines = [f"Project: {project['name']}", "All phases, in order: " + "; ".join(p["name"] for p in siblings),
             f"This phase: {phase['name']}"]
    if phase.get("goal"):
        lines.append("Goal: " + phase["goal"])
    if phase.get("description"):
        lines.append("Description:\n" + phase["description"])
    existing = [t["text"].strip().split("\n")[0][:160] for t in _todos(phase)]
    if existing:
        lines.append("Tasks already in this phase (do not repeat):\n" + "\n".join("- " + t for t in existing))
    try:
        out = providers.complete(model, [{"role": "user", "content": "\n\n".join(lines)[:9000]}], GENERATE_SYSTEM_PROMPT, 180)
    except providers.ProviderError as e:
        raise PhaseError(str(e))
    db.log_usage("phase_tasks", model["id"], out.input_tokens, out.output_tokens,
                 providers.estimate_cost(model, out.input_tokens, out.output_tokens))
    tasks = _parse_tasks(out.text)
    if not tasks:
        raise PhaseError("The model didn't return any tasks - try again or pick another model")
    for text in tasks:
        db.set_todo_phase(db.add_todo(phase["project_id"], text)["id"], phase_id)
    return {"phase": view(db.get_phase(phase_id)), "tasks": tasks, "model_id": model["id"]}


def assign_todo(todo_id: int, phase_id: Optional[int]) -> None:
    todo = db.get_todo(todo_id)
    if not todo:
        raise PhaseError("Todo not found")
    if phase_id:
        phase = db.get_phase(phase_id)
        if not phase or phase["project_id"] != todo["project_id"]:
            raise PhaseError("That phase belongs to a different project")
    db.set_todo_phase(todo_id, phase_id or None)


def _alert(phase: dict[str, Any], key: str) -> bool:
    """True the first time this alert key comes up for the phase (and remembers it)."""
    if key in phase["alerts_sent"]:
        return False
    db.update_phase(phase["id"], alerts_sent=phase["alerts_sent"] + [key])
    phase["alerts_sent"] = phase["alerts_sent"] + [key]
    return True


def _activate(phase: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """Make this the project's one active phase; whichever was active is paused."""
    for other in db.list_phases(phase["project_id"]):
        if other["id"] != phase["id"] and other["status"] == "active":
            db.update_phase(other["id"], status="paused")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return db.update_phase(phase["id"], status="active", blocked_note=None,
                           started_at=phase["started_at"] or stamp, finished_at=None)


def start(phase_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """You said go: this phase becomes the active one and its first open todo is queued now,
    whatever the usage ceiling says. Nothing to do in it is an error worth saying out loud."""
    phase = db.get_phase(phase_id)
    if not phase:
        raise PhaseError("Phase not found")
    if phase["status"] == "done":
        raise PhaseError("That phase is already done")
    if not db.has_folder(db.get_project(phase["project_id"])):
        raise PhaseError(db.NO_FOLDER_HINT)     # a CRM-only project's phases are for planning
    if not any(todo_state(t) == "pending" for t in _todos(phase)) and not any(
            todo_state(t) in IN_FLIGHT + FAILED for t in _todos(phase)):
        raise PhaseError("There's nothing to do in this phase yet - add some todos to it first")
    phase = _activate(phase, now)
    advance(phase, now, force=True)
    return db.get_phase(phase_id)


def pause(phase_id: int) -> dict[str, Any]:
    """Stop feeding this phase. What is already queued or running carries on."""
    phase = db.get_phase(phase_id)
    if not phase:
        raise PhaseError("Phase not found")
    if phase["status"] != "active":
        raise PhaseError("Only the active phase can be paused")
    return db.update_phase(phase_id, status="paused")


def complete(phase_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """Close the phase by hand, whatever is left in it."""
    phase = db.get_phase(phase_id)
    if not phase:
        raise PhaseError("Phase not found")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return db.update_phase(phase_id, status="done", finished_at=stamp, blocked_note=None)


def reopen(phase_id: int) -> dict[str, Any]:
    phase = db.get_phase(phase_id)
    if not phase:
        raise PhaseError("Phase not found")
    return db.update_phase(phase_id, status="planned", finished_at=None, blocked_note=None)


def set_mode(project_id: int, mode: str, ceiling: Optional[int] = None) -> dict[str, Any]:
    if mode not in MODES:
        raise PhaseError(f"Autopilot mode must be one of: {', '.join(MODES)}")
    if mode != "manual" and not db.has_folder(db.get_project(project_id)):
        raise PhaseError(db.NO_FOLDER_HINT)
    fields: dict[str, Any] = {"autopilot": mode}
    if ceiling is not None:
        fields["autopilot_ceiling"] = clean_ceiling(ceiling)
    return db.update_project_settings(project_id, **fields)


def clean_ceiling(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise PhaseError("The usage ceiling is a percentage")
    if not MIN_CEILING <= n <= MAX_CEILING:
        raise PhaseError(f"The usage ceiling must be between {MIN_CEILING} and {MAX_CEILING} percent")
    return n


# ----------------------------------------------------------------- capacity

def _utilization(usage: Optional[dict[str, Any]], window: str, now: datetime) -> Optional[float]:
    """0..1 used in a usage window, None if unknown or the window has already reset."""
    w = ((usage or {}).get("windows") or {}).get(window) or {}
    util, resets = w.get("utilization"), w.get("resets_at")
    if util is None:
        return None
    if resets:
        try:
            if datetime.fromisoformat(resets.replace("Z", "+00:00")) <= now:
                return None
        except ValueError:
            return None
    return float(util)


def capacity(project_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """Whether Claude has room for more of this project's work: {ok, reason, weekly_pct, five_hour_pct}.
    Unknown usage counts as room - the first task reports it."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    settings = db.get_project_settings(project_id)
    account = accounts.for_project(project_id, own=settings["claude_account"])
    limited = db.claude_account_limits(accounts.active_id())
    usage = (db.get_setting("claude_usage_by_account") or {}).get(account)
    weekly, five = _utilization(usage, "seven_day", now), _utilization(usage, "five_hour", now)
    out = {"ok": True, "reason": None, "ceiling": settings["autopilot_ceiling"],
           "weekly_pct": None if weekly is None else round(weekly * 100),
           "five_hour_pct": None if five is None else round(five * 100)}
    if account in limited:
        out.update(ok=False, reason="waiting for the usage limit to reset")
    elif weekly is not None and weekly * 100 >= settings["autopilot_ceiling"]:
        out.update(ok=False, reason=f"the weekly limit is {out['weekly_pct']}% used (ceiling {settings['autopilot_ceiling']}%)")
    elif five is not None and five >= FIVE_HOUR_CEILING:
        out.update(ok=False, reason=f"the 5-hour limit is {out['five_hour_pct']}% used")
    return out


# --------------------------------------------------------------------- feed

def _todos(phase: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in db.list_todos(phase["project_id"]) if t.get("phase_id") == phase["id"]]


def todo_decisions(project: dict[str, Any], todos: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """The router's decision for each todo with a model of its own - todos without one go to Claude Code."""
    name = None if db.is_general_project(project) else project["name"]
    decisions: dict[int, dict[str, Any]] = {}
    for t in todos:
        if t["status"] == "pending" and t["model_id"]:
            try:
                decision = router.route(t["text"], project_name=name, force_model=t["model_id"])
            except ValueError:
                continue  # can't take it right now - it runs on the default rather than being lost
            cfg = router.get_model(decision["model_id"]) or {}
            if t.get("images") and decision["lane"] != "agent" and not cfg.get("vision"):
                continue  # that model can't read its pictures - Claude Code (the default) can
            decisions[t["id"]] = decision
    return decisions


def _queue(project: dict[str, Any], todo: dict[str, Any]) -> None:
    db.run_checked_todos(project["id"], decisions=todo_decisions(project, [todo]), only_ids=[todo["id"]])


def advance(phase: dict[str, Any], now: Optional[datetime] = None, force: bool = False) -> list[dict[str, Any]]:
    """One step for an active phase: queue its next todo if nothing is in flight, stop if something
    failed, finish it when everything is done. force skips the usage ceiling (your command).
    Returns the alerts to push: [{"phase", "key", "title", "message", "kind"}]."""
    project = db.get_project(phase["project_id"])
    alerts: list[dict[str, Any]] = []
    if not project or not project["active"] or project.get("status") in ("on_hold", "completed"):
        return alerts
    if not db.has_folder(project):
        return alerts                           # nothing can run in a CRM-only project
    todos = _todos(phase)
    states = [todo_state(t) for t in todos]
    name = f"{project['name']} · {phase['name']}"

    def say(key: str, title: str, message: str, kind: str = "info") -> None:
        if _alert(phase, key):
            alerts.append({"phase": phase, "key": key, "title": title, "message": message, "kind": kind})

    end = phase.get("end_date")
    if end and end < _today(now).isoformat() and "late" not in phase["alerts_sent"] and any(
            s not in ("done", "cancelled") for s in states):
        left = sum(1 for s in states if s not in ("done", "cancelled"))
        say("late", f"Phase running late · {name}", f"It was due {end}; {left} item{'s' * (left != 1)} still open.", "failure")

    failed = [t for t, s in zip(todos, states) if s in FAILED]
    if failed:
        note = f"“{failed[0]['text'][:80]}” needs you"
        if phase["blocked_note"] != note:
            db.update_phase(phase["id"], blocked_note=note)
        say(f"blocked{failed[0]['id']}", f"Phase stopped · {name}",
            f"“{failed[0]['text'][:100]}” needs a look before I carry on.", "failure")
        return alerts
    if phase["blocked_note"]:
        db.update_phase(phase["id"], blocked_note=None)

    if any(s in IN_FLIGHT for s in states):
        return alerts                                   # one at a time: wait for it
    pending = [t for t, s in zip(todos, states) if s == "pending"]
    if pending:
        if not force and mode_of(project["id"]) != "manual":
            room = capacity(project["id"])
            if not room["ok"]:
                note = f"Waiting: {room['reason']}"
                if phase["blocked_note"] != note:
                    db.update_phase(phase["id"], blocked_note=note)
                return alerts
        _queue(project, pending[0])
        return alerts
    # Nothing pending, nothing in flight, nothing failed: every todo is done or was cancelled.
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    db.update_phase(phase["id"], status="done", finished_at=stamp, blocked_note=None)
    phase["status"] = "done"
    done = sum(1 for s in states if s == "done")
    say("done", f"Phase done · {name}", f"{done} item{'s' * (done != 1)} finished." + _next_hint(project["id"]))
    return alerts


def _next_hint(project_id: int) -> str:
    nxt = next((p for p in db.list_phases(project_id) if p["status"] == "planned"), None)
    return f" Next up: {nxt['name']}." if nxt else " That was the last phase."


def _next_to_start(project_id: int, mode: str, today: date) -> Optional[dict[str, Any]]:
    """The planned phase autopilot should start now, if any: first in order that has todos to do,
    and (timeline mode) whose start date has come."""
    for phase in db.list_phases(project_id):
        if phase["status"] != "planned":
            continue
        if not any(todo_state(t) == "pending" for t in _todos(phase)):
            continue
        if mode == "timeline" and phase["start_date"] and phase["start_date"] > today.isoformat():
            return None                                 # in order: the later ones wait for this one
        return phase
    return None


def tick(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Everything autopilot does this minute, for every project with phases. Returns the alerts to push."""
    alerts: list[dict[str, Any]] = []
    for project in db.projects_with_open_phases():
        pid = project["id"]
        mode = mode_of(pid)
        active = next((p for p in db.list_phases(pid) if p["status"] == "active"), None)
        if active:
            alerts += advance(active, now)
            if active["status"] != "done":
                continue
        if mode == "manual":
            continue
        # Between phases (or just finished one): start the next, if the limit has room.
        nxt = _next_to_start(pid, mode, _today(now))
        if not nxt or not capacity(pid)["ok"]:
            continue
        phase = _activate(nxt, now)
        alerts.append({"phase": phase, "key": "started", "kind": "info",
                       "title": f"Phase started · {project['name']} · {phase['name']}",
                       "message": "Working through its to-do list now."})
        alerts += advance(phase, now)
    return alerts


# --------------------------------------------------------------------- views

def view(phase: dict[str, Any], today: Optional[date] = None) -> dict[str, Any]:
    """A phase as the dashboard shows it: its todos' progress, what stops it, how late it is."""
    today = today or _today()
    todos = _todos(phase)
    states = [todo_state(t) for t in todos]
    counts = {k: sum(1 for s in states if s == k) for k in ("pending", "queued", "running", "rate_limited", "done", "error",
                                                              "needs_review", "cancelled")}
    total = len(todos) - counts["cancelled"]
    end = phase.get("end_date")
    days_left = (date.fromisoformat(end) - today).days if end else None
    live = phase["status"] != "done"
    return {
        **{k: v for k, v in phase.items() if k != "alerts_sent"},
        "todo_ids": [t["id"] for t in todos],
        "counts": counts, "total": total,
        "progress": round(100 * counts["done"] / total) if total else 0,
        "failed": counts["error"] + counts["needs_review"],
        "days_left": days_left,
        "late": bool(live and days_left is not None and days_left < 0),
        "starts_in": (date.fromisoformat(phase["start_date"]) - today).days if phase.get("start_date") else None,
    }


def project_view(project_id: int) -> dict[str, Any]:
    """Everything the Phases tab needs for one project."""
    settings = db.get_project_settings(project_id)
    phases = [view(p) for p in db.list_phases(project_id)]
    assigned = {i for p in phases for i in p["todo_ids"]}
    unassigned = [t["id"] for t in db.list_todos(project_id) if t["id"] not in assigned and t["status"] == "pending"]
    return {"phases": phases, "unassigned": unassigned,
            "autopilot": {"mode": mode_of(project_id), "ceiling": settings["autopilot_ceiling"],
                          "capacity": capacity(project_id)}}


def prompt_for(project_id: Optional[int]) -> str:
    """The project's phases as context for a model working on it. Empty without any."""
    if not project_id:
        return ""
    phases = db.list_phases(project_id)
    if not phases:
        return ""
    current = next((p for p in phases if p["status"] == "active"), None)
    lines = []
    for p in phases:
        dates = f" ({p['start_date'] or '…'} to {p['end_date'] or '…'})" if p["start_date"] or p["end_date"] else ""
        mark = {"done": "done", "active": "IN PROGRESS", "paused": "paused", "planned": "planned"}[p["status"]]
        lines.append(f"- {p['name']} [{mark}]{dates}" + (f": {p['goal']}" if p["goal"] and p is current else ""))
    focus = (f"You are working in the phase '{current['name']}'. Keep to its goal and don't start on later phases' work."
             if current else "")
    return ("This project is planned in phases, done in order:\n" + "\n".join(lines) + ("\n" + focus if focus else ""))


# ------------------------------------------------------------------- report

REPORT_INSTRUCTIONS = (
    "You write the overview at the top of a progress report on a project, for its owner. The report below lists "
    "the project's phases, what was done in each, what failed, and what is still to do. Write 3 to 6 sentences of "
    "plain text - no markdown, no headings, no lists: where things stand, the main things that got done, anything "
    "failed, stuck or running late, and what comes next. Address the owner as \"you\". Use only facts in the "
    "report - if something isn't there, don't guess."
)
REPORT_MAX_CHARS = 40_000
_STATUS_WORD = {"planned": "Planned", "active": "In progress", "paused": "Paused", "done": "Done"}
_MODE_WORD = {"manual": "on your command", "timeline": "by the timeline", "capacity": "when the usage limit allows"}


def _plain(text: Optional[str]) -> str:
    """Markdown emphasis, code marks and heading marks dropped, so it nests inside the report's own markdown."""
    text = re.sub(r"\*\*|__|`", "", (text or "").replace("\r", ""))
    return re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE).strip()


def _first_para(text: Optional[str], limit: int = 400) -> str:
    para = next((p.strip() for p in re.split(r"\n\s*\n", _plain(text)) if p.strip()), "")
    para = re.sub(r"\s*\n\s*", " ", para)
    if len(para) <= limit:
        return para
    cut = para[:limit]
    return cut[:max(cut.rfind(" "), limit // 2)].rstrip() + "…"


def _item(todo: dict[str, Any]) -> str:
    first = (todo["text"] or "").strip().split("\n")[0]
    return first if len(first) <= 140 else first[:138].rstrip() + "…"


def _what_was_done(todo: dict[str, Any]) -> str:
    """The chat's saved plain-words summary ("What was done:") if it has one, else the opening of Octopus's report."""
    thread = db.get_thread(todo["task_id"]) if todo.get("task_id") else []
    saved = (thread[0].get("summary") or "") if thread else ""
    m = re.search(r"What was done:\s*\n?(.*?)(?:\n\s*\n|\nWhere it stands:|$)", saved, re.S)
    if m and m.group(1).strip():
        return _first_para(m.group(1).replace("•", "").replace("\n", "; "))
    return _first_para(todo.get("task_result_summary"))


def _day(iso: str) -> str:
    """'Jun 3, 2026' for a calendar date or a UTC timestamp (as its local day)."""
    day = date.fromisoformat(iso) if len(iso) == 10 else datetime.fromisoformat(iso).astimezone(timeutil.local_tz()).date()
    return day.strftime("%b %-d, %Y")


def _phase_section(n: int, v: dict[str, Any], todos: list[dict[str, Any]]) -> list[str]:
    out = [f"## {n}. {v['name']} · {_STATUS_WORD.get(v['status'], v['status'])}", ""]
    facts = []
    if v.get("start_date") or v.get("end_date"):
        start, end = v.get("start_date"), v.get("end_date")
        when = f"{_day(start)} to {_day(end)}" if start and end else f"from {_day(start)}" if start else f"until {_day(end)}"
        if v["late"]:
            when += f" ({-v['days_left']} day{'s' * (v['days_left'] != -1)} late)"
        elif v["status"] != "done" and v["days_left"] is not None:
            when += " (due today)" if v["days_left"] == 0 else f" ({v['days_left']} day{'s' * (v['days_left'] != 1)} left)"
        facts.append(f"- Dates: {when}")
    if v.get("started_at"):
        facts.append(f"- Started: {_day(v['started_at'])}" + (f", finished {_day(v['finished_at'])}" if v.get("finished_at") else ""))
    facts.append(f"- Progress: {v['counts']['done']} of {v['total']} items done ({v['progress']}%)"
                 if v["total"] else "- Progress: no items in it")
    if v.get("blocked_note"):
        facts.append(f"- Holding: {v['blocked_note']}")
    out += facts + [""]
    if v.get("goal"):
        out += [f"**Goal:** {_plain(v['goal'])}", ""]

    groups: dict[str, list[str]] = {"done": [], "failed": [], "doing": [], "todo": [], "cancelled": []}
    for t in todos:
        s = todo_state(t)
        if s == "done":
            did = _what_was_done(t)
            groups["done"].append(f"- {_item(t)}" + (f" — {did}" if did else ""))
        elif s in FAILED:
            why = _first_para(t.get("task_error_message") or t.get("task_result_summary"), 300)
            groups["failed"].append(f"- {_item(t)}" + (f" — {why}" if why else ""))
        elif s in IN_FLIGHT:
            groups["doing"].append(f"- {_item(t)} ({'waiting for the usage limit' if s == 'rate_limited' else s})")
        elif s == "cancelled":
            groups["cancelled"].append(f"- {_item(t)}")
        else:
            groups["todo"].append(f"- {_item(t)}")
    for key, title in (("done", "Done"), ("failed", "Needs a look"), ("doing", "Under way"),
                       ("todo", "Still to do"), ("cancelled", "Cancelled")):
        if groups[key]:
            out += [f"### {title}", *groups[key], ""]
    return out


def _overview(markdown: str, project_id: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(overview, model_id, why_not) - a few sentences on the whole report from the cheapest model, if one is free."""
    model = router.cheapest_api_model()
    if not model:
        return None, None, "No model is free to write an overview right now."
    text = markdown if len(markdown) <= REPORT_MAX_CHARS else markdown[:REPORT_MAX_CHARS] + "\n\n[... rest of the report omitted ...]"
    try:
        out = providers.complete(model, [{"role": "user", "content": text}], REPORT_INSTRUCTIONS, 120)
    except providers.ProviderError as e:
        return None, None, f"The overview couldn't be written: {e}"
    db.log_usage("report", model["id"], out.input_tokens, out.output_tokens,
                 providers.estimate_cost(model, out.input_tokens, out.output_tokens), project_id=project_id)
    text = _plain(out.text)
    return (text or None), model["id"], (None if text else "The model returned an empty overview.")


def report(project_id: int, phase_ids: Optional[list[int]] = None, overview: bool = True,
           now: Optional[datetime] = None) -> dict[str, Any]:
    """A progress report on the project, or only on some of its phases, as markdown: where each phase stands,
    what was done in it (from each finished item's report), what failed and what is left. With overview, the
    cheapest free model writes a few sentences on top - the report still comes back without it if none can.
    Returns {title, markdown, filename, overview, model_id, overview_note, generated_at, phase_ids}."""
    project = db.get_project(project_id)
    if not project:
        raise PhaseError("Project not found")
    all_phases = db.list_phases(project_id)
    number = {p["id"]: i + 1 for i, p in enumerate(all_phases)}
    if phase_ids:
        wanted = set(phase_ids)
        if wanted - set(number):
            raise PhaseError("That phase belongs to a different project")
        chosen = [p for p in all_phases if p["id"] in wanted]
    else:
        chosen = all_phases
    whole = not phase_ids or len(chosen) == len(all_phases)
    now = now or timeutil.now()
    today = _today(now)
    todos = db.list_todos(project_id)
    views = [view(p, today) for p in chosen]
    by_phase = {v["id"]: [t for t in todos if t.get("phase_id") == v["id"]] for v in views}

    scope = "the whole project" if whole else ", ".join(f"phase {number[v['id']]} ({v['name']})" for v in views)
    title = f"{project['name']} · progress report" if whole else (
        f"{project['name']} · {views[0]['name']} report" if len(views) == 1 else f"{project['name']} · phases report")
    head = [f"# {title}", "", f"Generated {now.astimezone(timeutil.local_tz()).strftime('%b %-d, %Y at %H:%M')} · covers {scope}.", ""]

    glance = []
    if views:
        by_status = {s: sum(1 for v in views if v["status"] == s) for s in ("done", "active", "paused", "planned")}
        glance.append(f"- Phases: {by_status['done']} of {len(views)} done" + "".join(
            f" · {by_status[s]} {w}" for s, w in (("active", "in progress"), ("paused", "paused"), ("planned", "planned")) if by_status[s]))
        c = {k: sum(v["counts"][k] for v in views) for k in views[0]["counts"]}
        total = sum(v["total"] for v in views)
        glance.append(f"- Items: {c['done']} of {total} done" + "".join(
            f" · {n} {w}" for n, w in ((c["running"] + c["queued"] + c["rate_limited"], "under way"),
                                       (c["error"] + c["needs_review"], "need a look"), (c["pending"], "still to do")) if n))
        late = [f"{v['name']} ({-v['days_left']}d)" for v in views if v["late"]]
        if late:
            glance.append(f"- Running late: {', '.join(late)}")
    if whole:
        settings = db.get_project_settings(project_id)
        mode = mode_of(project_id)
        glance.append(f"- Autopilot: {_MODE_WORD[mode]}" + (f", holding work past {settings['autopilot_ceiling']}% of the weekly limit"
                                                             if mode != "manual" else ""))

    body: list[str] = ["## At a glance", *glance, ""] if glance else []
    for v in views:
        body += _phase_section(number[v["id"]], v, by_phase[v["id"]])
    if whole:
        loose = [t for t in todos if not t.get("phase_id") and t["status"] == "pending"]
        if loose:
            body += ["## Not in a phase yet", *[f"- {_item(t)}" for t in loose], ""]
    if not views:
        body += ["This project has no phases yet.", ""]

    markdown = "\n".join(head + body).rstrip() + "\n"
    text, model_id, note = _overview(markdown, project_id) if overview and views else (None, None, None)
    if text:
        markdown = "\n".join(head + ["## Overview", "", text, ""] + body).rstrip() + "\n"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] or "report"
    return {"title": title, "markdown": markdown, "filename": f"{slug}-{today.isoformat()}.md",
            "overview": text, "model_id": model_id, "overview_note": note,
            "generated_at": now.astimezone(timezone.utc).isoformat(), "phase_ids": [v["id"] for v in views]}
