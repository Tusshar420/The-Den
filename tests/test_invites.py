"""Meeting invites (invites.py): a project-calendar meeting sent as a Google Calendar
invite with a Meet link, kept in step when it's edited, and cancelled when it's
cancelled or deleted - through the API, against a fake Google."""

from unittest import mock
from urllib.parse import urlparse

import api
import db
import invites
import timeline
from assistant import connections, net
from tests.helpers import NOW, TestBase


class FakeCal:
    def __init__(self):
        self.calls, self.fail = [], None
        self.attendees = []

    def __call__(self, method, url, *, headers=None, params=None, json_body=None, form=None, timeout=20):
        self.calls.append({"method": method, "path": urlparse(url).path, "params": params or {}, "body": json_body})
        if self.fail:
            err, self.fail = self.fail, None
            raise err
        if method == "POST":
            out = {"id": "g1", "htmlLink": "https://calendar.google.com/event?eid=g1", **json_body}
            if "conferenceData" in json_body:
                out["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
            return out
        if method == "GET":
            return {"id": "g1", "attendees": self.attendees}
        if method == "PATCH":
            out = {"id": "g1", "htmlLink": "https://calendar.google.com/event?eid=g1"}
            if "conferenceData" in json_body:
                out["hangoutLink"] = "https://meet.google.com/new-link"
            return out
        return {}


class Base(TestBase):
    def setUp(self):
        super().setUp()
        self.connect_google()
        self.g = FakeCal()
        for p in (mock.patch.object(net, "request", self.g),
                  mock.patch.object(connections, "access_token", lambda pid: "TOKEN"),
                  mock.patch.object(connections, "expire_access_token", lambda pid: None)):
            p.start()
            self.addCleanup(p.stop)
        self.pid = db.add_project("shop", self._tmp.name + "/shop")["id"]
        self.priya = db.add_contact({"name": "Priya", "email": "Priya@Acme.com"})["id"]

    def add(self, **kw):
        body = {"kind": "meeting", "title": "Kickoff", "date": "2026-10-01", "time": "15:00", **kw}
        return api.add_timeline_event(self.pid, api.TimelineEventIn(**body))

    def patch(self, ev, **kw):
        return api.update_timeline_event(ev["id"], api.TimelineEventUpdateIn(**kw))

    def sent(self, method):
        return [c for c in self.g.calls if c["method"] == method]


class Send(Base):
    def test_invite_with_meet_link_emails_contacts_and_guests(self):
        ev = self.add(contact_ids=[self.priya], guests=["Sam@Example.com", "sam@example.com"], duration=30,
                      invite=True, meet=True)
        (post,) = self.sent("POST")
        self.assertEqual(post["params"], {"sendUpdates": "all", "conferenceDataVersion": 1})
        b = post["body"]
        self.assertEqual(b["summary"], "Kickoff")
        self.assertEqual(b["attendees"], [{"email": "priya@acme.com"}, {"email": "sam@example.com"}])
        self.assertEqual(b["conferenceData"]["createRequest"]["conferenceSolutionKey"], {"type": "hangoutsMeet"})
        self.assertTrue(b["start"]["dateTime"].startswith("2026-10-01T15:00:00"))
        self.assertTrue(b["end"]["dateTime"].startswith("2026-10-01T15:30:00"))
        self.assertNotIn("description", b, "notes stay in the Den")
        self.assertEqual((ev["google_event_id"], ev["meet_link"]), ("g1", "https://meet.google.com/abc-defg-hij"))
        self.assertEqual(ev["google_account"], "google", "later changes must go to the account that sent it")
        self.assertIsNone(ev["invite_error"])

    def test_without_invite_nothing_goes_to_google(self):
        ev = self.add(guests=["sam@example.com"])
        self.assertEqual(self.g.calls, [])
        self.assertEqual(ev["guests"], ["sam@example.com"])

    def test_all_day_and_repeating(self):
        self.add(time=None, invite=True)
        b = self.g.calls[-1]["body"]
        self.assertEqual((b["start"], b["end"]), ({"date": "2026-10-01"}, {"date": "2026-10-02"}))
        # 2026-10-01 is a Thursday: weekly on Mon + Thu, every 2 weeks, until the end of November.
        self.add(repeat_rule={"freq": "weeks", "every": 2, "on": [0, 3]}, repeat_until="2026-11-30", invite=True)
        (rule,) = self.g.calls[-1]["body"]["recurrence"]
        self.assertTrue(rule.startswith("RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH;WKST=MO;UNTIL=20261130T"), rule)

    def test_monthly_rules(self):
        ev = {"date": "2026-10-31", "repeat_rule": {"freq": "months", "every": 1}}
        self.assertEqual(invites.rrule(ev), "RRULE:FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=28,29,30,31;BYSETPOS=-1")
        ev = {"date": "2026-10-13", "repeat_rule": {"freq": "months", "every": 3, "nth": 2, "on": [1]}}
        self.assertEqual(invites.rrule(ev), "RRULE:FREQ=MONTHLY;INTERVAL=3;BYDAY=2TU")
        self.assertEqual(invites.rrule({"date": "2026-10-13", "repeat_rule": {"freq": "weekdays", "every": 1}}),
                         "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")

    def test_not_connected_refuses_before_saving(self):
        db.save_connection("google", "me@example.com", "disconnected", [])
        with self.assertRaises(api.HTTPException) as cm:
            self.add(invite=True)
        self.assertEqual(cm.exception.status_code, 422)
        self.assertIn("Connections", cm.exception.detail)
        self.assertEqual(db.list_project_events(self.pid), [])

    def test_google_failure_keeps_the_meeting_with_the_error(self):
        self.g.fail = net.ApiError("boom", 500, code="backendError")
        ev = self.add(invite=True)
        self.assertIsNone(ev["google_event_id"])
        self.assertIn("Google", ev["invite_error"])
        ev = self.patch(ev, invite=True)                       # saving again retries
        self.assertEqual(ev["google_event_id"], "g1")
        self.assertIsNone(ev["invite_error"])

    def test_bad_guest_and_duration(self):
        for kw in ({"guests": ["not-an-email"]}, {"duration": 2}):
            with self.assertRaises(api.HTTPException) as cm:
                self.add(**kw)
            self.assertEqual(cm.exception.status_code, 422)


class Edit(Base):
    def setUp(self):
        super().setUp()
        self.ev = self.add(contact_ids=[self.priya], invite=True)
        self.g.calls.clear()

    def test_reschedule_updates_google_and_guests(self):
        ev = self.patch(self.ev, time="16:00")
        (p,) = self.sent("PATCH")
        self.assertEqual(p["params"], {"sendUpdates": "all"})
        self.assertTrue(p["body"]["start"]["dateTime"].startswith("2026-10-01T16:00"))
        self.assertNotIn("attendees", p["body"], "same guests - the list (and their replies) is left alone")
        self.assertEqual(ev["google_event_id"], "g1")

    def test_private_changes_send_nothing(self):
        self.patch(self.ev, notes="our side only", remind_days=[2])
        self.assertEqual(self.g.calls, [])

    def test_new_guest_keeps_existing_replies(self):
        self.g.attendees = [{"email": "priya@acme.com", "responseStatus": "accepted"}]
        self.patch(self.ev, guests=["sam@example.com"])
        (p,) = self.sent("PATCH")
        self.assertEqual(p["body"]["attendees"], [{"email": "priya@acme.com", "responseStatus": "accepted"},
                                                  {"email": "sam@example.com"}])

    def test_add_meet_later(self):
        ev = self.patch(self.ev, meet=True)
        self.assertEqual(self.sent("PATCH")[0]["params"]["conferenceDataVersion"], 1)
        self.assertEqual(ev["meet_link"], "https://meet.google.com/new-link")

    def test_cancel_status_invite_off_and_delete_cancel_in_google(self):
        ev = self.patch(self.ev, status="cancelled")
        (d,) = self.sent("DELETE")
        self.assertEqual(d["params"], {"sendUpdates": "all"})
        self.assertIsNone(ev["google_event_id"])
        self.assertEqual(self.patch(ev, status="open")["google_event_id"], None, "reopening doesn't resend by itself")

        ev = self.patch(ev, invite=True)
        self.g.calls.clear()
        self.assertIsNone(self.patch(ev, invite=False)["google_event_id"])
        self.assertEqual(len(self.sent("DELETE")), 1)

        ev = self.patch(ev, invite=True)
        self.g.calls.clear()
        self.assertEqual(api.delete_timeline_event(ev["id"]), {"ok": True, "invite_cancelled": True})
        self.assertEqual(len(self.sent("DELETE")), 1)

    def test_deleted_in_google(self):
        self.g.fail = net.ApiError("gone", 404, code="notFound")
        ev = self.patch(self.ev, title="Kickoff v2")
        self.assertIsNone(ev["google_event_id"])
        self.assertIn("deleted in Google Calendar", ev["invite_error"])
        ev = self.patch(ev, invite=True)                       # sends a new one
        self.assertEqual(self.sent("POST")[-1]["body"]["summary"], "Kickoff v2")
        self.assertEqual((ev["google_event_id"], ev["invite_error"]), ("g1", None))

    def test_delete_still_deletes_when_google_fails(self):
        self.g.fail = net.ApiError("boom", 500, code="backendError")
        out = api.delete_timeline_event(self.ev["id"])
        self.assertIn("couldn't be cancelled", out["warning"])
        self.assertIsNone(db.get_project_event(self.ev["id"]))

    def test_no_longer_a_meeting(self):
        ev = self.patch(self.ev, kind="demo")
        self.assertEqual(len(self.sent("DELETE")), 1)
        self.assertIsNone(ev["google_event_id"])
        self.assertEqual((ev["guests"], ev["contact_ids"]), ([], []))
