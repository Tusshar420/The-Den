"""
Dates and times for the assistant: the local time zone, a small rule-based
parser for the phrases people actually say ("tomorrow at 3", "next week",
"between 2 and 5 pm", "in 30 minutes"), and display formatting.

The parser exists so the common requests can run without a model. It only
answers when it is sure: anything it can't read comes back empty and the
request goes to the planner instead. Callers always echo the resolved date
back to the user, so a wrong guess is visible.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- time zone

_TZ_CACHE: dict[str, tzinfo] = {}


def local_tz() -> tzinfo:
    """OCTOPUS_TZ, else the Mac's zone (from /etc/localtime), else the
    current fixed offset."""
    name = os.environ.get("OCTOPUS_TZ")
    if not name:
        try:
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                name = target.split("zoneinfo/", 1)[1]
        except OSError:
            name = None
    if name:
        if name not in _TZ_CACHE:
            try:
                _TZ_CACHE[name] = ZoneInfo(name)
            except Exception:
                _TZ_CACHE[name] = datetime.now().astimezone().tzinfo  # type: ignore[assignment]
        return _TZ_CACHE[name]
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def tz_name() -> str:
    tz = local_tz()
    return getattr(tz, "key", None) or str(tz)


def now() -> datetime:
    return datetime.now(local_tz())


def at(d: date, t: time = time(0, 0), tz: Optional[tzinfo] = None) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=tz or local_tz())


def parse_iso(value: str) -> datetime:
    """An ISO timestamp (with or without an offset) as an aware datetime."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=local_tz())


# --------------------------------------------------------------- formatting

def fmt_time(dt: datetime) -> str:
    dt = dt.astimezone(local_tz())
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_day(d) -> str:
    if isinstance(d, datetime):
        d = d.astimezone(local_tz()).date()
    return f"{d.strftime('%a %b')} {d.day}"


def fmt_long_date(d) -> str:
    if isinstance(d, datetime):
        d = d.astimezone(local_tz()).date()
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def fmt_when(dt: datetime) -> str:
    return f"{fmt_day(dt)}, {fmt_time(dt)}"


def fmt_range(start: datetime, end: datetime) -> str:
    return f"{fmt_time(start)} – {fmt_time(end)}"


def relative_day(d: date, today: date) -> str:
    delta = (d - today).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(delta, fmt_day(d))


# ------------------------------------------------------------------ parsing

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD_ALIASES = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
_MONTH_ALIASES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
                  "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}

# Where a vague period lands when it is the time of a reminder or event...
PERIOD_POINTS = {"morning": time(9, 0), "afternoon": time(14, 0), "evening": time(18, 0),
                 "tonight": time(20, 0), "eod": time(17, 0), "noon": time(12, 0)}
# ...and the stretch of the day it covers when it narrows a calendar read.
PERIOD_WINDOWS = {"morning": (time(5, 0), time(12, 0)), "afternoon": (time(12, 0), time(17, 0)),
                  "evening": (time(17, 0), time(23, 59)), "tonight": (time(17, 0), time(23, 59))}

_WD_RE = "|".join(sorted([*WEEKDAYS, *_WD_ALIASES], key=len, reverse=True))
_MON_RE = "|".join(sorted([*MONTHS, *_MONTH_ALIASES], key=len, reverse=True))
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"


@dataclass
class When:
    day: Optional[date] = None
    days: Optional[tuple[date, date]] = None  # [first, last+1) for "next week"
    start: Optional[time] = None
    end: Optional[time] = None
    is_range: bool = False
    period: Optional[str] = None
    relative: Optional[datetime] = None
    # "at 8" with no am/pm: a morning hour that might have meant the evening.
    bare_hour: bool = False
    spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return any([self.day, self.days, self.start, self.period, self.relative])


def _meridiem(token: Optional[str]) -> Optional[str]:
    return token.replace(".", "").lower() if token else None


def _to_24h(hour: int, minute: int, mer: Optional[str], guess_pm: Optional[bool] = None) -> Optional[time]:
    if minute > 59 or hour > 23:
        return None
    if mer:
        if not 1 <= hour <= 12:
            return None
        hour = (hour % 12) + (12 if mer == "pm" else 0)
    elif hour <= 12 and guess_pm is not None:
        # No am/pm given: office-hours guess. 1-7 is the afternoon, 8-11 the
        # morning, 12 is noon.
        if hour == 12:
            pass
        elif guess_pm:
            hour += 12
    return time(hour, minute)


def _office_guess(hour: int) -> bool:
    return 1 <= hour <= 7


def _find_day(text: str, today: date) -> Optional[tuple[date, tuple[int, int]]]:
    m = re.search(r"\bday after tomorrow\b", text, re.I)
    if m:
        return today + timedelta(days=2), m.span()
    m = re.search(r"\btomorrow\b", text, re.I)
    if m:
        return today + timedelta(days=1), m.span()
    m = re.search(r"\b(today|tonight|this (?:morning|afternoon|evening))\b", text, re.I)
    if m:
        return today, m.span()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.span()
        except ValueError:
            return None
    m = re.search(rf"\b(next|this|coming|on)?\s*\b({_WD_RE})\b", text, re.I)
    if m:
        name = m.group(2).lower()
        wd = WEEKDAYS.index(name) if name in WEEKDAYS else _WD_ALIASES[name]
        qualifier = (m.group(1) or "").lower()
        ahead = (wd - today.weekday()) % 7
        if qualifier == "next":
            # "next friday" = that day in next calendar week
            monday_next = today + timedelta(days=7 - today.weekday())
            return monday_next + timedelta(days=wd), m.span()
        if qualifier == "this":
            return today + timedelta(days=ahead), m.span()
        return today + timedelta(days=ahead or 7), m.span()
    for pattern, order in ((rf"\b({_MON_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?\b", "md"),
                           (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON_RE})\.?(?:,?\s*(\d{{4}}))?\b", "dm")):
        m = re.search(pattern, text, re.I)
        if not m:
            continue
        mon_name, day_no = (m.group(1), m.group(2)) if order == "md" else (m.group(2), m.group(1))
        month = (MONTHS.index(mon_name.lower()) + 1) if mon_name.lower() in MONTHS else _MONTH_ALIASES[mon_name.lower()]
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = date(year, month, int(day_no))
        except ValueError:
            return None
        if not m.group(3) and d < today:
            d = date(year + 1, month, int(day_no))
        return d, m.span()
    return None


def _find_week(text: str, today: date) -> Optional[tuple[tuple[date, date], tuple[int, int]]]:
    m = re.search(r"\bnext week\b", text, re.I)
    if m:
        start = today + timedelta(days=7 - today.weekday())
        return (start, start + timedelta(days=7)), m.span()
    m = re.search(r"\bthis week\b", text, re.I)
    if m:
        return (today, today + timedelta(days=7 - today.weekday())), m.span()
    return None


def _find_relative(text: str, current: datetime) -> Optional[tuple[datetime, tuple[int, int]]]:
    m = re.search(r"\bin (an?|\d{1,3}) (minute|min|hour|hr|day|week)s?\b", text, re.I)
    if not m:
        return None
    n = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
    unit = m.group(2).lower()
    delta = {"minute": timedelta(minutes=n), "min": timedelta(minutes=n), "hour": timedelta(hours=n),
             "hr": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
    return current + delta, m.span()


def _find_time_range(text: str) -> Optional[tuple[time, time, tuple[int, int]]]:
    m = re.search(rf"\b(?:between|from)\s+{_CLOCK}\s*(?:and|to|-|–|until)\s*{_CLOCK}", text, re.I) \
        or re.search(rf"\b{_CLOCK}\s*(?:-|–|to)\s*{_CLOCK}\b(?=\s*(?:pm|am|$|[.,?!]|\s))", text, re.I)
    if not m:
        return None
    h1, m1, mer1, h2, m2, mer2 = int(m.group(1)), int(m.group(2) or 0), _meridiem(m.group(3)), \
        int(m.group(4)), int(m.group(5) or 0), _meridiem(m.group(6))
    if mer2 and not mer1:
        # "2 and 5 pm" -> both pm, unless that would put the start after the end ("11 to 2 pm").
        mer1 = mer2 if (h1 % 12) <= (h2 % 12) or h1 == 12 and h2 == 12 else ("am" if mer2 == "pm" else "pm")
    start = _to_24h(h1, m1, mer1, _office_guess(h1))
    end = _to_24h(h2, m2, mer2 or mer1, _office_guess(h2))
    if not start or not end:
        return None
    if end <= start:
        if end.hour + 12 < 24 and not mer2:
            end = end.replace(hour=end.hour + 12)
        if end <= start:
            return None
    return start, end, m.span()


def _find_time(text: str) -> Optional[tuple[time, tuple[int, int]]]:
    m = re.search(r"\bnoon\b", text, re.I)
    if m:
        return time(12, 0), m.span()
    m = re.search(r"\bmidnight\b", text, re.I)
    if m:
        return time(0, 0), m.span()
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", text, re.I)
    if m:
        t = _to_24h(int(m.group(1)), int(m.group(2)), _meridiem(m.group(3)),
                    _office_guess(int(m.group(1))) if not m.group(3) and int(m.group(1)) <= 12 else None)
        return (t, m.span()) if t else None
    m = re.search(r"\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)", text, re.I)
    if m:
        t = _to_24h(int(m.group(1)), 0, _meridiem(m.group(2)))
        return (t, m.span()) if t else None
    m = re.search(r"\b(?:at|around|by)\s+(\d{1,2})(?:\s*o'?clock)?\b(?!\s*(?:[:/-]|\d))", text, re.I)
    if m:
        h = int(m.group(1))
        t = _to_24h(h, 0, None, _office_guess(h))
        return (t, m.span()) if t else None
    return None


def _find_period(text: str) -> Optional[tuple[str, tuple[int, int]]]:
    m = re.search(r"\b(morning|afternoon|evening|tonight|eod|end of (?:the )?day)\b", text, re.I)
    if not m:
        return None
    word = m.group(1).lower()
    return ("eod" if word.startswith(("eod", "end of")) else word), m.span()


def parse_when(text: str, current: Optional[datetime] = None) -> When:
    """Whatever date/time information the text carries. Never raises; an
    empty When (found == False) means "I can't tell"."""
    current = current or now()
    today = current.date()
    w = When()

    rel = _find_relative(text, current)
    if rel:
        w.relative = rel[0]
        w.spans.append(rel[1])
        return w

    week = _find_week(text, today)
    if week:
        w.days = week[0]
        w.spans.append(week[1])
    else:
        day = _find_day(text, today)
        if day:
            w.day = day[0]
            w.spans.append(day[1])

    rng = _find_time_range(text)
    if rng:
        w.start, w.end, span = rng
        w.is_range = True
        w.spans.append(span)
    else:
        t = _find_time(text)
        if t:
            w.start = t[0]
            w.spans.append(t[1])
            w.bare_hour = not re.search(r"am|pm|a\.m\.|p\.m\.|noon|midnight|:\d{2}", text[t[1][0]:t[1][1]], re.I)
    per = _find_period(text)
    if per:
        w.period = per[0]
        # "tonight" / "this evening" already consumed as the day word.
        if not any(s[0] <= per[1][0] and per[1][1] <= s[1] for s in w.spans):
            w.spans.append(per[1])
    return w


def strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """The text with the parsed date/time phrases cut out (and any dangling
    'on', 'at', 'for' left behind trimmed)."""
    out = text
    for a, b in sorted(spans, reverse=True):
        out = out[:a] + " " + out[b:]
    out = re.sub(r"\s+", " ", out).strip(" ,.;:-")
    while True:
        trimmed = re.sub(r"\b(on|at|for|by|around|in the|in)\s*$", "", out, flags=re.I).strip(" ,.;:-")
        trimmed = re.sub(r"^(on|at|for|by|around|in the)\s+", "", trimmed, flags=re.I).strip(" ,.;:-")
        if trimmed == out:
            return out
        out = trimmed


# ------------------------------------------------------------ resolved forms

def calendar_window(w: When, current: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    """(start, end, label) - the stretch of calendar a request is about. No
    date at all means today."""
    current = current or now()
    tz = current.tzinfo or local_tz()
    today = current.date()
    if w.days:
        a, b = w.days
        label = "next week" if a > today else "this week"
        return at(a, tz=tz), at(b, tz=tz), f"{label} ({fmt_day(a)} – {fmt_day(b - timedelta(days=1))})"
    day = w.day or today
    label = relative_day(day, today)
    if w.start and not w.is_range:
        start = at(day, w.start, tz)
        return start, start + timedelta(minutes=1), f"{relative_day(day, today)} at {fmt_time(start)}"
    if w.is_range and w.start and w.end:
        return at(day, w.start, tz), at(day, w.end, tz), \
            f"{relative_day(day, today)} between {fmt_time(at(day, w.start, tz))} and {fmt_time(at(day, w.end, tz))}"
    if w.period in PERIOD_WINDOWS:
        a, b = PERIOD_WINDOWS[w.period]
        return at(day, a, tz), at(day, b, tz), f"{relative_day(day, today)} {w.period}"
    return at(day, tz=tz), at(day + timedelta(days=1), tz=tz), label


def reminder_time(w: When, current: Optional[datetime] = None) -> Optional[datetime]:
    """When a reminder should fire, or None if the words don't pin it down
    to a moment in the future."""
    current = current or now()
    tz = current.tzinfo or local_tz()
    if w.relative:
        return w.relative
    if w.days:
        return None
    t = w.start or (PERIOD_POINTS.get(w.period) if w.period else None)
    if w.day:
        due = at(w.day, t or time(9, 0), tz)
    elif t:
        due = at(current.date(), t, tz)
        if due <= current and w.bare_hour and t.hour < 12:
            due += timedelta(hours=12)  # "at 8" said at 7:50 pm means tonight
        if due <= current:
            due += timedelta(days=1)
    else:
        return None
    return due if due > current else None
