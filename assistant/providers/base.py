"""
What a mail or calendar backend must offer. Tools (assistant/tools/) only
ever talk to these interfaces, so Gmail and Outlook are interchangeable and
a new provider is one new file.

Shapes (plain dicts, so they go straight into JSON):

  message  {id, thread_id, from_name, from_email, to[], cc[], reply_to,
            subject, date (ISO), date_text, snippet, body?, attachments[],
            message_id_header?, references?}
  event    {id, title, start, end (ISO, aware), all_day, location,
            description, attendees[{email, name, status}], organizer,
            link, meet_link}
"""

from datetime import datetime
from typing import Any, Optional


class ProviderError(Exception):
    """A failure with a message fit to show the user - never token material."""


class EmailProvider:
    id = ""

    def account(self) -> str:
        raise NotImplementedError

    def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get(self, message_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def create_draft(self, to: list[str], subject: str, body: str, cc: Optional[list[str]] = None,
                     reply_to_message_id: Optional[str] = None) -> dict[str, Any]:
        """-> {draft_id, message_id, thread_id, url}"""
        raise NotImplementedError

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        """-> {draft_id, to[], cc[], subject, body}"""
        raise NotImplementedError

    def send(self, to: list[str], subject: str, body: str, cc: Optional[list[str]] = None,
             reply_to_message_id: Optional[str] = None) -> dict[str, Any]:
        """-> {message_id, thread_id}"""
        raise NotImplementedError

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        raise NotImplementedError


class CalendarProvider:
    id = ""

    def account(self) -> str:
        raise NotImplementedError

    def list_events(self, start: datetime, end: datetime, query: Optional[str] = None,
                    max_results: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_event(self, event_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def freebusy(self, emails: list[str], start: datetime, end: datetime) -> dict[str, dict[str, Any]]:
        """-> {email: {"busy": [(start, end), ...], "error": str | None}}. The
        account's own calendar is always included under its own address."""
        raise NotImplementedError

    def create_event(self, title: str, start: datetime, end: datetime, attendees: Optional[list[str]] = None,
                     description: Optional[str] = None, location: Optional[str] = None,
                     all_day: bool = False, add_video_call: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def update_event(self, event_id: str, changes: dict[str, Any], notify: bool) -> dict[str, Any]:
        """changes: any of title, start, end, description, location, add_attendees[]."""
        raise NotImplementedError

    def delete_event(self, event_id: str, notify: bool) -> None:
        raise NotImplementedError
