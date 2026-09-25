"""
Which backend serves which service. A tool asks for "the email provider" and
gets whichever connected account offers it (Google today; add Microsoft by
registering its two classes here once they exist).
"""

from assistant import connections
from assistant.providers.base import CalendarProvider, EmailProvider, ProviderError
from assistant.providers.google import GmailProvider, GoogleCalendarProvider

# provider id -> service -> class
REGISTRY: dict = {
    "google": {"email": GmailProvider, "calendar": GoogleCalendarProvider},
}

_LABELS = {"email": "an email account", "calendar": "a calendar"}


def _pick(service: str):
    for pid in connections.connected_with(service):
        cls = REGISTRY.get(pid, {}).get(service)
        if cls:
            return cls()
    raise connections.NotConnected(
        f"There's no connected account for {service} - connect {_LABELS[service]} under Personal > Connections.")


def email() -> EmailProvider:
    return _pick("email")


def calendar() -> CalendarProvider:
    return _pick("calendar")


__all__ = ["EmailProvider", "CalendarProvider", "ProviderError", "email", "calendar", "REGISTRY"]
