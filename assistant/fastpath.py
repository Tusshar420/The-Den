"""
The no-model path. For requests whose meaning is fixed and whose parameters
can be read straight out of the words ("what's on my calendar tomorrow",
"latest email from Rahul", "remind me at 5 to call mom"), turn the text into
tool calls directly. Nothing here calls an AI; if anything is unclear the
function returns None and the request goes to the planner instead.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from assistant import timeutil


@dataclass
class Step:
    tool: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)


# A step argument set to this is replaced with the id of the first message
# the previous step found ("read Rahul's latest email" = search, then read).
FIRST_MESSAGE = "@first_message"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().strip("?!.")).strip()


def _quote(who: str) -> str:
    who = who.strip().strip("\"'")
    return who if "@" in who else (f'"{who}"' if " " in who else who)


# ---------------------------------------------------------------- calendar

def _window(text: str, now: datetime) -> tuple[datetime, datetime]:
    start, end, _ = timeutil.calendar_window(timeutil.parse_when(text, now), now)
    return start, end


def plan_calendar_read(text: str, now: datetime) -> Optional[list[Step]]:
    start, end = _window(text, now)
    return [Step("calendar", "list_events", {"start": start.isoformat(), "end": end.isoformat()})]


def plan_calendar_availability(text: str, now: datetime) -> Optional[list[Step]]:
    start, end = _window(text, now)
    return [Step("calendar", "check_availability", {"start": start.isoformat(), "end": end.isoformat()})]


_DURATION = re.compile(r"\bfor (\d{1,3}|an?|one|half an?) ?(minutes?|mins?|hours?|hrs?)\b", re.I)


def _duration(text: str) -> tuple[Optional[int], Optional[tuple[int, int]]]:
    m = _DURATION.search(text)
    if not m:
        return None, None
    n = m.group(1).lower()
    qty = 0.5 if n.startswith("half") else 1 if n in ("a", "an", "one") else int(n)
    minutes = int(qty * (60 if m.group(2).lower().startswith(("h")) else 1))
    return minutes, m.span()


def plan_calendar_create(text: str, now: datetime) -> Optional[list[Step]]:
    """"Block 3 PM tomorrow for project work" - a personal event with a time
    and a title, nobody invited. Anything with attendees goes to the planner."""
    if re.search(r"\b(with|invite|and)\s+[a-z@]", text, re.I) and not re.search(r"\bwith\b.*\b(focus|lunch|coffee)\b", text, re.I):
        return None
    w = timeutil.parse_when(text, now)
    if not w.start or w.days or w.relative:
        return None
    day = w.day or now.date()
    minutes, dspan = _duration(text)
    spans = list(w.spans) + ([dspan] if dspan else [])
    title = None
    m = re.search(r"\b(?:called|titled|named)\s+(.+)$", text, re.I) or \
        re.search(r"\bfor\s+(?!\d|an? |one |half )(.+)$", text, re.I) or \
        re.search(r"\b(?:add|put|create|schedule)\s+(?:an?\s+)?(.+?)\s+(?:to|on|in)\s+my\s+calendar\b", text, re.I)
    if m:
        title = timeutil.strip_spans(m.group(1), [])
        # the title may still hold the date/time words that came after it
        tw = timeutil.parse_when(title, now)
        title = timeutil.strip_spans(title, tw.spans) if tw.found else title
    if not title:
        return None
    start = timeutil.at(day, w.start, now.tzinfo)
    if w.is_range and w.end:
        end = timeutil.at(day, w.end, now.tzinfo)
    else:
        end = start + timedelta(minutes=minutes or 60)
    title = title[0].upper() + title[1:]
    return [Step("calendar", "create_event", {"title": title, "start": start.isoformat(), "end": end.isoformat()})]


# ------------------------------------------------------------------- email

_WHO = r"(?P<who>[^?.!,]+?)"
_TOPIC = r"(?:\s+(?:about|regarding|on|re:?|concerning)\s+(?P<topic>.+?))?"


def _mail_query(text: str) -> Optional[tuple[str, bool]]:
    """(gmail query, wants just the latest one) for "latest email from X" /
    "X's latest email about Y"."""
    t = _clean(text)
    latest = bool(re.search(r"\b(latest|last|most recent|newest|recent)\b", t, re.I))
    m = re.search(rf"\b(?:e-?mails?|messages?|mails?)\s+(?:from|by)\s+{_WHO}{_TOPIC}$", t, re.I) or \
        re.search(rf"\b{_WHO}'s\s+(?:(?:latest|last|most recent|newest|recent)\s+)?(?:e-?mails?|messages?|mails?){_TOPIC}$", t, re.I)
    if not m:
        return None
    who = m.group("who")
    # "Read Rahul's latest email" - the name is what is left after the verb goes.
    who = re.sub(r"^(?:please\s+|can you\s+|could you\s+)?(?:read|open|show(?: me)?|find|search(?: for)?|check|get|fetch|see|view|"
                 r"pull up|look (?:for|up)|display|any|are there any)\s+", "", who, flags=re.I)
    who = re.sub(r"^(the|my|an?)\s+|\b(latest|last|newest|recent|most)\b", "", who, flags=re.I).strip()
    if not who or len(who.split()) > 4:
        return None
    query = f"from:{_quote(who)}"
    if m.group("topic"):
        query += f" {_quote(m.group('topic')) if len(m.group('topic').split()) == 1 else '(' + m.group('topic').strip() + ')'}"
    return query, latest


def plan_email_search(text: str, now: datetime) -> Optional[list[Step]]:
    found = _mail_query(text)
    if found:
        query, latest = found
        return [Step("email", "search", {"query": query, "max_results": 1 if latest else 5})]
    if re.search(r"\b(inbox|new e-?mails?|unread|new mail)\b", text, re.I):
        query = "in:inbox is:unread" if re.search(r"unread|new", text, re.I) else "in:inbox"
        return [Step("email", "search", {"query": query, "max_results": 5})]
    return None


def plan_email_read(text: str, now: datetime) -> Optional[list[Step]]:
    found = _mail_query(text)
    if not found:
        return None
    return [Step("email", "search", {"query": found[0], "max_results": 1}),
            Step("email", "read", {"message_id": FIRST_MESSAGE})]


# --------------------------------------------------------------- reminders

def plan_reminder_create(text: str, now: datetime) -> Optional[list[Step]]:
    w = timeutil.parse_when(text, now)
    due = timeutil.reminder_time(w, now)
    if not due:
        return None
    body = timeutil.strip_spans(text, w.spans)
    body = re.sub(r"^\s*(please\s+)?(?:set (?:a |an )?reminder|create (?:a )?reminder|remind me)\s*(?:to|that|about|of)?\s*", "",
                  body, flags=re.I).strip(" ,.;:-")
    body = re.sub(r"\s*\bremind me\b\s*$", "", body, flags=re.I).strip(" ,.;:-")
    if not body:
        return None
    return [Step("reminders", "create", {"text": body[0].upper() + body[1:], "when": due.isoformat()})]


def plan_reminder_list(text: str, now: datetime) -> Optional[list[Step]]:
    return [Step("reminders", "list", {})]


PLANNERS = {
    "calendar.read": plan_calendar_read,
    "calendar.availability": plan_calendar_availability,
    "calendar.create_event": plan_calendar_create,
    "email.search": plan_email_search,
    "email.read": plan_email_read,
    "reminder.create": plan_reminder_create,
    "reminder.list": plan_reminder_list,
}


def plan(intent: str, text: str, now: Optional[datetime] = None) -> Optional[list[Step]]:
    """Tool calls for this request, or None if it needs a model."""
    fn = PLANNERS.get(intent)
    if not fn:
        return None
    return fn(text, now or timeutil.now())
