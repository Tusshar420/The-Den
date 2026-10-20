"""
Which backend serves which service. A tool asks for "the email provider" and
gets whichever connected account offers it (Google today; add Microsoft by
registering its two classes here once they exist).

With several accounts connected for a service and none named, the tool gets
all of them behind one provider (AllMail / AllCalendars): searches and
listings cover every account, anything about one message or event goes to
the account that has it, and new mail or events go out from the default
account. Name an account (email(account="work")) to use only that one.
"""

from typing import Any, Optional

from assistant import connections
from assistant.providers.base import CalendarProvider, EmailProvider, ProviderError
from assistant.providers.google import GmailProvider, GoogleCalendarProvider

# provider id -> service -> class
REGISTRY: dict = {
    "google": {"email": GmailProvider, "calendar": GoogleCalendarProvider},
}

_LABELS = {"email": "an email account", "calendar": "a calendar"}
_NOT_HERE = (400, 404, 410)       # "no such message / event" - it may be in another account


def _members(service: str, account: Optional[str] = None) -> list:
    ids = [connections.resolve(service, account)] if account else connections.connected_with(service)
    out = []
    for cid in ids:
        cls = REGISTRY.get(connections.provider_of(cid), {}).get(service)
        if cls:
            out.append(cls(cid))
    if not out:
        raise connections.NotConnected(
            f"There's no connected account for {service} - connect {_LABELS[service]} under Personal > Connections.")
    return out


class _All:
    """Several accounts as one. `owner` is the id of the account the last
    look-up of a single item found it in (tools pin an approval to it)."""

    def __init__(self, members: list):
        self.members = members
        self.conn = members[0].conn
        self.owner: Optional[str] = None

    def account(self) -> str:
        return self.members[0].account()

    def accounts(self) -> list[str]:
        return [m.account() for m in self.members]

    def _holder(self, method: str, item_id: str) -> tuple[Any, Any]:
        """(the member that has this item, what `method` returned for it)."""
        last: Optional[ProviderError] = None
        for m in self.members:
            try:
                found = getattr(m, method)(item_id)
            except ProviderError as e:
                if getattr(e, "status", None) not in _NOT_HERE:
                    raise
                last = e
                continue
            self.owner = m.conn
            return m, found
        raise last or ProviderError("Not found in any connected account")


class AllMail(_All, EmailProvider):
    id = "all"

    def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        found = []
        for m in self.members:
            found += [{**msg, "account": m.account()} for msg in m.search(query, max_results)]
        found.sort(key=lambda msg: msg.get("date") or "", reverse=True)
        return found[:max_results]

    def get(self, message_id: str) -> dict[str, Any]:
        m, msg = self._holder("get", message_id)
        return {**msg, "account": m.account()}

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self._holder("get_draft", draft_id)[1]

    def create_draft(self, to, subject, body, cc=None, reply_to_message_id=None) -> dict[str, Any]:
        m = self._holder("get", reply_to_message_id)[0] if reply_to_message_id else self.members[0]
        return m.create_draft(to, subject, body, cc, reply_to_message_id)

    def send(self, to, subject, body, cc=None, reply_to_message_id=None, html=None) -> dict[str, Any]:
        m = self._holder("get", reply_to_message_id)[0] if reply_to_message_id else self.members[0]
        return m.send(to, subject, body, cc, reply_to_message_id, html)

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        return self._holder("get_draft", draft_id)[0].send_draft(draft_id)


class AllCalendars(_All, CalendarProvider):
    id = "all"

    def list_events(self, start, end, query=None, max_results=50) -> list[dict[str, Any]]:
        seen, out = set(), []
        for m in self.members:
            for ev in m.list_events(start, end, query, max_results):
                if ev["id"] not in seen:            # the same invite on two of your calendars
                    seen.add(ev["id"])
                    out.append({**ev, "account": m.account()})
        out.sort(key=lambda ev: ev["start"])
        return out[:max_results]

    def get_event(self, event_id: str) -> dict[str, Any]:
        m, ev = self._holder("get_event", event_id)
        return {**ev, "account": m.account()}

    def freebusy(self, emails, start, end) -> dict[str, dict[str, Any]]:
        """Busy on any of your calendars is busy; other people are looked up from the default account."""
        mine = {a.lower() for a in self.accounts()}
        out: dict[str, dict[str, Any]] = {}
        for i, m in enumerate(self.members):
            out.update(m.freebusy([e for e in emails if e.lower() not in mine] if i == 0 else [], start, end))
        return out

    def create_event(self, *args, **kwargs) -> dict[str, Any]:
        return self.members[0].create_event(*args, **kwargs)

    def update_event(self, event_id, changes, notify) -> dict[str, Any]:
        return self._holder("get_event", event_id)[0].update_event(event_id, changes, notify)

    def delete_event(self, event_id, notify) -> None:
        self._holder("get_event", event_id)[0].delete_event(event_id, notify)


def email(account: Optional[str] = None) -> EmailProvider:
    members = _members("email", account)
    return members[0] if len(members) == 1 else AllMail(members)


def calendar(account: Optional[str] = None) -> CalendarProvider:
    members = _members("calendar", account)
    return members[0] if len(members) == 1 else AllCalendars(members)


def default_email() -> EmailProvider:
    """Only the default email account - for mail the Den itself sends."""
    return _members("email")[0]


def default_calendar() -> CalendarProvider:
    """Only the default calendar - where the Den's own meetings are made."""
    return _members("calendar")[0]


__all__ = ["EmailProvider", "CalendarProvider", "ProviderError", "email", "calendar", "default_email",
           "default_calendar", "REGISTRY"]
