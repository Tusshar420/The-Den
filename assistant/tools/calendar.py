"""
Calendar tool: read, check availability, find a free slot, create events and
meetings, move and cancel them. Works with any CalendarProvider.

Reading and a personal event with nobody invited run on their own. Anything
that emails other people (an invitation, a change, a cancellation) also
needs CALENDAR_INVITE, and changing or cancelling an event needs
CALENDAR_UPDATE / CALENDAR_DELETE - all of which ask first by default.
"""

from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from assistant import mailutil, timeutil
from assistant.tools import ActionSpec, Prepared, Tool, ToolError, preview, register

WORK_START, WORK_END = time(9, 0), time(18, 0)
WORK_DAYS = (0, 1, 2, 3, 4)
SLOT_STEP_MINUTES = 30
MAX_SLOTS = 6
# How often a new event can repeat: days apart -> label (same choices as the project timeline, timeline.REPEATS).
REPEATS = {2: "Every other day"}
_REPEAT_WORDS = {"every other day": 2, "every 2 days": 2, "every second day": 2, "alternate days": 2, "2": 2}


# ------------------------------------------------------------------ helpers

def _dt(args: dict[str, Any], key: str) -> datetime:
    try:
        return timeutil.parse_iso(str(args[key]))
    except (KeyError, ValueError):
        raise ToolError(f"'{key}' must be a date-time like 2026-09-25T15:00:00+05:30")


def _busy(ev: dict[str, Any]) -> bool:
    return not ev.get("free") and not ev.get("declined") and ev.get("status") != "cancelled"


def _others(ev: dict[str, Any], me: str) -> list[dict[str, Any]]:
    return [a for a in ev.get("attendees", []) if not a.get("self") and a["email"] != me]


def _names(attendees: list[dict[str, Any]], limit: int = 3) -> str:
    def label(a):
        return a.get("name") or a["email"].split("@")[0]
    shown = [label(a) for a in attendees[:limit]]
    return ", ".join(shown) + (f" +{len(attendees) - limit}" if len(attendees) > limit else "")


def _span(ev: dict[str, Any]) -> str:
    if ev.get("all_day"):
        return "All day"
    return timeutil.fmt_range(timeutil.parse_iso(ev["start"]), timeutil.parse_iso(ev["end"]))


def event_line(ev: dict[str, Any], me: str = "") -> str:
    others = _others(ev, me)
    with_ = f" · with {_names(others)}" if others else ""
    where = f" · {ev['location']}" if ev.get("location") else ""
    again = " · repeats" if ev.get("recurring") else ""
    on = f" · {ev['account']}" if ev.get("account") else ""      # set when several calendars are read together
    return f"• {_span(ev)}  {ev['title']}{with_}{where}{again}{on}"


def _brief(ev: dict[str, Any], me: str = "") -> dict[str, Any]:
    out = {"id": ev["id"], "title": ev["title"], "start": ev["start"], "end": ev["end"], "all_day": ev.get("all_day", False),
           "attendees": [a["name"] or a["email"] for a in _others(ev, me)], "location": ev.get("location"),
           "recurring": bool(ev.get("recurring"))}
    if ev.get("account"):
        out["account"] = ev["account"]
    return out


def _repeat(args: dict[str, Any], start: datetime) -> tuple[Optional[int], Optional[date]]:
    """(days apart, last day) from create_event's repeat / repeat_until - (None, None) for a one-off."""
    word = str(args.get("repeat") or "").strip().lower().replace("_", " ").replace("-", " ")
    if word in ("", "none", "once", "no", "false", "0", "never"):
        return None, None
    n = _REPEAT_WORDS.get(word)
    if n is None:
        raise ToolError("repeat can only be 'every_other_day' (every 2 days) - leave it out for a one-off event")
    if not args.get("repeat_until"):
        return n, None
    try:
        until = date.fromisoformat(str(args["repeat_until"]).strip()[:10])
    except ValueError:
        raise ToolError("'repeat_until' must be a date like 2026-10-31")
    if until < start.date():
        raise ToolError("'repeat_until' can't be before the first day")
    return n, until


def repeat_words(n: Optional[int], until: Optional[date] = None) -> str:
    """'Every other day' / 'Every other day until Thu Oct 8' ('' for a one-off)."""
    if not n:
        return ""
    return REPEATS.get(n, f"Every {n} days") + (f" until {timeutil.fmt_day(until)}" if until else "")


def window_label(start: datetime, end: datetime, now: datetime) -> str:
    """How a window reads in a sentence: "tomorrow", "today at 3:00 PM",
    "tomorrow between 2:00 PM and 5:00 PM", "from Mon Sep 28 to Sun Oct 4"."""
    today = now.date()
    last = (end - timedelta(seconds=1)).date()
    if end - start <= timedelta(minutes=1):
        return f"{timeutil.relative_day(start.date(), today)} at {timeutil.fmt_time(start)}"
    if start.date() != last:
        return f"from {timeutil.fmt_day(start)} to {timeutil.fmt_day(last)}"
    if start.time() == time(0, 0) and end - start >= timedelta(hours=23):
        return timeutil.relative_day(start.date(), today)
    return f"{timeutil.relative_day(start.date(), today)} between {timeutil.fmt_time(start)} and {timeutil.fmt_time(end)}"


def format_events(events: list[dict[str, Any]], start: datetime, end: datetime, now: datetime, me: str = "") -> str:
    label = window_label(start, end, now)
    if not events:
        return f"You have nothing on your calendar {label}."
    n = len(events)
    # "tomorrow" alone is ambiguous in a saved transcript - add the date.
    date_note = f" ({timeutil.fmt_day(start)})" if label in ("today", "tomorrow") else ""
    head = f"You have {n} event{'s' if n != 1 else ''} {label}{date_note}:"
    if start.date() == (end - timedelta(seconds=1)).date():
        return head + "\n" + "\n".join(event_line(e, me) for e in events)
    lines = [head]
    by_day: dict[date, list[dict[str, Any]]] = {}
    for e in events:
        by_day.setdefault(timeutil.parse_iso(e["start"]).date(), []).append(e)
    for d in sorted(by_day):
        lines.append(f"{timeutil.fmt_day(d)}:")
        lines += [event_line(e, me) for e in by_day[d]]
    return "\n".join(lines)


def merge_busy(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def free_gaps(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    gaps, cursor = [], start
    for s, e in merge_busy(busy):
        if e <= cursor:
            continue
        if s >= end:
            break
        if s > cursor:
            gaps.append((cursor, min(s, end)))
        cursor = max(cursor, e)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def free_slots(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime, minutes: int, now: datetime,
               work_start: time = WORK_START, work_end: time = WORK_END, work_days: tuple = WORK_DAYS,
               step: int = SLOT_STEP_MINUTES, limit: int = MAX_SLOTS) -> list[tuple[datetime, datetime]]:
    """Candidate meeting slots: inside working hours on working days, on a
    half-hour grid, clear of everything in `busy`, no earlier than an hour
    from now. Offers the earliest slot each day plus the earliest afternoon
    one, so the choices are spread out rather than all at 9 am on day one."""
    tz = start.tzinfo
    merged = merge_busy(busy)
    earliest = now + timedelta(hours=1)
    dur = timedelta(minutes=minutes)
    picks: list[tuple[datetime, datetime]] = []
    day = start.date()
    while datetime.combine(day, time(0, 0), tzinfo=tz) < end and len(picks) < limit:
        if day.weekday() in work_days:
            t = datetime.combine(day, work_start, tzinfo=tz)
            day_end = datetime.combine(day, work_end, tzinfo=tz)
            found: list[tuple[datetime, datetime]] = []
            while t + dur <= day_end:
                slot = (t, t + dur)
                if t >= start and slot[1] <= end and t >= earliest and not any(s < slot[1] and e > slot[0] for s, e in merged):
                    found.append(slot)
                t += timedelta(minutes=step)
            if found:
                chosen = [found[0]]
                afternoon = next((s for s in found if s[0].time() >= time(14, 0)), None)
                if afternoon and afternoon != found[0]:
                    chosen.append(afternoon)
                picks += chosen[:max(0, limit - len(picks))]
        day += timedelta(days=1)
    return picks


def _conflicts(provider, start: datetime, end: datetime, exclude_id: Optional[str] = None) -> list[str]:
    try:
        events = provider.list_events(start, end)
    except Exception:  # a warning is a courtesy; never block the action on it
        return []
    return [f"You already have “{e['title']}” ({_span(e)})" for e in events
            if _busy(e) and not e.get("all_day") and e["id"] != exclude_id]


def _calendar_field(provider) -> str:
    acct = provider.account()
    return f"Primary ({acct})" if acct else "Primary"


class CalendarTool(Tool):
    id = "calendar"
    label = "Calendar"
    account_service = "calendar"
    actions = {
        "list_events": ActionSpec(
            "list_events", "List calendar events in a time window.",
            {"start": "window start, ISO 8601 with offset", "end": "window end, ISO 8601 with offset",
             "query?": "only events matching this text"}, reads_untrusted=True),
        "check_availability": ActionSpec(
            "check_availability", "Say whether the owner is free in a window (a point in time is a one-minute window).",
            {"start": "window start, ISO 8601", "end": "window end, ISO 8601"}, reads_untrusted=True),
        "find_slots": ActionSpec(
            "find_slots", "Find free meeting slots inside working hours (9-6, weekdays), checking the owner's calendar "
            "and, where Google lets us see it, the attendees'.",
            {"duration_minutes": "meeting length", "start": "search from, ISO 8601", "end": "search until, ISO 8601",
             "attendees?": "attendee email address(es)"}),
        "create_event": ActionSpec(
            "create_event", "Create an event. With attendees it is a meeting and an invitation is emailed (needs approval).",
            {"title": "event title", "start": "start, ISO 8601 with offset",
             "end?": "end, ISO 8601 (or give duration_minutes)", "duration_minutes?": "default 60",
             "attendees?": "attendee email address(es)", "description?": "notes", "location?": "where",
             "add_video_call?": "true to add a Google Meet link", "all_day?": "true for an all-day event",
             "repeat?": "'every_other_day' to repeat it every 2 days (leave out for a one-off)",
             "repeat_until?": "last day it repeats on, YYYY-MM-DD (leave out for no end)"}),
        "update_event": ActionSpec(
            "update_event", "Change an existing event (move it, rename it, add attendees). Needs approval.",
            {"event_id": "id of the event (from list_events)", "title?": "new title", "start?": "new start, ISO 8601",
             "end?": "new end (defaults to keeping the same length)", "description?": "new notes",
             "location?": "new location", "add_attendees?": "email address(es) to invite"}),
        "cancel_event": ActionSpec("cancel_event", "Cancel (delete) an event. Needs approval.",
                                   {"event_id": "id of the event"}),
    }

    # ---------------------------------------------------------------- reads

    def prepare_list_events(self, args, ctx):
        start, end = _dt(args, "start"), _dt(args, "end")
        if end <= start:
            raise ToolError("The end of the window must be after its start")
        return Prepared({"start": start.isoformat(), "end": end.isoformat(), "query": args.get("query")},
                        ["CALENDAR_READ"], f"events {timeutil.fmt_day(start)}", f"Reading your calendar for {timeutil.fmt_day(start)}")

    def run_list_events(self, args, ctx):
        provider = ctx.calendar()
        start, end = timeutil.parse_iso(args["start"]), timeutil.parse_iso(args["end"])
        events = provider.list_events(start, end, args.get("query"))
        me = provider.account()
        return {"text": format_events(events, start, end, ctx.now(), me),
                "data": {"events": [_brief(e, me) for e in events]}}

    def prepare_check_availability(self, args, ctx):
        p = self.prepare_list_events(args, ctx)
        p.describe = f"Checking whether you're free {timeutil.fmt_day(timeutil.parse_iso(args['start']))}"
        return p

    def run_check_availability(self, args, ctx):
        provider = ctx.calendar()
        start, end = timeutil.parse_iso(args["start"]), timeutil.parse_iso(args["end"])
        me = provider.account()
        busy_events = [e for e in provider.list_events(start, end) if _busy(e) and not e.get("all_day")]
        label = window_label(start, end, ctx.now())
        point = end - start <= timedelta(minutes=1)
        if not busy_events:
            text = f"Yes - nothing is scheduled {label}." if point else f"Yes, you're free {label}."
            return {"text": text, "data": {"free": True, "events": []}}
        lines = [event_line(e, me) for e in busy_events]
        if point:
            text = f"You have something {label}:\n" + "\n".join(lines)
        else:
            gaps = free_gaps([(timeutil.parse_iso(e["start"]), timeutil.parse_iso(e["end"])) for e in busy_events], start, end)
            free = ", ".join(timeutil.fmt_range(a, b) for a, b in gaps if b - a >= timedelta(minutes=15))
            text = f"Not the whole time - {label} you have:\n" + "\n".join(lines) + (f"\nFree: {free}." if free else "\nNo free gaps.")
        return {"text": text, "data": {"free": False, "events": [_brief(e, me) for e in busy_events]}}

    def prepare_find_slots(self, args, ctx):
        start, end = _dt(args, "start"), _dt(args, "end")
        try:
            minutes = int(args["duration_minutes"])
        except (TypeError, ValueError):
            raise ToolError("duration_minutes must be a whole number")
        if not 5 <= minutes <= 480:
            raise ToolError("duration_minutes must be between 5 and 480")
        attendees = _addresses(args.get("attendees"))
        return Prepared({"start": start.isoformat(), "end": end.isoformat(), "duration_minutes": minutes,
                         "attendees": attendees}, ["CALENDAR_READ"], f"free slots {timeutil.fmt_day(start)}",
                        "Looking for a free slot" + (f" with {', '.join(attendees)}" if attendees else ""))

    def run_find_slots(self, args, ctx):
        provider = ctx.calendar()
        start, end = timeutil.parse_iso(args["start"]), timeutil.parse_iso(args["end"])
        now = ctx.now()
        start = max(start, now)
        fb = provider.freebusy(args.get("attendees", []), start, end)
        busy: list[tuple[datetime, datetime]] = []
        blind: list[str] = []
        for who, info in fb.items():
            if info.get("error"):
                blind.append(who)
            else:
                busy += info["busy"]
        slots = free_slots(busy, start, end, args["duration_minutes"], now)
        notes = ""
        if blind:
            notes = ("\nI can't see " + ", ".join(blind) + "'s calendar (it isn't shared with you), so these times only "
                     "account for the calendars I could see - confirm with them.")
        if not slots:
            return {"text": f"I couldn't find a free {args['duration_minutes']}-minute slot in working hours in that window." + notes,
                    "data": {"slots": [], "unseen": blind}}
        by_day: dict[date, list[str]] = {}
        for a, b in slots:
            by_day.setdefault(a.date(), []).append(timeutil.fmt_range(a, b))
        lines = [f"{timeutil.fmt_day(d)}: {', '.join(v)}" for d, v in by_day.items()]
        text = f"Free {args['duration_minutes']}-minute slots:\n" + "\n".join("• " + l for l in lines) + notes
        return {"text": text, "data": {"slots": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in slots],
                                       "unseen": blind}}

    # --------------------------------------------------------------- creating

    def prepare_create_event(self, args, ctx):
        provider = ctx.calendar()
        all_day = str(args.get("all_day", "")).lower() in ("true", "1", "yes")
        start = _dt(args, "start")
        if "end" in args:
            end = _dt(args, "end")
        else:
            try:
                minutes = int(args.get("duration_minutes", 60))
            except (TypeError, ValueError):
                raise ToolError("duration_minutes must be a whole number")
            end = start + (timedelta(days=1) if all_day else timedelta(minutes=minutes))
        if end <= start:
            raise ToolError("The event must end after it starts")
        attendees = _addresses(args.get("attendees"))
        repeat, until = _repeat(args, start)
        caps = ["CALENDAR_CREATE"] + (["CALENDAR_INVITE"] if attendees else [])
        resolved = {"title": args["title"], "start": start.isoformat(), "end": end.isoformat(), "attendees": attendees,
                    "description": args.get("description"), "location": args.get("location"),
                    "all_day": all_day, "add_video_call": str(args.get("add_video_call", "")).lower() in ("true", "1", "yes"),
                    "repeat_days": repeat, "repeat_until": until.isoformat() if until else None}
        meeting = bool(attendees)
        again = repeat_words(repeat, until)
        fields = [("Title", args["title"]), ("Date", timeutil.fmt_long_date(start) + (" (first)" if repeat else "")),
                  ("Time", "All day" if all_day else timeutil.fmt_range(start, end)),
                  ("Repeats", (again if until else again + ", no end date") if repeat else None),
                  ("Attendees", ", ".join(attendees)), ("Where", args.get("location")),
                  ("Video call", "Google Meet link will be added" if resolved["add_video_call"] else None),
                  ("Calendar", _calendar_field(provider)),
                  ("Action", "Create meeting and send invitation" if meeting else "Add to your calendar")]
        pv = preview("meeting" if meeting else "event", "MEETING APPROVAL" if meeting else "EVENT APPROVAL",
                     "Create meeting" if meeting else "Create event", fields, "Approve",
                     [] if all_day else _conflicts(provider, start, end))
        return Prepared(resolved, caps, f"{args['title'][:60]} ({timeutil.fmt_day(start)})",
                        f"{'Schedule' if meeting else 'Add'} “{args['title']}” {timeutil.fmt_when(start)}"
                        + (f", {again[0].lower() + again[1:]}" if repeat else "")
                        + (f" with {', '.join(attendees)}" if meeting else ""), pv)

    def run_create_event(self, args, ctx):
        start, end = timeutil.parse_iso(args["start"]), timeutil.parse_iso(args["end"])
        repeat = args.get("repeat_days")
        until = date.fromisoformat(args["repeat_until"]) if args.get("repeat_until") else None
        ev = ctx.calendar().create_event(args["title"], start, end, args.get("attendees") or None, args.get("description"),
                                         args.get("location"), args.get("all_day", False), args.get("add_video_call", False),
                                         repeat, until)
        when = "all day" if args.get("all_day") else timeutil.fmt_range(start, end)
        words = repeat_words(repeat, until)
        again = f" Repeats {words[0].lower() + words[1:]}." if repeat else ""
        invited = f" Invitation sent to {', '.join(args['attendees'])}." if args.get("attendees") else ""
        link = f"\n{ev['link']}" if ev.get("link") else ""
        return {"text": f"Created “{ev['title']}” on {timeutil.fmt_day(start)}, {when}.{again}{invited}{link}",
                "data": {"event": _brief(ev)}, "ref": ev.get("id")}

    # --------------------------------------------------------------- changing

    def _existing(self, provider, event_id: str) -> dict[str, Any]:
        try:
            return provider.get_event(event_id)
        except Exception as e:
            raise ToolError(f"I couldn't open that event: {e}")

    def prepare_update_event(self, args, ctx):
        provider = ctx.calendar()
        ev = self._existing(provider, args["event_id"])
        me = provider.account()
        old_start, old_end = timeutil.parse_iso(ev["start"]), timeutil.parse_iso(ev["end"])
        changes: dict[str, Any] = {}
        new_start, new_end = old_start, old_end
        if "start" in args:
            new_start = _dt(args, "start")
            new_end = _dt(args, "end") if "end" in args else new_start + (old_end - old_start)
        elif "end" in args:
            new_end = _dt(args, "end")
        if (new_start, new_end) != (old_start, old_end):
            if new_end <= new_start:
                raise ToolError("The event must end after it starts")
            changes["start"], changes["end"] = new_start.isoformat(), new_end.isoformat()
        for k in ("title", "description", "location"):
            if k in args and args[k] != ev.get(k):
                changes[k] = args[k]
        add = [a for a in _addresses(args.get("add_attendees")) if a not in {x["email"] for x in ev["attendees"]}]
        if add:
            changes["add_attendees"] = add
        if not changes:
            raise ToolError("That would not change anything about the event")
        others = _others(ev, me)
        notify = bool(others or add)
        caps = ["CALENDAR_UPDATE"] + (["CALENDAR_INVITE"] if notify else [])
        moved = "start" in changes
        fields = [("Title", changes.get("title", ev["title"]) if "title" not in changes else f"{ev['title']} → {changes['title']}"),
                  ("Date", (timeutil.fmt_long_date(new_start) if new_start.date() == old_start.date()
                            else f"{timeutil.fmt_long_date(old_start)} → {timeutil.fmt_long_date(new_start)}")),
                  ("Time", (f"{timeutil.fmt_range(old_start, old_end)} → {timeutil.fmt_range(new_start, new_end)}"
                            if moved else timeutil.fmt_range(old_start, old_end))),
                  ("Attendees", ", ".join([a["email"] for a in others] + [f"{x} (new)" for x in add])),
                  ("Calendar", _calendar_field(provider)),
                  ("Action", ("Move" if moved else "Update") + (" meeting and notify attendees" if notify else " event"))]
        warn = _conflicts(provider, new_start, new_end, exclude_id=ev["id"]) if moved else []
        resolved = {"event_id": ev["id"], "changes": changes, "notify": notify, "title": ev["title"]}
        return Prepared(resolved, caps, f"{ev['title'][:60]} ({timeutil.fmt_day(old_start)})",
                        f"{'Move' if moved else 'Update'} “{ev['title']}”"
                        + (f" to {timeutil.fmt_when(new_start)}" if moved else ""),
                        preview("calendar_change", "EVENT CHANGE APPROVAL", "Change event", fields, "Approve", warn))

    def run_update_event(self, args, ctx):
        changes = dict(args["changes"])
        for k in ("start", "end"):
            if k in changes:
                changes[k] = timeutil.parse_iso(changes[k])
        ev = ctx.calendar().update_event(args["event_id"], changes, args.get("notify", False))
        start, end = timeutil.parse_iso(ev["start"]), timeutil.parse_iso(ev["end"])
        note = " Attendees were notified." if args.get("notify") else ""
        return {"text": f"Updated “{ev['title']}” - now {timeutil.fmt_day(start)}, {timeutil.fmt_range(start, end)}.{note}",
                "data": {"event": _brief(ev)}, "ref": ev.get("id")}

    def prepare_cancel_event(self, args, ctx):
        provider = ctx.calendar()
        ev = self._existing(provider, args["event_id"])
        others = _others(ev, provider.account())
        start, end = timeutil.parse_iso(ev["start"]), timeutil.parse_iso(ev["end"])
        caps = ["CALENDAR_DELETE"] + (["CALENDAR_INVITE"] if others else [])
        fields = [("Title", ev["title"]), ("Date", timeutil.fmt_long_date(start)),
                  ("Time", "All day" if ev.get("all_day") else timeutil.fmt_range(start, end)),
                  ("Attendees", ", ".join(a["email"] for a in others)), ("Calendar", _calendar_field(provider)),
                  ("Action", "Cancel meeting and notify attendees" if others else "Delete event")]
        return Prepared({"event_id": ev["id"], "notify": bool(others), "title": ev["title"], "start": ev["start"]}, caps,
                        f"{ev['title'][:60]} ({timeutil.fmt_day(start)})", f"Cancel “{ev['title']}” {timeutil.fmt_when(start)}",
                        preview("calendar_cancel", "CANCEL EVENT APPROVAL", "Cancel event", fields, "Cancel event",
                                deny="Keep it"))

    def run_cancel_event(self, args, ctx):
        ctx.calendar().delete_event(args["event_id"], args.get("notify", False))
        note = " Attendees were notified." if args.get("notify") else ""
        return {"text": f"Cancelled “{args.get('title', 'the event')}”.{note}", "data": {"cancelled": args["event_id"]}}

    # ---------------------------------------------------------------- memory

    def remember(self, action, result, memory):
        data = result.get("data") or {}
        if action in ("list_events", "check_availability") and "events" in data:
            memory["events"] = data["events"][:25]
        elif action == "find_slots":
            memory["slots"] = data.get("slots", [])
        elif action in ("create_event", "update_event") and data.get("event"):
            keep = [e for e in memory.get("events", []) if e["id"] != data["event"]["id"]]
            memory["events"] = ([data["event"]] + keep)[:25]
        elif action == "cancel_event":
            memory["events"] = [e for e in memory.get("events", []) if e["id"] != data.get("cancelled")]


def _addresses(value: Any) -> list[str]:
    try:
        return mailutil.recipients(value, "attendee")
    except ValueError as e:
        raise ToolError(str(e))


register(CalendarTool())
