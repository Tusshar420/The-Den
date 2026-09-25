"""
Gmail and Google Calendar through their official REST APIs.

Scopes requested (see oauth.GOOGLE): gmail.readonly + gmail.compose (read,
draft, send - never delete) and calendar.events + calendar.freebusy.
Everything goes through assistant.net so errors are scrubbed and the HTTP
layer can be faked in tests.
"""

import base64
import html
import re
import secrets
from datetime import datetime
from typing import Any, Optional

import db
from assistant import connections, mailutil, net, oauth, timeutil
from assistant.providers.base import CalendarProvider, EmailProvider, ProviderError

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"


def _friendly(e: net.ApiError) -> ProviderError:
    code = (e.code or "").lower()
    if e.status == 403 and code in ("accessnotconfigured", "service_disabled"):
        return ProviderError("That Google API isn't switched on for your Google Cloud project. Enable the Gmail API "
                             "and Google Calendar API in the Cloud Console (free), then try again.")
    if e.status == 403 and ("scope" in code or "insufficient" in code or "permission" in code):
        return ProviderError("Google didn't grant that permission. Disconnect and connect again, and tick every "
                             "box on Google's consent screen.")
    if e.status == 404:
        return ProviderError("Google couldn't find that item - it may have been deleted or moved.")
    if e.status == 429 or code in ("ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded"):
        return ProviderError("Google is rate-limiting requests right now. Try again in a minute.")
    return ProviderError(f"Google API error: {e}")


def call(method: str, url: str, params: Optional[dict[str, Any]] = None, json_body: Any = None) -> Any:
    """One authenticated Google call. A 401 means the access token was
    revoked or expired early: refresh once and retry."""
    for attempt in (1, 2):
        try:
            token = connections.access_token("google")
        except (connections.NotConnected, oauth.ReauthRequired) as e:
            raise ProviderError(str(e))
        try:
            return net.request(method, url, headers={"Authorization": f"Bearer {token}"},
                               params=params, json_body=json_body)
        except net.ApiError as e:
            if e.status == 401 and attempt == 1:
                connections.expire_access_token("google")
                continue
            if e.status == 401:
                raise ProviderError("Google rejected the stored sign-in. Connect the account again.")
            raise _friendly(e)
    raise ProviderError("Google request failed")


def _account() -> str:
    row = db.get_connection("google")
    return ((row or {}).get("account") or "").lower()


# -------------------------------------------------------------------- Gmail

def _decode(data: str, charset: str = "utf-8") -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _walk(payload: dict[str, Any], out: dict[str, Any]) -> None:
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    if payload.get("filename"):
        out["attachments"].append(payload["filename"])
    elif mime in ("text/plain", "text/html") and body.get("data"):
        ctype = _headers(payload).get("content-type", "")
        m = re.search(r"charset=\"?([\w\-]+)", ctype, re.I)
        out["plain" if mime == "text/plain" else "html"].append(_decode(body["data"], m.group(1) if m else "utf-8"))
    for part in payload.get("parts", []) or []:
        _walk(part, out)


def _parse_message(m: dict[str, Any], full: bool = False) -> dict[str, Any]:
    payload = m.get("payload", {}) or {}
    h = _headers(payload)
    from_name, from_email = mailutil.parse_address(h.get("from", ""))
    reply_to = mailutil.split_addresses(h.get("reply-to"))
    when = None
    if m.get("internalDate"):
        when = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timeutil.local_tz())
    out: dict[str, Any] = {
        "id": m.get("id"), "thread_id": m.get("threadId"),
        "from_name": from_name, "from_email": from_email,
        "to": [a for _, a in mailutil.split_addresses(h.get("to"))],
        "cc": [a for _, a in mailutil.split_addresses(h.get("cc"))],
        "reply_to": reply_to[0][1] if reply_to else "",
        "subject": h.get("subject", "(no subject)"),
        "date": when.isoformat() if when else None,
        "date_text": (f"{when.strftime('%a, %b')} {when.day}, {when.year} at {timeutil.fmt_time(when)}" if when else ""),
        "snippet": html.unescape(m.get("snippet", "")),
        "unread": "UNREAD" in (m.get("labelIds") or []),
        "message_id_header": h.get("message-id"), "references": h.get("references"),
    }
    if full:
        parts: dict[str, Any] = {"plain": [], "html": [], "attachments": []}
        _walk(payload, parts)
        text = "\n".join(parts["plain"]) or mailutil.html_to_text("\n".join(parts["html"]))
        out["body"] = text[:mailutil.MAX_BODY_CHARS]
        out["attachments"] = parts["attachments"]
    return out


_META = ["From", "To", "Cc", "Subject", "Date", "Reply-To", "Message-ID", "References"]


class GmailProvider(EmailProvider):
    id = "google"

    def account(self) -> str:
        return _account()

    def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        listing = call("GET", f"{GMAIL}/messages", params={"q": query, "maxResults": max(1, min(max_results, 20))})
        out = []
        for stub in listing.get("messages", []) or []:
            m = call("GET", f"{GMAIL}/messages/{stub['id']}", params={"format": "metadata", "metadataHeaders": _META})
            out.append(_parse_message(m))
        return out

    def get(self, message_id: str) -> dict[str, Any]:
        return _parse_message(call("GET", f"{GMAIL}/messages/{message_id}", params={"format": "full"}), full=True)

    def _raw(self, to, subject, body, cc, reply_to_message_id) -> tuple[str, Optional[str]]:
        in_reply_to = references = thread_id = None
        if reply_to_message_id:
            orig = _parse_message(call("GET", f"{GMAIL}/messages/{reply_to_message_id}",
                                       params={"format": "metadata", "metadataHeaders": _META}))
            in_reply_to, references, thread_id = orig["message_id_header"], orig["references"], orig["thread_id"]
        return mailutil.build_mime(to, subject, body, cc, in_reply_to, references), thread_id

    def create_draft(self, to, subject, body, cc=None, reply_to_message_id=None) -> dict[str, Any]:
        raw, thread_id = self._raw(to, subject, body, cc, reply_to_message_id)
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        d = call("POST", f"{GMAIL}/drafts", json_body={"message": message})
        mid = (d.get("message") or {}).get("id")
        return {"draft_id": d.get("id"), "message_id": mid, "thread_id": (d.get("message") or {}).get("threadId"),
                "url": f"https://mail.google.com/mail/u/0/#drafts?compose={mid}" if mid else None}

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        d = call("GET", f"{GMAIL}/drafts/{draft_id}", params={"format": "full"})
        m = _parse_message(d.get("message") or {}, full=True)
        return {"draft_id": draft_id, "to": m["to"], "cc": m["cc"], "subject": m["subject"], "body": m.get("body", "")}

    def send(self, to, subject, body, cc=None, reply_to_message_id=None) -> dict[str, Any]:
        raw, thread_id = self._raw(to, subject, body, cc, reply_to_message_id)
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        sent = call("POST", f"{GMAIL}/messages/send", json_body=payload)
        return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        sent = call("POST", f"{GMAIL}/drafts/send", json_body={"id": draft_id})
        return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}


# ----------------------------------------------------------------- Calendar

def _event_time(node: dict[str, Any]) -> tuple[datetime, bool]:
    if "dateTime" in node:
        return timeutil.parse_iso(node["dateTime"]), False
    return timeutil.parse_iso(node["date"] + "T00:00:00"), True


def _parse_event(item: dict[str, Any]) -> dict[str, Any]:
    start, all_day = _event_time(item.get("start", {}))
    end, _ = _event_time(item.get("end", item.get("start", {})))
    attendees = [{"email": (a.get("email") or "").lower(), "name": a.get("displayName"),
                  "status": a.get("responseStatus"), "self": bool(a.get("self"))}
                 for a in item.get("attendees", []) or []]
    return {
        "id": item.get("id"), "title": item.get("summary") or "(no title)",
        "start": start.isoformat(), "end": end.isoformat(), "all_day": all_day,
        "location": item.get("location"), "description": (item.get("description") or "")[:500],
        "attendees": attendees, "organizer": (item.get("organizer") or {}).get("email"),
        "link": item.get("htmlLink"), "meet_link": item.get("hangoutLink"),
        "status": item.get("status"),
        # Doesn't block time: marked "free", or one the owner declined.
        "free": item.get("transparency") == "transparent",
        "declined": any(a["self"] and a["status"] == "declined" for a in attendees),
    }


def _when(dt: datetime, all_day: bool = False) -> dict[str, str]:
    if all_day:
        return {"date": dt.date().isoformat()}
    return {"dateTime": dt.isoformat(), "timeZone": timeutil.tz_name()}


class GoogleCalendarProvider(CalendarProvider):
    id = "google"

    def account(self) -> str:
        return _account()

    def list_events(self, start, end, query=None, max_results=50) -> list[dict[str, Any]]:
        data = call("GET", f"{CAL}/calendars/primary/events", params={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": max(1, min(max_results, 100)), "q": query,
            "timeZone": timeutil.tz_name()})
        return [_parse_event(i) for i in data.get("items", []) or [] if i.get("status") != "cancelled"]

    def get_event(self, event_id: str) -> dict[str, Any]:
        return _parse_event(call("GET", f"{CAL}/calendars/primary/events/{event_id}"))

    def freebusy(self, emails, start, end) -> dict[str, dict[str, Any]]:
        me = self.account() or "primary"
        items = [{"id": "primary"}] + [{"id": e} for e in emails if e.lower() != me]
        data = call("POST", f"{CAL}/freeBusy", json_body={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": timeutil.tz_name(), "items": items})
        out: dict[str, dict[str, Any]] = {}
        for key, cal in (data.get("calendars") or {}).items():
            errors = cal.get("errors") or []
            out[me if key == "primary" else key.lower()] = {
                "busy": [(timeutil.parse_iso(b["start"]), timeutil.parse_iso(b["end"])) for b in cal.get("busy", [])],
                "error": errors[0].get("reason", "unavailable") if errors else None,
            }
        return out

    def create_event(self, title, start, end, attendees=None, description=None, location=None,
                     all_day=False, add_video_call=False) -> dict[str, Any]:
        body: dict[str, Any] = {"summary": title, "start": _when(start, all_day), "end": _when(end, all_day)}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        params: dict[str, Any] = {"sendUpdates": "all" if attendees else "none"}
        if add_video_call:
            body["conferenceData"] = {"createRequest": {"requestId": secrets.token_hex(8),
                                                        "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
            params["conferenceDataVersion"] = 1
        return _parse_event(call("POST", f"{CAL}/calendars/primary/events", params=params, json_body=body))

    def update_event(self, event_id, changes, notify) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if "title" in changes:
            body["summary"] = changes["title"]
        if "description" in changes:
            body["description"] = changes["description"]
        if "location" in changes:
            body["location"] = changes["location"]
        if "start" in changes:
            body["start"] = _when(changes["start"])
        if "end" in changes:
            body["end"] = _when(changes["end"])
        if changes.get("add_attendees"):
            # PATCH replaces the attendee list, so merge with the current one.
            current = self.get_event(event_id)["attendees"]
            have = {a["email"] for a in current}
            body["attendees"] = [{"email": a["email"]} for a in current] + \
                                [{"email": e} for e in changes["add_attendees"] if e not in have]
        return _parse_event(call("PATCH", f"{CAL}/calendars/primary/events/{event_id}",
                                 params={"sendUpdates": "all" if notify else "none"}, json_body=body))

    def delete_event(self, event_id, notify) -> None:
        call("DELETE", f"{CAL}/calendars/primary/events/{event_id}",
             params={"sendUpdates": "all" if notify else "none"})
