"""
Google Calendar invites for meetings on the project calendar - so a meeting
made in the Den goes out exactly as if it had been made in Google Calendar.

A meeting can be sent as an invite: the Den creates the matching event on the
connected Google account's calendar, inviting the meeting's contacts (their
CRM email addresses) and any other guest addresses, optionally with a Google
Meet link. Google itself emails every guest - the usual invitation with
Accept / Decline and "add to calendar", from the connected account - and
emails them again when the meeting changes or is cancelled.

- send: create the Google event and keep its id, Meet link and link on the row.
- sync: after an edit, bring the Google event in line (guests get an update).
- cancel: delete the Google event (guests get a cancellation).
- after_update: which of those an edit calls for.

timeline.py owns the meeting itself; this only mirrors it, and never loses
it: a failed send or update is kept on the row as invite_error (shown under
the meeting), and saving the meeting again with invites on retries it.
The meeting's notes stay in the Den - they aren't sent to guests (the notes
page saves as you type, and every save would email everyone an update).
"""

import secrets
from datetime import date, time, timedelta, timezone
from typing import Any, Optional

import db
import timeline
from assistant import connections, timeutil
from assistant.providers import google
from assistant.providers.base import ProviderError

EVENTS = f"{google.CAL}/calendars/primary/events"
# What, changing on the meeting, changes its Google event (and emails the guests an update).
SYNCED = ("title", "date", "time", "duration", "guests", "contact_ids", "employee_ids", "repeat_days", "repeat_rule", "repeat_until")
_BYDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_GONE = (404, 410)


class InviteError(ValueError):
    pass


def available() -> bool:
    """Is a Google account connected with Calendar access?"""
    return _account() is not None


def _account() -> Optional[str]:
    """The Google account new invites go out from: the default calendar account, or the first Google one."""
    return next((c for c in connections.connected_with("calendar") if connections.provider_of(c) == "google"), None)


def _sent_from(ev: dict[str, Any]) -> str:
    """The account a meeting's invite went out from - it can only be changed or cancelled there."""
    return ev.get("google_account") or "google"


def require() -> None:
    if not available():
        raise InviteError("Connect your Google account (with Calendar) under Personal > Connections "
                          "to send meeting invites and make Meet links.")


def guest_emails(ev: dict[str, Any]) -> list[str]:
    """Everyone the invite goes to: the meeting's contacts that have an email address, then its guests."""
    out: list[str] = []
    for cid in ev.get("contact_ids") or []:
        c = db.get_contact(cid)
        if c and (c.get("email") or "").strip():
            out.append(c["email"].strip().lower())
    for eid in ev.get("employee_ids") or []:
        e = db.get_employee(eid)
        if e and (e.get("email") or "").strip():
            out.append(e["email"].strip().lower())
    out += [g.lower() for g in ev.get("guests") or []]
    return list(dict.fromkeys(out))


def no_email(ev: dict[str, Any]) -> list[str]:
    """Names of the meeting's contacts with no email address - they can't be invited."""
    people = [db.get_contact(cid) for cid in ev.get("contact_ids") or []]
    people += [db.get_employee(eid) for eid in ev.get("employee_ids") or []]
    return [p["name"] for p in people if p and not (p.get("email") or "").strip()]


# ---------------------------------------------------------- the Google event

def rrule(ev: dict[str, Any]) -> Optional[str]:
    """The meeting's repeat rule (timeline.rule_of) as Google's RRULE, None for a one-off."""
    rule = timeline.rule_of(ev)
    if not rule:
        return None
    freq, every = rule["freq"], int(rule.get("every") or 1)
    first = date.fromisoformat(ev["date"])
    if freq == "days":
        out = f"RRULE:FREQ=DAILY;INTERVAL={every}"
    elif freq == "weekdays":
        out = "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    elif freq == "weeks":
        # Weeks are counted Monday to Sunday, as timeline._hit does.
        days = ",".join(_BYDAY[d] for d in sorted(rule.get("on") or [first.weekday()]))
        out = f"RRULE:FREQ=WEEKLY;INTERVAL={every};BYDAY={days};WKST=MO"
    elif rule.get("nth"):
        out = f"RRULE:FREQ=MONTHLY;INTERVAL={every};BYDAY={rule['nth']}{_BYDAY[(rule.get('on') or [first.weekday()])[0]]}"
    elif first.day > 28:
        # The 29th-31st falls back to a short month's last day (timeline._hit); plain BYMONTHDAY would skip it.
        out = f"RRULE:FREQ=MONTHLY;INTERVAL={every};BYMONTHDAY={','.join(str(d) for d in range(28, first.day + 1))};BYSETPOS=-1"
    else:
        out = f"RRULE:FREQ=MONTHLY;INTERVAL={every}"
    if ev.get("repeat_until"):
        until = date.fromisoformat(ev["repeat_until"])
        if ev.get("time"):
            last = timeutil.at(until, time(23, 59, 59)).astimezone(timezone.utc)
            out += f";UNTIL={last.strftime('%Y%m%dT%H%M%SZ')}"
        else:
            out += f";UNTIL={until.strftime('%Y%m%d')}"
    return out


def _span(ev: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Google's start and end: from the meeting's (first) day and time for its duration, or all day."""
    first = date.fromisoformat(ev["date"])
    if not ev.get("time"):
        return {"date": first.isoformat()}, {"date": (first + timedelta(days=1)).isoformat()}
    h, m = (int(x) for x in ev["time"].split(":"))
    start = timeutil.at(first, time(h, m))
    end = start + timedelta(minutes=ev.get("duration") or timeline.DEFAULT_DURATION)
    tz = timeutil.tz_name()
    return {"dateTime": start.isoformat(), "timeZone": tz}, {"dateTime": end.isoformat(), "timeZone": tz}


def body(ev: dict[str, Any]) -> dict[str, Any]:
    start, end = _span(ev)
    rule = rrule(ev)
    return {"summary": ev["title"], "start": start, "end": end, "recurrence": [rule] if rule else [],
            "attendees": [{"email": e} for e in guest_emails(ev)]}


def _meet_request() -> dict[str, Any]:
    return {"createRequest": {"requestId": secrets.token_hex(8), "conferenceSolutionKey": {"type": "hangoutsMeet"}}}


def _meet_link(item: dict[str, Any]) -> Optional[str]:
    if item.get("hangoutLink"):
        return item["hangoutLink"]
    for ep in (item.get("conferenceData") or {}).get("entryPoints") or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return None


def _meet_pending(item: dict[str, Any]) -> bool:
    status = (((item.get("conferenceData") or {}).get("createRequest") or {}).get("status") or {}).get("statusCode")
    return status == "pending"


def _keep(ev: dict[str, Any], item: dict[str, Any], conn: str) -> dict[str, Any]:
    """Store what the Google event gave back on the meeting, and which account has it."""
    if _meet_pending(item) and not _meet_link(item):
        item = google.call("GET", f"{EVENTS}/{item['id']}", conn=conn)   # Meet links are usually made at once; look once more
    return db.update_project_event(ev["id"], google_event_id=item["id"], invite_link=item.get("htmlLink"),
                                   meet_link=_meet_link(item) or ev.get("meet_link"), invite_error=None, google_account=conn)


def _failed(ev: dict[str, Any], e: Exception, **fields: Any) -> dict[str, Any]:
    return db.update_project_event(ev["id"], invite_error=str(e)[:500], **fields)


# ----------------------------------------------------------------- actions

def send(ev: dict[str, Any], meet: bool = False) -> dict[str, Any]:
    """Create the meeting's Google event and email its guests the invitation.
    Returns the row: with google_event_id (and meet_link) when it went out,
    with invite_error when it didn't."""
    try:
        require()
        conn = _account()
        params: dict[str, Any] = {"sendUpdates": "all"}
        b = body(ev)
        if meet:
            b["conferenceData"] = _meet_request()
            params["conferenceDataVersion"] = 1
        return _keep(ev, google.call("POST", EVENTS, params=params, json_body=b, conn=conn), conn)
    except (ProviderError, InviteError) as e:
        return _failed(ev, e)


def sync(old: dict[str, Any], ev: dict[str, Any], meet: bool = False, force: bool = False) -> dict[str, Any]:
    """Bring the Google event in line with an edited meeting (guests get Google's update
    email). Nothing is sent when nothing they'd see changed; meet adds a Meet link."""
    gid = ev.get("google_event_id")
    add_meet = meet and not ev.get("meet_link")
    if not gid or not (force or add_meet or any(old.get(k) != ev.get(k) for k in SYNCED)):
        return ev
    conn = _sent_from(ev)
    try:
        b = body(ev)
        if set(guest_emails(old)) == set(guest_emails(ev)):
            del b["attendees"]
        else:
            # Patching replaces the whole guest list: keep the ones staying as Google has them (with their replies).
            have = {(a.get("email") or "").lower(): a for a in google.call("GET", f"{EVENTS}/{gid}", conn=conn).get("attendees") or []}
            b["attendees"] = [have.get(a["email"], a) for a in b["attendees"]]
        params: dict[str, Any] = {"sendUpdates": "all"}
        if add_meet:
            b["conferenceData"] = _meet_request()
            params["conferenceDataVersion"] = 1
        return _keep(ev, google.call("PATCH", f"{EVENTS}/{gid}", params=params, json_body=b, conn=conn), conn)
    except ProviderError as e:
        if getattr(e, "status", None) in _GONE:
            return _failed(ev, InviteError("The invite was deleted in Google Calendar. Turn on \"Send invite\" "
                                           "and save to send a new one."), google_event_id=None, invite_link=None, meet_link=None)
        return _failed(ev, e)


def cancel(ev: dict[str, Any]) -> Optional[str]:
    """Delete the meeting's Google event, emailing its guests that it's cancelled, and forget it
    on the row (if the row is still there). Returns why it couldn't, or None."""
    gid = ev.get("google_event_id")
    if not gid:
        return None
    try:
        google.call("DELETE", f"{EVENTS}/{gid}", params={"sendUpdates": "all"}, conn=_sent_from(ev))
    except ProviderError as e:
        if getattr(e, "status", None) not in _GONE:
            if db.get_project_event(ev["id"]):
                _failed(ev, e)
            return str(e)
    if db.get_project_event(ev["id"]):
        db.update_project_event(ev["id"], google_event_id=None, invite_link=None, meet_link=None, invite_error=None,
                                google_account=None)
    return None


def after_update(old: dict[str, Any], ev: dict[str, Any], invite: Optional[bool] = None,
                 meet: Optional[bool] = None) -> dict[str, Any]:
    """What an edit means for the invite. invite: True sends it (if it hasn't gone out),
    False cancels it, None leaves it as it is; meet: True adds a Meet link. A meeting
    that's cancelled, or stops being a meeting, has its invite cancelled too."""
    if meet and invite is None:
        invite = True
    if ev.get("google_event_id"):
        if invite is False or ev["status"] == "cancelled" or ev["kind"] not in timeline.WITH_PEOPLE:
            cancel(ev)
            return db.get_project_event(ev["id"])
        # Saving with invites on after a failed update tries it again.
        return sync(old, ev, bool(meet), force=bool(invite and ev.get("invite_error")))
    if invite and ev["status"] == "open" and ev["kind"] in timeline.WITH_PEOPLE:
        return send(ev, bool(meet))
    return ev
