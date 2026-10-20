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
- repeats: a meeting, reminder, task or event can repeat by a rule (see
  rule_of): every N days, every weekday, on chosen weekdays every N weeks, or
  every N months (same date, or the 2nd Tuesday / last Friday). It stays one
  row whose date is its first day (moved on to the first day that fits the
  rule); current() gives it as it stands now (date = its next day), and its
  alerts are kept per day ("2026-10-08:d1"), so each one gets its own.
  Deleting one day of a repeating item only skips that day (skip): the rule
  keeps a list of skipped days, which never happen and are never alerted.

Dates are local calendar days (assistant.timeutil's zone); nothing here
touches the network except briefing(), which reads today's Google Calendar
meetings when a calendar is connected.
"""

import calendar
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
    # A nudge at a time ("renew the cert"): pushed on the day, and an hour before if it has a time.
    "reminder":     {"label": "Reminder",     "icon": "🔔", "due": False, "remind": [0]},
}
STATUSES = ("open", "done", "cancelled")
# Kinds that can say who they're with (CRM contacts, see db.event_contacts).
WITH_PEOPLE = ("meeting",)
# How a meeting can repeat, and the kinds that can. A rule is {"freq", "every", ...}:
#   days      every N days (1 = daily, 2 = every other day)
#   weekdays  every Monday to Friday
#   weeks     every N weeks on `on` (weekday numbers, Monday = 0)
#   months    every N months, on the first day's date - or, with `nth` (1-4, -1 = last) and
#             on=[weekday], on the nth such weekday ("the 2nd Tuesday")
# Any rule may also carry `skip`: days (YYYY-MM-DD) it would fall on that were taken out one by one.
REPEAT_FREQS = ("days", "weekdays", "weeks", "months")
REPEAT_MAX_EVERY = {"days": 365, "weeks": 52, "months": 12}
REPEAT_NTH = (1, 2, 3, 4, -1)
REPEATS: dict[int, str] = {1: "Every day", 2: "Every other day"}      # the words for a days rule
# Deadlines, milestones, demos and presentations are one-off dates, so they never repeat.
REPEATABLE = ("meeting", "reminder", "task", "event")
MAX_SKIPS = 500
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SCAN_DAYS = 4800            # how far next_day looks for a day that fits (a rule always fits within 12 months)
# A timed item that isn't "due" (a meeting, a demo) also gets a push this long before it starts.
SOON_MINUTES = 60
# A deadline or milestone this close, with work still open, puts the project at risk.
AT_RISK_DAYS = 3
UPCOMING_DAYS = 7
MAX_REMIND_DAYS = 60
TITLE_MAX = 200
NOTES_MAX = 4000
# A meeting's length in minutes (what its Google Calendar invite spans; NULL = DEFAULT_DURATION),
# and the email addresses it can invite besides its contacts (see invites.py).
DEFAULT_DURATION = 60
MAX_DURATION = 24 * 60
MAX_GUESTS = 50
_EMAIL_RE = re.compile(r"^[^@\s<>(),;:\"]+@[^@\s<>(),;:\"]+\.[^@\s<>(),;:\"]+$")

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


def _first(ev: dict[str, Any]) -> date:
    """A repeating item's first day (current() moves `date` to the next one)."""
    return date.fromisoformat(ev.get("start_date") or ev["date"])


def _until(ev: dict[str, Any]) -> Optional[date]:
    return date.fromisoformat(ev["repeat_until"]) if ev.get("repeat_until") else None


def rule_of(ev: dict[str, Any]) -> Optional[dict[str, Any]]:
    """How an item repeats (None for a one-off). Older rows only have repeat_days: every that many days."""
    rule = ev.get("repeat_rule")
    if rule:
        return rule
    n = ev.get("repeat_days")
    return {"freq": "days", "every": n} if n else None


def _month_len(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def _hit(rule: dict[str, Any], first: date, d: date) -> bool:
    """Does the rule, counted from its first day, fall on d (d on or after first)? A skipped day never does."""
    if rule.get("skip") and d.isoformat() in rule["skip"]:
        return False
    freq, every = rule["freq"], rule.get("every", 1)
    if freq == "days":
        return (d - first).days % every == 0
    if freq == "weekdays":
        return d.weekday() < 5
    if freq == "weeks":
        weeks = ((d - timedelta(days=d.weekday())) - (first - timedelta(days=first.weekday()))).days // 7
        return d.weekday() in rule["on"] and weeks % every == 0
    if ((d.year - first.year) * 12 + d.month - first.month) % every:
        return False
    nth = rule.get("nth")
    if not nth:
        return d.day == min(first.day, _month_len(d))
    if d.weekday() != rule["on"][0]:
        return False
    return d.day + 7 > _month_len(d) if nth == -1 else (d.day - 1) // 7 + 1 == nth


def align(rule: dict[str, Any], day: date) -> date:
    """The first day on or after `day` that fits the rule (a rule on Mondays and Fridays,
    started on a Wednesday, starts on the Friday)."""
    loose = {**rule, "every": 1}
    for i in range(_SCAN_DAYS):
        d = day + timedelta(days=i)
        if _hit(loose, day, d):
            return d
    return day


def next_day(ev: dict[str, Any], day: date) -> Optional[date]:
    """The first day on or after `day` this item happens (None if it doesn't any more)."""
    first, rule = _first(ev), rule_of(ev)
    if not rule:
        return first if first >= day else None
    until, d = _until(ev), max(day, first)
    for _ in range(_SCAN_DAYS):
        if until and d > until:
            return None
        if _hit(rule, first, d):
            return d
        d += timedelta(days=1)
    return None


def days_between(ev: dict[str, Any], start: date, end: date) -> list[date]:
    """Every day from start to end (inclusive) this item happens on."""
    out, day = [], next_day(ev, start)
    while day and day <= end:
        out.append(day)
        day = next_day(ev, day + timedelta(days=1)) if rule_of(ev) else None
    return out


def _last_day(ev: dict[str, Any], rule: dict[str, Any]) -> date:
    """The last day a repeating item happens on (what it still shows once the repeats are over)."""
    first, d = _first(ev), _until(ev) or _first(ev)
    for _ in range(_SCAN_DAYS):
        if d <= first or _hit(rule, first, d):
            break
        d -= timedelta(days=1)
    return max(d, first)


def current(ev: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """A repeating item as it stands now: date is its next day from today on
    (its last one once the repeats are over), start_date its first. Anything
    else comes back as it is."""
    rule = rule_of(ev)
    if not rule:
        return ev
    day = next_day(ev, _now(now).date()) or _last_day(ev, rule)
    return {**ev, "date": day.isoformat(), "start_date": _first(ev).isoformat()}


def _bare(rule: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The rule without its skipped days - what decides which days it falls on in the first place."""
    if not rule or "skip" not in rule:
        return rule
    return {k: v for k, v in rule.items() if k != "skip"}


def skip_dates(ev: dict[str, Any]) -> list[str]:
    return list((rule_of(ev) or {}).get("skip") or [])


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _rule_words(rule: dict[str, Any], first: date) -> str:
    freq, n = rule["freq"], rule.get("every", 1)
    if freq == "days":
        return REPEATS.get(n, f"Every {n} days").lower()
    if freq == "weekdays":
        return "every weekday"
    if freq == "weeks":
        names = [_WEEKDAY_NAMES[i] for i in rule["on"]]
        if n == 1 and len(names) == 1:
            return f"every {names[0]}"
        lead = "every week" if n == 1 else "every other week" if n == 2 else f"every {n} weeks"
        return f"{lead} on " + ", ".join(x[:3] for x in names)
    lead = "every month" if n == 1 else "every other month" if n == 2 else f"every {n} months"
    nth = rule.get("nth")
    if not nth:
        return f"{lead} on the {_ordinal(first.day)}"
    return f"{lead} on the {'last' if nth == -1 else _ordinal(nth)} {_WEEKDAY_NAMES[rule['on'][0]]}"


def repeat_words(ev: dict[str, Any]) -> str:
    """'every other day' / 'every Monday until Thu Oct 8' ('' for a one-off)."""
    rule = rule_of(ev)
    if not rule:
        return ""
    until = _until(ev)
    return _rule_words(rule, _first(ev)) + (f" until {timeutil.fmt_day(until)}" if until else "")


def _order(ev: dict[str, Any]) -> tuple:
    return ev["date"], ev.get("time") or "", ev["id"]


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


def _int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise TimelineError(f"{what} must be a whole number")


def _clean_repeat(value: Any, days: Any, kind: str, first: str) -> Optional[dict[str, Any]]:
    """The repeat rule (None = a one-off) from `value`, a rule dict (see REPEAT_FREQS), or the older
    `days`, every that many days. REPEATABLE kinds only. `first` is the day it starts, which fills in the
    weekday a weekly or monthly rule doesn't name."""
    if value in (None, "", {}):
        if days in (None, "", 0, False):
            return None
        value = {"freq": "days", "every": days}
    if kind not in REPEATABLE:
        raise TimelineError("Only meetings, reminders, tasks and events can repeat")
    if not isinstance(value, dict) or value.get("freq") not in REPEAT_FREQS:
        raise TimelineError("repeat_rule.freq must be one of " + ", ".join(REPEAT_FREQS))
    freq = value["freq"]
    rule: dict[str, Any] = {"freq": freq, "every": 1}
    if freq != "weekdays":
        rule["every"] = _int(value.get("every", 1), "repeat_rule.every")
        if not 1 <= rule["every"] <= REPEAT_MAX_EVERY[freq]:
            raise TimelineError(f"repeat_rule.every must be 1 to {REPEAT_MAX_EVERY[freq]} for {freq}")
    anchor = date.fromisoformat(first)
    if freq == "weeks":
        on = sorted({_int(x, "repeat_rule.on") for x in (value.get("on") or [anchor.weekday()])})
        if not on or on[0] < 0 or on[-1] > 6:
            raise TimelineError("repeat_rule.on must be weekday numbers 0 (Monday) to 6 (Sunday)")
        rule["on"] = on
    elif freq == "months" and value.get("nth") not in (None, ""):
        nth = _int(value["nth"], "repeat_rule.nth")
        if nth not in REPEAT_NTH:
            raise TimelineError("repeat_rule.nth must be 1, 2, 3, 4 or -1 (the last)")
        wd = _int((value.get("on") or [anchor.weekday()])[0], "repeat_rule.on")
        if not 0 <= wd <= 6:
            raise TimelineError("repeat_rule.on must be a weekday number 0 (Monday) to 6 (Sunday)")
        rule.update(nth=nth, on=[wd])
    return rule


def _store(rule: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The columns a rule is kept in: a plain every-N-days one in repeat_days, the rest (and any
    rule with skipped days) in repeat_rule."""
    simple = bool(rule) and rule["freq"] == "days" and not rule.get("skip")
    return {"repeat_days": rule["every"] if simple else None, "repeat_rule": rule if rule and not simple else None}


def _clean_skips(value: Any) -> list[str]:
    """Skipped days: a list of YYYY-MM-DD, kept sorted and without repeats (the latest MAX_SKIPS)."""
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise TimelineError("skip_dates must be a list of YYYY-MM-DD days")
    try:
        days = sorted({_clean_date(v) for v in value})
    except TimelineError:
        raise TimelineError("skip_dates must be a list of YYYY-MM-DD days")
    return days[-MAX_SKIPS:]


def _clean_until(value: Any, first: str, repeat: Optional[dict[str, Any]]) -> Optional[str]:
    """The last day a repeat may fall on (None = no end); never before the first day."""
    if not repeat or value in (None, ""):
        return None
    try:
        v = _clean_date(value)
    except TimelineError as e:
        raise TimelineError(f"repeat_until: {e}")
    if v < first:
        raise TimelineError("repeat_until can't be before the first day")
    return v


def _clean_contacts(value: Any, kind: str) -> list[int]:
    """Who a meeting is with: CRM contact ids, each one existing. Only meetings have people -
    any other kind always gets an empty list."""
    if value is None or kind not in WITH_PEOPLE:
        return []
    if not isinstance(value, list):
        raise TimelineError("contact_ids must be a list of contacts")
    try:
        ids = list(dict.fromkeys(int(v) for v in value))
    except (TypeError, ValueError):
        raise TimelineError("contact_ids must be a list of contacts")
    for cid in ids:
        if not db.get_contact(cid):
            raise TimelineError("One of those contacts doesn't exist (any more)")
    return ids


def _clean_employees(value: Any, kind: str) -> list[int]:
    """Which employees attend a meeting: ids of employees that exist. Other kinds get an empty list."""
    if value is None or kind not in WITH_PEOPLE:
        return []
    if not isinstance(value, list):
        raise TimelineError("employee_ids must be a list of employees")
    try:
        ids = list(dict.fromkeys(int(v) for v in value))
    except (TypeError, ValueError):
        raise TimelineError("employee_ids must be a list of employees")
    for eid in ids:
        if not db.get_employee(eid):
            raise TimelineError("One of those employees doesn't exist (any more)")
    return ids


def _clean_guests(value: Any, kind: str) -> list[str]:
    """Email addresses a meeting invites besides its contacts: lower-cased, each one once.
    Only meetings have guests - any other kind always gets an empty list."""
    if value is None or kind not in WITH_PEOPLE:
        return []
    if not isinstance(value, list):
        raise TimelineError("guests must be a list of email addresses")
    out: list[str] = []
    for v in value:
        email = str(v or "").strip().lower()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            raise TimelineError(f"{email} isn't an email address")
        if email not in out:
            out.append(email)
    if len(out) > MAX_GUESTS:
        raise TimelineError(f"A meeting can invite at most {MAX_GUESTS} guests")
    return out


def _clean_duration(value: Any, kind: str) -> Optional[int]:
    """How long a meeting runs, in minutes (None = DEFAULT_DURATION). Other kinds don't have one."""
    if value is None or kind not in WITH_PEOPLE:
        return None
    minutes = _int(value, "duration")
    if not 5 <= minutes <= MAX_DURATION:
        raise TimelineError(f"duration must be between 5 and {MAX_DURATION} minutes")
    return minutes


def people_names(ev: dict[str, Any]) -> list[str]:
    """The names of who a meeting is with (contacts deleted since are skipped)."""
    people = [c["name"] for c in (db.get_contact(cid) for cid in ev.get("contact_ids") or []) if c]
    return people + [e["name"] for e in (db.get_employee(eid) for eid in ev.get("employee_ids") or []) if e]


def _with_words(ev: dict[str, Any]) -> str:
    """' with Priya and Sam' / ' with Priya, Sam and 2 more' ('' when nobody is picked)."""
    names = people_names(ev)
    if not names:
        return ""
    if len(names) > 3:
        return f" with {', '.join(names[:2])} and {len(names) - 2} more"
    return " with " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


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


def _tag(ev: dict[str, Any], key: str) -> str:
    """A repeating item's alert keys say which day they were for, so every repeat gets its own."""
    return f"{ev['date']}:{key}" if rule_of(ev) and ":" not in key else key


def _covered(ev: dict[str, Any], now: datetime) -> list[str]:
    """Every alert already behind this item as of now - marked sent when it is
    created or re-dated, so only the next ones go out. (ev as current() gives it.)"""
    if ev["status"] != "open":
        return []
    n = days_left(ev, now)
    keys = [f"d{d}" for d in remind_days(ev) if d >= n]
    start = _start(ev)
    if start and not is_due_kind(ev["kind"]) and start - now <= timedelta(minutes=SOON_MINUTES):
        keys.append("soon")
    if is_overdue(ev, now):
        keys.append("overdue")
    return [_tag(ev, k) for k in keys]


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
        lead = f"'{ev['title']}'{_with_words(ev)} starts at {timeutil.fmt_time(_start(ev))}."
    else:
        n = days_left(ev, now)
        title = f"{label} {'today' if n == 0 else 'tomorrow' if n == 1 else f'in {n} days'} · {where}"
        lead = f"'{ev['title']}'{_with_words(ev)} - {when_words(ev, now)}."
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
        ev = current(ev, now)
        key = _alert_key(ev, now)
        if not key or _tag(ev, key) in ev["alerts_sent"]:
            continue
        title, message = alert_text(ev, key, now)
        out.append({"event": ev, "key": key, "title": title, "message": message, "covered": _covered(ev, now)})
    return out


def mark_alerted(ev: dict[str, Any], keys: list[str]) -> None:
    """ev as pending_alerts gave it. A repeating item forgets the alerts of days already behind it."""
    sent = set(ev["alerts_sent"]) | {_tag(ev, k) for k in keys}
    if rule_of(ev):
        sent = {k for k in sent if ":" not in k or k.split(":", 1)[0] >= ev["date"]}
    db.update_project_event(ev["id"], alerts_sent=sorted(sent))


# ----------------------------------------------------------------- editing

def create(project_id: int, kind: str, title: str, date_: str, time_: Optional[str] = None,
           notes: Optional[str] = None, remind: Optional[list[int]] = None, source: str = "dashboard",
           now: Optional[datetime] = None, contact_ids: Optional[list[int]] = None, repeat_days: Optional[int] = None,
           repeat_until: Optional[str] = None, repeat_rule: Optional[dict[str, Any]] = None,
           guests: Optional[list[str]] = None, duration: Optional[int] = None,
           employee_ids: Optional[list[int]] = None) -> dict[str, Any]:
    """repeat_rule (see REPEAT_FREQS) or the older repeat_days, every that many days: None for a
    one-off. REPEATABLE kinds only. It repeats from date_ - moved on to the first day that fits the rule -
    up to repeat_until (None = no end). guests / duration: a meeting's other invitees and its
    length (see _clean_guests); sending the invite is invites.py's job."""
    project = db.get_project(project_id)
    if not project:
        raise TimelineError("Project not found")
    ev = {"project_id": project_id, "kind": _clean_kind(kind), "title": _clean_text(title, "title", TITLE_MAX, True),
          "date": _clean_date(date_), "time": _clean_time(time_), "notes": _clean_text(notes, "notes", NOTES_MAX, False),
          "remind_days": _clean_remind(remind), "status": "open"}
    rule = _clean_repeat(repeat_rule, repeat_days, ev["kind"], ev["date"])
    if rule:
        ev["date"] = align(rule, date.fromisoformat(ev["date"])).isoformat()
    ev.update(_store(rule))
    ev["repeat_until"] = _clean_until(repeat_until, ev["date"], rule)
    now = _now(now)
    return db.add_project_event(project_id, ev["kind"], ev["title"], ev["date"], ev["time"], ev["notes"],
                                ev["remind_days"], _covered(current(ev, now), now), source,
                                _clean_contacts(contact_ids, ev["kind"]), ev["repeat_days"], ev["repeat_until"], ev["repeat_rule"],
                                _clean_guests(guests, ev["kind"]), _clean_duration(duration, ev["kind"]),
                                _clean_employees(employee_ids, ev["kind"]))


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
    kind = fields.get("kind", ev["kind"])
    # How it repeats: changed when sent, and dropped when it stops being repeatable. A new rule (or date)
    # moves the first day on to the first one that fits it. Skipped days stay (a new rule keeps them -
    # ones it doesn't fall on simply never matter) unless skip_dates is sent.
    day = fields.get("date", ev["date"])
    asked = "repeat_rule" in changes or "repeat_days" in changes
    skips = _clean_skips(changes["skip_dates"]) if "skip_dates" in changes else skip_dates(ev)
    if kind in REPEATABLE:
        rule = _clean_repeat(changes.get("repeat_rule"), changes.get("repeat_days"), kind, day) if asked else _bare(rule_of(ev))
        if rule and ("date" in fields or rule != _bare(rule_of(ev))):
            day = align(rule, date.fromisoformat(day)).isoformat()
            if day != ev["date"]:
                fields["date"] = day
        until = _clean_until(changes["repeat_until"] if "repeat_until" in changes else ev.get("repeat_until"), day, rule)
    else:
        _clean_repeat(changes.get("repeat_rule"), changes.get("repeat_days"), kind, day)    # asking a deadline to repeat is an error
        rule, until = None, None
    if rule and skips:
        rule = {**rule, "skip": skips}
    elif skips and "skip_dates" in changes:
        raise TimelineError("Only a repeating item can skip a day - delete it instead")
    for k, v in {**_store(rule), "repeat_until": until}.items():
        if v != ev.get(k):
            fields[k] = v
    # A new date, time, kind, repeat or reminder list - or reopening it - starts the alerts over from here.
    if any(k in fields and fields[k] != ev.get(k) for k in ("date", "time", "kind", "remind_days", "status", "repeat_days", "repeat_rule", "repeat_until")):
        fields["alerts_sent"] = _covered(current({**ev, **fields}, now), _now(now))
    # Who it's with: replaced when sent, and dropped when it stops being a meeting.
    contact_ids = None
    if "contact_ids" in changes:
        contact_ids = _clean_contacts(changes["contact_ids"] or [], kind)
    elif kind not in WITH_PEOPLE and ev.get("contact_ids"):
        contact_ids = []
    employee_ids = None
    if "employee_ids" in changes:
        employee_ids = _clean_employees(changes["employee_ids"] or [], kind)
    elif kind not in WITH_PEOPLE and ev.get("employee_ids"):
        employee_ids = []
    # Guests and length: likewise, and only a meeting keeps them.
    for k, clean, empty in (("guests", _clean_guests, []), ("duration", _clean_duration, None)):
        v = clean(changes[k], kind) if k in changes else (ev.get(k) if kind in WITH_PEOPLE else empty)
        if v != ev.get(k) and (v or ev.get(k)):
            fields[k] = v if v or k != "guests" else None
    if not fields and contact_ids is None and employee_ids is None:
        return ev
    return db.update_project_event(event_id, contact_ids=contact_ids, employee_ids=employee_ids, **fields)


def skip(event_id: int, day: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Take one day out of a repeating item ("delete just this one"): the rest of the
    series stays as it is. That day is never shown or alerted again."""
    ev = db.get_project_event(event_id)
    if not ev:
        raise TimelineError("Timeline item not found")
    rule = rule_of(ev)
    if not rule:
        raise TimelineError("Only a repeating item can skip a day - delete it instead")
    d = date.fromisoformat(_clean_date(day))
    if d.isoformat() in skip_dates(ev):
        return ev
    until = _until(ev)
    if d < _first(ev) or (until and d > until) or not _hit(rule, _first(ev), d):
        raise TimelineError("It doesn't happen on that day")
    return update(event_id, {"skip_dates": skip_dates(ev) + [d.isoformat()]}, now)


# ---------------------------------------------------------------- queries

def view(ev: dict[str, Any], now: Optional[datetime] = None, events: Optional[list[dict[str, Any]]] = None,
         todos: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """An item as the dashboard shows it: the row plus its countdown and, for
    something with work leading up to it, what is still open. A repeating
    item's date is its next day; start_date is always its first."""
    ev = current(ev, now)
    kind = KINDS.get(ev["kind"], KINDS["event"])
    out = {**ev, "kind_label": kind["label"], "icon": kind["icon"], "due_kind": kind["due"],
           "days_left": days_left(ev, now), "when": when_words(ev, now), "overdue": is_overdue(ev, now),
           "start_date": ev.get("start_date") or ev["date"], "first_date": ev.get("start_date") or ev["date"],
           "repeat": repeat_words(ev), "repeat_rule": rule_of(ev), "skip_dates": skip_dates(ev)}
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
    evs = sorted((current(e, now) for e in db.list_project_events(project_id)), key=_order)
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
    evs = sorted((current(e, now) for e in db.list_project_events(project_id, end=horizon, statuses=["open"])), key=_order)
    evs = [e for e in evs if is_overdue(e, now) or days_left(e, now) >= 0][:10]
    if not evs:
        return ""
    lines = []
    for e in evs:
        label = KINDS.get(e["kind"], KINDS["event"])["label"]
        state = "OVERDUE, was due " if is_overdue(e, now) else ""
        at = f" {e['time']}" if e.get("time") else ""
        again = f" (repeats {repeat_words(e)})" if rule_of(e) else ""
        lines.append(f"- {state}{timeutil.fmt_day(_day(e))}{at}: {label} - {e['title']}{_with_words(e)}{again}")
    return (f"This project has a timeline (today is {timeutil.fmt_long_date(now.date())}). Keep these dates in mind: "
            "put work that unblocks the nearest deadline first, and say so clearly if something you find puts a date at risk.\n"
            + "\n".join(lines))


# ---------------------------------------------------------------- briefing

def _greeting(now: datetime) -> str:
    h = now.hour
    return "Good morning" if 5 <= h < 12 else "Good afternoon" if 12 <= h < 17 else "Good evening" if 17 <= h < 22 else "Hello"


def _calendar_today(now: datetime) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Today's meetings from the connected calendars (all of them, when there
    are several): (events, error). (None, None) when no calendar is connected."""
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
    evs = sorted(views(db.list_project_events(end=week_end, statuses=["open"]), now), key=_order)
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
    return f"• {e['kind_label']}: {e['title']}{_with_words(e)} ({e['project_name']}){when}"


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
