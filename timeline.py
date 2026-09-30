"""
Project calendar and timelines: deadlines, milestones, meetings, client
presentations, demos and dated tasks, one row each in db.project_events.

This module owns the rules; db.py only stores rows.

- create / update / set_status: validation, plus alert bookkeeping - an item
  starts with every reminder it is already inside of marked as sent, so
  adding "demo in 2 days" doesn't immediately push "demo in 3 days".
- pending_alerts: what the scheduler should push right now (N days ahead of
  a date, an hour before a timed meeting, once when something goes overdue),
  each with what is still open before it.
- health: is a project on track, at risk (a deadline is close and work is
  still open) or overdue.
- briefing: the "Good morning" summary - today's items and meetings,
  overdue work, the week ahead, approvals and failures waiting on you.
- prompt_for: the timeline as context for any model working on the project,
  so it keeps the dates in mind.

Dates are local calendar days (assistant.timeutil's zone); nothing here
touches the network except briefing(), which reads today's Google Calendar
meetings when a calendar is connected.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional

import db
from assistant import timeutil

# due: it has to be finished by its date and goes overdue when it passes.
# The rest happen at their date and are simply over afterwards.
# remind: how many days ahead the default alerts go out (0 = on the day).
KINDS: dict[str, dict[str, Any]] = {
    "deadline":     {"label": "Deadline",     "icon": "⏰", "due": True,  "remind": [7, 3, 1, 0]},
    "milestone":    {"label": "Milestone",    "icon": "🏁", "due": True,  "remind": [3, 1, 0]},
    "task":         {"label": "Task",         "icon": "☑️", "due": True,  "remind": [1, 0]},
    "presentation": {"label": "Presentation", "icon": "📊", "due": False, "remind": [3, 1, 0]},
    "demo":         {"label": "Demo",         "icon": "🎬", "due": False, "remind": [3, 1, 0]},
    "meeting":      {"label": "Meeting",      "icon": "👥", "due": False, "remind": [1, 0]},
    "event":        {"label": "Event",        "icon": "📌", "due": False, "remind": [1, 0]},
}
STATUSES = ("open", "done", "cancelled")
# A timed item that isn't "due" (a meeting, a demo) also gets a push this long before it starts.
SOON_MINUTES = 60
# A deadline or milestone this close, with work still open, puts the project at risk.
AT_RISK_DAYS = 3
UPCOMING_DAYS = 7
MAX_REMIND_DAYS = 60
TITLE_MAX = 200
NOTES_MAX = 4000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class TimelineError(ValueError):
    pass


# ------------------------------------------------------------------- basics

def _now(now: Optional[datetime] = None) -> datetime:
    return (now or timeutil.now()).astimezone(timeutil.local_tz())


def today(now: Optional[datetime] = None) -> date:
    return _now(now).date()


def _day(ev: dict[str, Any]) -> date:
    return date.fromisoformat(ev["date"])


def _start(ev: dict[str, Any]) -> Optional[datetime]:
    """When a timed item starts (local), None for an all-day one."""
    if not ev.get("time"):
        return None
    h, m = (int(x) for x in ev["time"].split(":"))
    return timeutil.at(_day(ev), time(h, m))


def is_due_kind(kind: str) -> bool:
    return KINDS.get(kind, KINDS["event"])["due"]


def remind_days(ev: dict[str, Any]) -> list[int]:
    got = ev.get("remind_days")
    days = got if isinstance(got, list) else KINDS.get(ev["kind"], KINDS["event"])["remind"]
    return sorted({int(d) for d in days if 0 <= int(d) <= MAX_REMIND_DAYS}, reverse=True)


def days_left(ev: dict[str, Any], now: Optional[datetime] = None) -> int:
    return (_day(ev) - _now(now).date()).days


def is_overdue(ev: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """An open deadline / milestone / task whose date (or time, if it has one) has passed."""
    if ev["status"] != "open" or not is_due_kind(ev["kind"]):
        return False
    start = _start(ev)
    return _now(now) > start if start else days_left(ev, now) < 0


def when_words(ev: dict[str, Any], now: Optional[datetime] = None) -> str:
    """'today at 3:00 PM', 'tomorrow', 'in 3 days (Tue Sep 29)', '2 days ago (Thu Sep 24)'."""
    n = days_left(ev, now)
    at = f" at {timeutil.fmt_time(_start(ev))}" if ev.get("time") else ""
    day = timeutil.fmt_day(_day(ev))
    if n == 0:
        return "today" + at
    if n == 1:
        return "tomorrow" + at
    if n == -1:
        return f"yesterday ({day})"
    if n < 0:
        return f"{-n} days ago ({day})"
    return f"in {n} days ({day}{at})"


# --------------------------------------------------------------- validation

def _clean_date(value: Any) -> str:
    v = str(value or "").strip()
    if not _DATE_RE.match(v):
        raise TimelineError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise TimelineError(f"{v} isn't a real date")
    return v


def _clean_time(value: Any) -> Optional[str]:
    v = str(value or "").strip()
    if not v:
        return None
    if not _TIME_RE.match(v):
        raise TimelineError("time must be HH:MM (24-hour)")
    return v


def _clean_remind(value: Any) -> Optional[list[int]]:
    if value is None:
        return None
    try:
        days = sorted({int(d) for d in value}, reverse=True)
    except (TypeError, ValueError):
        raise TimelineError("remind_days must be a list of whole days")
    if any(d < 0 or d > MAX_REMIND_DAYS for d in days):
        raise TimelineError(f"remind_days must be between 0 and {MAX_REMIND_DAYS}")
    return days


def _clean_text(value: Any, field: str, limit: int, required: bool) -> Optional[str]:
    v = re.sub(r"\s+", " ", str(value or "")).strip() if field == "title" else str(value or "").strip()
    if required and not v:
        raise TimelineError(f"{field} is required")
    if len(v) > limit:
        raise TimelineError(f"{field} is too long (max {limit} characters)")
    return v or None


def _clean_kind(kind: Any) -> str:
    k = str(kind or "").strip().lower()
    if k not in KINDS:
        raise TimelineError(f"kind must be one of {', '.join(KINDS)}")
    return k


# ------------------------------------------------------------------ alerts

def _alert_key(ev: dict[str, Any], now: datetime) -> Optional[str]:
    """The one alert this item is due for right now (it may already have been sent)."""
    if ev["status"] != "open":
        return None
    if is_overdue(ev, now):
        return "overdue"
    start = _start(ev)
    if start and not is_due_kind(ev["kind"]):
        if start <= now:
            return None                                   # it has started - nothing left to warn about
        if start - now <= timedelta(minutes=SOON_MINUTES):
            return "soon"
    n = days_left(ev, now)
    leads = [d for d in remind_days(ev) if d >= n]
    return f"d{min(leads)}" if leads and n >= 0 else None


def _covered(ev: dict[str, Any], now: datetime) -> list[str]:
    """Every alert already behind this item as of now - marked sent when it is
    created or re-dated, so only the next ones go out."""
    if ev["status"] != "open":
        return []
    n = days_left(ev, now)
    keys = [f"d{d}" for d in remind_days(ev) if d >= n]
    start = _start(ev)
    if start and not is_due_kind(ev["kind"]) and start - now <= timedelta(minutes=SOON_MINUTES):
        keys.append("soon")
    if is_overdue(ev, now):
        keys.append("overdue")
    return keys


def outstanding(ev: dict[str, Any], events: Optional[list[dict[str, Any]]] = None,
                todos: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """What still has to happen before this item: the project's open
    deadlines / milestones / tasks dated on or before it, and its backlog
    items that haven't been run."""
    if events is None:
        events = db.list_project_events(ev["project_id"], statuses=["open"])
    if todos is None:
        todos = db.list_todos(ev["project_id"])
    before = [e for e in events if e["id"] != ev["id"] and e["status"] == "open" and is_due_kind(e["kind"])
              and e["date"] <= ev["date"]]
    backlog = [t for t in todos if t.get("status") == "pending"]
    active = [t for t in todos if t.get("status") in ("queued", "rate_limited")]
    return {"items": [{"id": e["id"], "title": e["title"], "date": e["date"], "kind": e["kind"]} for e in before],
            "backlog": len(backlog), "backlog_titles": [t["text"][:80] for t in backlog[:3]], "in_queue": len(active)}


def outstanding_words(out: dict[str, Any]) -> str:
    """'Still open: Fix login, Write docs, plus 4 in the backlog.' ('' when nothing is.)"""
    parts = []
    if out["items"]:
        names = ", ".join(i["title"] for i in out["items"][:3])
        more = len(out["items"]) - 3
        parts.append(names + (f" and {more} more" if more > 0 else ""))
    if out["backlog"]:
        parts.append(f"{out['backlog']} backlog item{'s' if out['backlog'] != 1 else ''} not started")
    if out.get("in_queue"):
        parts.append(f"{out['in_queue']} in the queue")
    return ("Still open: " + "; ".join(parts) + ".") if parts else ""


def alert_text(ev: dict[str, Any], key: str, now: Optional[datetime] = None,
               out: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    """(title, message) for one push."""
    label = KINDS.get(ev["kind"], KINDS["event"])["label"]
    where = ev.get("project_name") or ""
    if key == "overdue":
        title = f"Overdue · {where}"
        lead = f"{label} '{ev['title']}' was due {when_words(ev, now)}."
    elif key == "soon":
        title = f"{label} in {SOON_MINUTES} min · {where}"
        lead = f"'{ev['title']}' starts at {timeutil.fmt_time(_start(ev))}."
    else:
        n = days_left(ev, now)
        title = f"{label} {'today' if n == 0 else 'tomorrow' if n == 1 else f'in {n} days'} · {where}"
        lead = f"'{ev['title']}' - {when_words(ev, now)}."
    tail = ""
    if is_due_kind(ev["kind"]) or ev["kind"] in ("presentation", "demo"):
        tail = outstanding_words(out if out is not None else outstanding(ev))
    return title, (lead + (" " + tail if tail else ""))[:300]


def pending_alerts(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """[{event, key, title, message, covered}] - one per item that has an
    alert due and not yet sent. `covered` is every key to mark sent with it
    (a missed "in 3 days" isn't sent late once "tomorrow" is due)."""
    now = _now(now)
    horizon = (now.date() + timedelta(days=MAX_REMIND_DAYS)).isoformat()
    out = []
    for ev in db.list_project_events(end=horizon, statuses=["open"]):
        key = _alert_key(ev, now)
        if not key or key in ev["alerts_sent"]:
            continue
        title, message = alert_text(ev, key, now)
        out.append({"event": ev, "key": key, "title": title, "message": message, "covered": _covered(ev, now)})
    return out


def mark_alerted(ev: dict[str, Any], keys: list[str]) -> None:
    db.update_project_event(ev["id"], alerts_sent=sorted(set(ev["alerts_sent"]) | set(keys)))


# ----------------------------------------------------------------- editing

def create(project_id: int, kind: str, title: str, date_: str, time_: Optional[str] = None,
           notes: Optional[str] = None, remind: Optional[list[int]] = None, source: str = "dashboard",
           now: Optional[datetime] = None) -> dict[str, Any]:
    project = db.get_project(project_id)
    if not project:
        raise TimelineError("Project not found")
    ev = {"project_id": project_id, "kind": _clean_kind(kind), "title": _clean_text(title, "title", TITLE_MAX, True),
          "date": _clean_date(date_), "time": _clean_time(time_), "notes": _clean_text(notes, "notes", NOTES_MAX, False),
          "remind_days": _clean_remind(remind), "status": "open"}
    return db.add_project_event(project_id, ev["kind"], ev["title"], ev["date"], ev["time"], ev["notes"],
                                ev["remind_days"], _covered(ev, _now(now)), source)


def update(event_id: int, changes: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    ev = db.get_project_event(event_id)
    if not ev:
        raise TimelineError("Timeline item not found")
    fields: dict[str, Any] = {}
    if "project_id" in changes and changes["project_id"] is not None:
        if not db.get_project(int(changes["project_id"])):
            raise TimelineError("Project not found")
        fields["project_id"] = int(changes["project_id"])
    if "kind" in changes and changes["kind"] is not None:
        fields["kind"] = _clean_kind(changes["kind"])
    if "title" in changes and changes["title"] is not None:
        fields["title"] = _clean_text(changes["title"], "title", TITLE_MAX, True)
    if "notes" in changes:
        fields["notes"] = _clean_text(changes["notes"], "notes", NOTES_MAX, False)
    if "date" in changes and changes["date"] is not None:
        fields["date"] = _clean_date(changes["date"])
    if "time" in changes:
        fields["time"] = _clean_time(changes["time"])
    if "remind_days" in changes:
        fields["remind_days"] = _clean_remind(changes["remind_days"])
    if "status" in changes and changes["status"] is not None:
        if changes["status"] not in STATUSES:
            raise TimelineError(f"status must be one of {', '.join(STATUSES)}")
        fields["status"] = changes["status"]
        if fields["status"] != ev["status"]:
            fields["done_at"] = datetime.now(timezone.utc).isoformat() if fields["status"] == "done" else None
    # A new date, time, kind or reminder list - or reopening it - starts the alerts over from here.
    if any(k in fields and fields[k] != ev.get(k) for k in ("date", "time", "kind", "remind_days", "status")):
        fields["alerts_sent"] = _covered({**ev, **fields}, _now(now))
    return db.update_project_event(event_id, **fields) if fields else ev


# ---------------------------------------------------------------- queries

def view(ev: dict[str, Any], now: Optional[datetime] = None, events: Optional[list[dict[str, Any]]] = None,
         todos: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """An item as the dashboard shows it: the row plus its countdown and, for
    something with work leading up to it, what is still open."""
    kind = KINDS.get(ev["kind"], KINDS["event"])
    out = {**ev, "kind_label": kind["label"], "icon": kind["icon"], "due_kind": kind["due"],
           "days_left": days_left(ev, now), "when": when_words(ev, now), "overdue": is_overdue(ev, now)}
    if ev["status"] == "open" and (kind["due"] or ev["kind"] in ("presentation", "demo")):
        out["outstanding"] = outstanding(ev, events, todos)
    return out


def views(events: list[dict[str, Any]], now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """view() for many items, reading each project's open items and backlog once."""
    open_by: dict[int, list[dict[str, Any]]] = {}
    todos_by: dict[int, list[dict[str, Any]]] = {}
    for pid in {e["project_id"] for e in events if e["status"] == "open"}:
        open_by[pid] = db.list_project_events(pid, statuses=["open"])
        todos_by[pid] = db.list_todos(pid)
    return [view(e, now, open_by.get(e["project_id"]), todos_by.get(e["project_id"])) for e in events]


def health(project_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """{state, label, reason, next, overdue} - state: none (no timeline) |
    on_track | at_risk | overdue | done (nothing left open)."""
    now = _now(now)
    evs = db.list_project_events(project_id)
    open_evs = [e for e in evs if e["status"] == "open"]
    if not evs:
        return {"state": "none", "label": "No timeline", "reason": "Nothing is scheduled for this project.", "next": None, "overdue": 0}
    overdue = [e for e in open_evs if is_overdue(e, now)]
    upcoming = [e for e in open_evs if not is_overdue(e, now) and days_left(e, now) >= 0]
    nxt = next((e for e in upcoming if is_due_kind(e["kind"]) or e["kind"] in ("presentation", "demo")), upcoming[0] if upcoming else None)
    nxt_view = view(nxt, now) if nxt else None
    if overdue:
        worst = overdue[0]
        reason = f"'{worst['title']}' was due {when_words(worst, now)}" + (f" (+{len(overdue) - 1} more overdue)" if len(overdue) > 1 else "")
        return {"state": "overdue", "label": "Overdue", "reason": reason + ".", "next": nxt_view, "overdue": len(overdue)}
    if not open_evs:
        return {"state": "done", "label": "All done", "reason": "Everything on the timeline is finished.", "next": None, "overdue": 0}
    near = [e for e in upcoming if (is_due_kind(e["kind"]) or e["kind"] in ("presentation", "demo")) and days_left(e, now) <= AT_RISK_DAYS]
    for e in near:
        o = outstanding(e, open_evs)
        if o["items"] or o["backlog"]:
            return {"state": "at_risk", "label": "At risk",
                    "reason": f"'{e['title']}' is {when_words(e, now)}. {outstanding_words(o)}", "next": view(e, now), "overdue": 0}
    reason = f"Next: '{nxt['title']}' {when_words(nxt, now)}." if nxt else "Nothing coming up."
    return {"state": "on_track", "label": "On track", "reason": reason, "next": nxt_view, "overdue": 0}


def prompt_for(project_id: Optional[int], now: Optional[datetime] = None) -> str:
    """The project's timeline as context for a model working on it. Empty
    when there's nothing open."""
    if not project_id:
        return ""
    now = _now(now)
    horizon = (now.date() + timedelta(days=45)).isoformat()
    evs = [e for e in db.list_project_events(project_id, end=horizon, statuses=["open"])
           if is_overdue(e, now) or days_left(e, now) >= 0][:10]
    if not evs:
        return ""
    lines = []
    for e in evs:
        label = KINDS.get(e["kind"], KINDS["event"])["label"]
        state = "OVERDUE, was due " if is_overdue(e, now) else ""
        at = f" {e['time']}" if e.get("time") else ""
        lines.append(f"- {state}{timeutil.fmt_day(_day(e))}{at}: {label} - {e['title']}")
    return (f"This project has a timeline (today is {timeutil.fmt_long_date(now.date())}). Keep these dates in mind: "
            "put work that unblocks the nearest deadline first, and say so clearly if something you find puts a date at risk.\n"
            + "\n".join(lines))


# ---------------------------------------------------------------- briefing

def _greeting(now: datetime) -> str:
    h = now.hour
    return "Good morning" if 5 <= h < 12 else "Good afternoon" if 12 <= h < 17 else "Good evening" if 17 <= h < 22 else "Hello"


def _calendar_today(now: datetime) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Today's meetings from the connected calendar: (events, error). (None,
    None) when no calendar is connected."""
    try:
        from assistant import connections, providers
        if not connections.connected_with("calendar"):
            return None, None
        provider = providers.calendar()
        midnight = timeutil.at(now.date(), tz=now.tzinfo)
        events = provider.list_events(midnight, midnight + timedelta(days=1), max_results=30)
        return [{"id": e["id"], "title": e["title"], "start": e["start"], "end": e["end"], "all_day": e["all_day"],
                 "location": e.get("location"), "link": e.get("link")} for e in events], None
    except Exception as e:               # the briefing is still worth having without it
        return [], str(e)[:200]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def briefing(now: Optional[datetime] = None, calendar: bool = True,
             calendar_fn: Optional[Callable[[datetime], tuple]] = None) -> dict[str, Any]:
    """Everything worth knowing at the start of the day, structured for the
    dashboard's Good morning sheet, plus `text` (chat / push) and `spoken`
    (voice) renderings of the same."""
    now = _now(now)
    today = now.date()
    week_end = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
    evs = views([e for e in db.list_project_events(end=week_end, statuses=["open"])], now)
    todays = [e for e in evs if e["days_left"] == 0 and not e["overdue"]]
    overdue = [e for e in evs if e["overdue"]]
    upcoming = [e for e in evs if 0 < e["days_left"] <= UPCOMING_DAYS]
    meetings, cal_error = (None, None)
    if calendar:
        meetings, cal_error = (calendar_fn or _calendar_today)(now)

    approvals = [{"id": a["id"], "task_id": a["task_id"], "title": a.get("task_title") or (a.get("task_prompt") or "")[:60],
                  "project_name": (db.get_project(a["project_id"]) or {}).get("name"), "tool": a["tool_name"]}
                 for a in db.list_pending_approvals()]
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()   # task times are stored in UTC
    attention = [{"task_id": t["id"], "title": t.get("title") or t["prompt"][:60], "status": t["status"],
                  "project_name": (db.get_project(t["project_id"]) or {}).get("name")}
                 for t in db.list_tasks(statuses=["error", "needs_review"])
                 if (t.get("finished_at") or t["created_at"]) >= week_ago and not t.get("thread_id")][-8:]
    reminders = []
    for r in db.list_reminders(["pending"], 50):
        try:
            due = timeutil.parse_iso(r["due_at"]).astimezone(now.tzinfo)
        except ValueError:
            continue
        if due.date() == today:
            reminders.append({"id": r["id"], "text": r["text"], "due_at": r["due_at"], "time": timeutil.fmt_time(due)})
    q = db.queue_status()
    queue = {"running": len(q.get("running_all") or []), "queued": len(db.list_tasks(statuses=["queued"]))}
    risk = []
    for p in db.list_projects(active_only=True):
        if db.is_general_project(p):
            continue
        h = health(p["id"], now)
        if h["state"] in ("at_risk", "overdue"):
            risk.append({"project_id": p["id"], "name": p["name"], "state": h["state"], "label": h["label"], "reason": h["reason"]})

    out = {"date": today.isoformat(), "day": f"{today.strftime('%A')}, {timeutil.fmt_day(today)[4:]}", "greeting": _greeting(now),
           "today": todays, "overdue": overdue, "upcoming": upcoming, "meetings": meetings,
           "calendar_connected": meetings is not None, "calendar_error": cal_error,
           "approvals": approvals, "attention": attention, "reminders": reminders, "queue": queue, "at_risk": risk}
    out["headline"] = headline(out)
    out["text"], out["spoken"] = render(out)
    return out


def headline(b: dict[str, Any]) -> str:
    """One line for a push: '1 overdue · 2 today · 3 this week · 2 approvals waiting'."""
    parts = []
    if b["overdue"]:
        parts.append(f"{len(b['overdue'])} overdue")
    n_today = len(b["today"]) + len([m for m in (b["meetings"] or []) if not m["all_day"]])
    if n_today:
        parts.append(f"{n_today} today")
    if b["upcoming"]:
        parts.append(f"{len(b['upcoming'])} coming up this week")
    if b["approvals"]:
        parts.append(_plural(len(b["approvals"]), "approval") + " waiting")
    if b["attention"]:
        parts.append(f"{len(b['attention'])} to review")
    return " · ".join(parts) if parts else "Nothing urgent - a clear day."


def _line(e: dict[str, Any], with_when: bool = True) -> str:
    when = f" - {e['when']}" if with_when else (f" at {timeutil.fmt_time(_start(e))}" if e.get("time") else "")
    return f"• {e['kind_label']}: {e['title']} ({e['project_name']}){when}"


def render(b: dict[str, Any]) -> tuple[str, str]:
    """(text, spoken) for a briefing."""
    lines = [f"{b['greeting']}! Here's {b['day']}."]
    if b["overdue"]:
        lines += ["", "Overdue:"] + [_line(e) for e in b["overdue"]]
    today = [_line(e, False) for e in b["today"]]
    for m in b["meetings"] or []:
        at = "all day" if m["all_day"] else timeutil.fmt_time(timeutil.parse_iso(m["start"]))
        today.append(f"• {at}: {m['title']} (calendar)")
    for r in b["reminders"]:
        today.append(f"• {r['time']}: reminder - {r['text']}")
    if today:
        lines += ["", "Today:"] + today
    if b["upcoming"]:
        lines += ["", "Coming up this week:"]
        for e in b["upcoming"]:
            tail = outstanding_words(e["outstanding"]) if e.get("outstanding") else ""
            lines.append(_line(e) + (f". {tail}" if tail else ""))
    needs = []
    if b["approvals"]:
        needs.append(_plural(len(b["approvals"]), "approval") + " waiting on you")
    if b["attention"]:
        needs.append(_plural(len(b["attention"]), "task") + " failed or need a review")
    if needs:
        lines += ["", "Needs you: " + ", ".join(needs) + "."]
    if b["at_risk"]:
        lines += ["", "Watch:"] + [f"• {r['name']} is {r['label'].lower()}: {r['reason']}" for r in b["at_risk"]]
    qn = b["queue"]
    if qn["running"] or qn["queued"]:
        lines += ["", f"Queue: {qn['running']} running, {qn['queued']} waiting."]
    if b["calendar_error"]:
        lines += ["", "(Couldn't read your calendar: " + b["calendar_error"] + ")"]
    if len(lines) == 1:
        lines += ["", "Nothing on the timeline, no meetings and nothing waiting on you. A clear day."]

    said = [f"{b['greeting']}."]
    n_meet = len([m for m in (b["meetings"] or []) if not m["all_day"]])
    today_bits = []
    if b["today"]:
        today_bits.append(_plural(len(b["today"]), "project item"))
    if n_meet:
        today_bits.append(_plural(n_meet, "meeting"))
    if today_bits:
        said.append("Today you have " + " and ".join(today_bits) + ".")
    if b["overdue"]:
        said.append(f"{_plural(len(b['overdue']), 'item')} {'is' if len(b['overdue']) == 1 else 'are'} overdue.")
    if b["upcoming"]:
        said.append(f"{len(b['upcoming'])} more coming up this week.")
    if b["approvals"]:
        said.append(f"{_plural(len(b['approvals']), 'approval')} {'is' if len(b['approvals']) == 1 else 'are'} waiting.")
    if len(said) == 1:
        said.append("It's a clear day.")
    return "\n".join(lines), " ".join(said)
