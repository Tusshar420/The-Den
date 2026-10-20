"""
Shared test scaffolding: a throwaway database and token store, a fixed
clock, and in-memory fake email / calendar providers that record what was
done to them. No network, no Google account.

Run everything:   .venv/bin/python -m unittest discover -s tests -t .
"""

import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["OCTOPUS_TZ"] = "Asia/Kolkata"
os.environ["OCTOPUS_TOKEN_BACKEND"] = "file"
# Tests never talk to a real model or push service, whatever the shell has set.
for _var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ACTIVITYSMITH_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
    os.environ.pop(_var, None)

import db  # noqa: E402
from assistant import tools  # noqa: E402
from assistant.providers.base import CalendarProvider, EmailProvider  # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")
# Thursday 24 Sep 2026, 19:50 IST - the day this feature was written.
NOW = datetime(2026, 9, 24, 19, 50, tzinfo=TZ)
ME = "tushar@example.com"


class TestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["OCTOPUS_TOKEN_FILE"] = str(Path(self._tmp.name) / "tokens.json")
        self._old_path, self._old_conn = db.DB_PATH, db._conn
        db.DB_PATH = Path(self._tmp.name) / "queue.db"
        db._conn = None
        self.addCleanup(self._restore_db)
        self.project = db.add_project("General", self._tmp.name)
        import mailer
        self._old_mail = mailer.RESTART_MARK
        mailer.RESTART_MARK = Path(self._tmp.name) / "restart_requested.json"
        self.addCleanup(lambda: setattr(mailer, "RESTART_MARK", self._old_mail))
        import api
        api._failures.clear()          # tests send wrong keys on purpose; don't let one lock out the next
        import providers as llm

        def no_real_model_calls(*a, **k):
            raise AssertionError("a test made a real model call")
        self._old_complete, llm.complete = llm.complete, no_real_model_calls
        TestBase._orig_complete = staticmethod(self._old_complete)
        self.addCleanup(lambda: setattr(llm, "complete", self._old_complete))

    def _restore_db(self):
        if db._conn is not None:
            db._conn.close()
        db.DB_PATH, db._conn = self._old_path, self._old_conn

    def make_task(self, prompt: str, thread_id: Optional[int] = None) -> dict[str, Any]:
        return db.add_task(self.project["id"], prompt, 0, thread_id, lane="assistant", model_id="octopus-tools",
                           category="assistant", routed_by="rules")

    def ctx(self, task=None, email=None, calendar=None, memory=None) -> tools.ToolContext:
        return tools.ToolContext(email=lambda: email or FakeEmail(), calendar=lambda: calendar or FakeCalendar(),
                                 now=lambda: NOW, task=task, memory=memory if memory is not None else {})

    def connect_google(self, scopes=None):
        from assistant import oauth
        scopes = scopes or [s for svc in oauth.GOOGLE.service_scopes.values() for s in svc]
        db.save_connection("google", ME, "connected", scopes)


def message(mid="m1", frm="Rahul Sharma <rahul@example.com>", subject="Proposal", body="Hi, can you send the proposal?",
            when=NOW - timedelta(hours=5), to=None, thread="t1"):
    from assistant import mailutil
    name, addr = mailutil.parse_address(frm)
    return {"id": mid, "thread_id": thread, "from_name": name, "from_email": addr, "to": to or [ME], "cc": [],
            "reply_to": "", "subject": subject, "date": when.isoformat(),
            "date_text": f"{when.strftime('%a, %b')} {when.day}, {when.year} at 2:50 PM", "snippet": body[:80],
            "unread": True, "body": body, "attachments": [], "message_id_header": f"<{mid}@mail.example.com>",
            "references": None}


class FakeEmail(EmailProvider):
    id = "fake"

    def __init__(self, messages=None):
        self.messages = {m["id"]: m for m in (messages if messages is not None else [message()])}
        self.sent: list[dict[str, Any]] = []
        self.drafts: dict[str, dict[str, Any]] = {}
        self.queries: list[str] = []

    def account(self):
        return ME

    def search(self, query, max_results=10):
        self.queries.append(query)
        out = []
        for m in self.messages.values():
            hay = f"{m['from_name']} {m['from_email']} {' '.join(m['to'])} {m['subject']} {m['body']}".lower()
            terms = [t.split(":", 1)[-1].strip('"()').lower() for t in query.replace(" OR ", " ").split()
                     if not t.startswith(("in:", "is:", "newer_than"))]
            if all(t in hay for t in terms if t):
                out.append({k: v for k, v in copy.deepcopy(m).items() if k != "body"})
        return out[:max_results]

    def get(self, message_id):
        return copy.deepcopy(self.messages[message_id])

    def create_draft(self, to, subject, body, cc=None, reply_to_message_id=None):
        did = f"d{len(self.drafts) + 1}"
        self.drafts[did] = {"to": to, "cc": cc or [], "subject": subject, "body": body}
        return {"draft_id": did, "message_id": f"dm{did}", "thread_id": None, "url": f"https://mail.example.com/{did}"}

    def get_draft(self, draft_id):
        return {"draft_id": draft_id, **self.drafts[draft_id]}

    def send(self, to, subject, body, cc=None, reply_to_message_id=None):
        self.sent.append({"to": to, "cc": cc or [], "subject": subject, "body": body, "reply_to": reply_to_message_id})
        return {"message_id": f"s{len(self.sent)}", "thread_id": "t1"}

    def send_draft(self, draft_id):
        d = self.drafts[draft_id]
        self.sent.append({**d, "draft_id": draft_id})
        return {"message_id": f"s{len(self.sent)}", "thread_id": "t1"}


def event(eid="e1", title="Project review", start=None, end=None, attendees=None, all_day=False):
    start = start or datetime(2026, 9, 25, 15, 0, tzinfo=TZ)
    end = end or start + timedelta(minutes=30)
    return {"id": eid, "title": title, "start": start.isoformat(), "end": end.isoformat(), "all_day": all_day,
            "location": None, "description": "", "link": f"https://cal.example.com/{eid}", "meet_link": None,
            "organizer": ME, "status": "confirmed", "free": False, "declined": False,
            "attendees": [{"email": a, "name": a.split("@")[0].title(), "status": "accepted", "self": False}
                          for a in (attendees or [])]}


class FakeCalendar(CalendarProvider):
    id = "fake"

    def __init__(self, events=None, others_visible=True):
        self.events = {e["id"]: e for e in (events if events is not None else [])}
        self.others_visible = others_visible
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any], bool]] = []
        self.deleted: list[tuple[str, bool]] = []

    def account(self):
        return ME

    def list_events(self, start, end, query=None, max_results=50):
        from assistant import timeutil
        return sorted((copy.deepcopy(e) for e in self.events.values()
                       if timeutil.parse_iso(e["start"]) < end and timeutil.parse_iso(e["end"]) > start),
                      key=lambda e: e["start"])

    def get_event(self, event_id):
        if event_id not in self.events:
            raise KeyError(event_id)
        return copy.deepcopy(self.events[event_id])

    def freebusy(self, emails, start, end):
        from assistant import timeutil
        mine = [(timeutil.parse_iso(e["start"]), timeutil.parse_iso(e["end"])) for e in self.events.values()]
        out = {ME: {"busy": mine, "error": None}}
        for e in emails:
            out[e] = {"busy": [], "error": None if self.others_visible else "notFound"}
        return out

    def create_event(self, title, start, end, attendees=None, description=None, location=None, all_day=False,
                     add_video_call=False, repeat_days=None, repeat_until=None):
        eid = f"new{len(self.created) + 1}"
        self.created.append({"title": title, "start": start, "end": end, "attendees": attendees or [],
                             "repeat_days": repeat_days, "repeat_until": repeat_until})
        ev = event(eid, title, start, end, attendees)
        ev["recurring"] = bool(repeat_days)
        self.events[eid] = ev
        return copy.deepcopy(ev)

    def update_event(self, event_id, changes, notify):
        self.updated.append((event_id, changes, notify))
        ev = self.events[event_id]
        if "start" in changes:
            ev["start"] = changes["start"].isoformat()
        if "end" in changes:
            ev["end"] = changes["end"].isoformat()
        if "title" in changes:
            ev["title"] = changes["title"]
        return copy.deepcopy(ev)

    def delete_event(self, event_id, notify):
        self.deleted.append((event_id, notify))
        self.events.pop(event_id, None)
