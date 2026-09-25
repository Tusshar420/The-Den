import base64
import email
import email.policy
import unittest
from datetime import timedelta
from unittest import mock
from urllib.parse import urlparse

from tests.helpers import NOW, ME, TestBase
from assistant import connections, net
from assistant.providers import google
from assistant.providers.base import ProviderError


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_msg(mid="m1", plain="Can you send the proposal?", html=None, attachments=(), extra_headers=()):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(plain)}, "headers": []})
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}, "headers": []})
    for name in attachments:
        parts.append({"mimeType": "application/pdf", "filename": name, "body": {"attachmentId": "a1"}})
    headers = [{"name": "From", "value": "Rahul Sharma <Rahul@Example.com>"}, {"name": "To", "value": f"Tushar <{ME}>, Priya <priya@example.com>"},
               {"name": "Cc", "value": "boss@example.com"}, {"name": "Subject", "value": "Proposal"},
               {"name": "Message-ID", "value": f"<{mid}@mail.example.com>"}, {"name": "References", "value": "<root@mail.example.com>"},
               *[{"name": k, "value": v} for k, v in extra_headers]]
    return {"id": mid, "threadId": "t1", "internalDate": str(int((NOW - timedelta(hours=5)).timestamp() * 1000)),
            "labelIds": ["INBOX", "UNREAD"], "snippet": "Can you send the proposal? &amp; more",
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


class FakeGoogle:
    """Answers the handful of Gmail / Calendar REST calls the adapters make."""

    def __init__(self):
        self.calls = []
        self.messages = {"m1": gmail_msg("m1")}
        self.events = []
        self.fail_with = None

    def __call__(self, method, url, *, headers=None, params=None, json_body=None, form=None, timeout=20):
        self.calls.append({"method": method, "url": url, "params": params, "body": json_body, "headers": headers})
        if self.fail_with:
            err, self.fail_with = self.fail_with, None
            raise err
        path = urlparse(url).path
        if path.endswith("/messages") and method == "GET":
            return {"messages": [{"id": i, "threadId": "t1"} for i in self.messages]}
        if "/messages/send" in path:
            return {"id": "sent1", "threadId": (json_body or {}).get("threadId") or "tnew"}
        if "/messages/" in path and method == "GET":
            return self.messages[path.rsplit("/", 1)[1]]
        if path.endswith("/drafts/send"):
            return {"id": "sent2", "threadId": "t9"}
        if path.endswith("/drafts") and method == "POST":
            return {"id": "dr1", "message": {"id": "dm1", "threadId": (json_body["message"].get("threadId") or "tnew")}}
        if "/drafts/" in path and method == "GET":
            return {"id": "dr1", "message": gmail_msg("dm1", "Draft body")}
        if path.endswith("/freeBusy"):
            return {"calendars": {"primary": {"busy": [{"start": "2026-09-25T10:00:00+05:30", "end": "2026-09-25T11:00:00+05:30"}]},
                                  "rahul@example.com": {"busy": [], "errors": [{"domain": "global", "reason": "notFound"}]}}}
        if path.endswith("/events") and method == "GET":
            return {"items": self.events}
        if path.endswith("/events") and method == "POST":
            return {"id": "ev9", "summary": json_body["summary"], "start": json_body["start"], "end": json_body["end"],
                    "attendees": json_body.get("attendees", []), "htmlLink": "https://cal/ev9", "status": "confirmed"}
        if "/events/" in path and method == "GET":
            return self.events[0]
        if "/events/" in path and method == "PATCH":
            return {**self.events[0], **{"summary": json_body.get("summary", self.events[0]["summary"])}}
        if "/events/" in path and method == "DELETE":
            return {}
        raise AssertionError(f"unexpected call {method} {url}")


class GoogleBase(TestBase):
    def setUp(self):
        super().setUp()
        self.connect_google()
        self.g = FakeGoogle()
        for patcher in (mock.patch.object(net, "request", self.g),
                        mock.patch.object(connections, "access_token", lambda pid: "TOKEN-ABC"),
                        mock.patch.object(connections, "expire_access_token", lambda pid: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mail, self.cal = google.GmailProvider(), google.GoogleCalendarProvider()


class Gmail(GoogleBase):
    def test_search_parses_people_dates_and_html_entities(self):
        (m,) = self.mail.search("from:rahul", 5)
        self.assertEqual((m["from_name"], m["from_email"]), ("Rahul Sharma", "rahul@example.com"))
        self.assertEqual(m["to"], [ME, "priya@example.com"])
        self.assertEqual((m["subject"], m["unread"]), ("Proposal", True))
        self.assertEqual(m["snippet"], "Can you send the proposal? & more")
        self.assertTrue(m["date"].startswith("2026-09-24T14:50:00+05:30"))
        self.assertEqual(self.g.calls[0]["headers"]["Authorization"], "Bearer TOKEN-ABC")
        self.assertEqual(self.g.calls[0]["params"]["q"], "from:rahul")

    def test_full_message_prefers_plain_falls_back_to_html_and_lists_attachments(self):
        self.g.messages["m2"] = gmail_msg("m2", plain=None, html="<div>Hello <b>there</b></div><p>Second&nbsp;line</p><script>x()</script>",
                                          attachments=["spec.pdf"])
        m = self.mail.get("m2")
        self.assertIn("Hello there", m["body"])
        self.assertNotIn("x()", m["body"])
        self.assertEqual(m["attachments"], ["spec.pdf"])
        self.assertIn("Can you send", self.mail.get("m1")["body"])

    def test_reply_is_threaded_and_keeps_the_original_context(self):
        from assistant import mailutil
        orig = self.mail.get("m1")
        composed = mailutil.compose_reply(orig, "I'll check it today.", ME)
        self.mail.send(composed["to"], composed["subject"], composed["body"], None, reply_to_message_id="m1")
        sent = next(c for c in self.g.calls if c["url"].endswith("/messages/send"))
        self.assertEqual(sent["body"]["threadId"], "t1", "stays in the original thread")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(sent["body"]["raw"]), policy=email.policy.default)
        self.assertEqual(msg["To"], "rahul@example.com")
        self.assertEqual(msg["Subject"], "Re: Proposal")
        self.assertEqual(msg["In-Reply-To"], "<m1@mail.example.com>")
        self.assertEqual(msg["References"], "<root@mail.example.com> <m1@mail.example.com>")
        body = msg.get_content()
        self.assertTrue(body.startswith("I'll check it today."))
        self.assertIn("> Can you send the proposal?", body)

    def test_reply_all_excludes_me_and_forward_marks_the_original(self):
        from assistant import mailutil
        orig = self.mail.get("m1")
        r = mailutil.compose_reply(orig, "ok", ME, reply_all=True)
        self.assertEqual((r["to"], r["cc"]), (["rahul@example.com"], ["priya@example.com", "boss@example.com"]))
        f = mailutil.compose_forward(orig, "FYI")
        self.assertEqual(f["subject"], "Fwd: Proposal")
        self.assertIn("---------- Forwarded message ---------", f["body"])
        self.assertIn("Can you send the proposal?", f["body"])

    def test_draft_and_send_draft(self):
        d = self.mail.create_draft(["rahul@example.com"], "Hello", "Body", cc=["a@x.com"])
        self.assertEqual(d["draft_id"], "dr1")
        self.assertIn("compose=dm1", d["url"])
        raw = next(c for c in self.g.calls if c["url"].endswith("/drafts"))["body"]["message"]["raw"]
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
        self.assertEqual((msg["To"], msg["Cc"], msg["Subject"]), ("rahul@example.com", "a@x.com", "Hello"))
        self.assertEqual(self.mail.get_draft("dr1")["body"], "Draft body")
        self.assertEqual(self.mail.send_draft("dr1")["message_id"], "sent2")

    def test_header_injection_is_impossible_at_the_mime_layer_too(self):
        from assistant import mailutil
        with self.assertRaises(ValueError):
            mailutil.build_mime(["a@x.com"], "hi\r\nBcc: evil@x.com", "body")


class Calendar(GoogleBase):
    ITEM = {"id": "ev1", "summary": "Project review", "status": "confirmed", "htmlLink": "https://cal/ev1",
            "start": {"dateTime": "2026-09-25T15:00:00+05:30"}, "end": {"dateTime": "2026-09-25T15:30:00+05:30"},
            "organizer": {"email": ME}, "attendees": [{"email": ME, "self": True, "responseStatus": "accepted"},
                                                      {"email": "Rahul@example.com", "displayName": "Rahul", "responseStatus": "needsAction"}]}

    def test_list_parses_timed_all_day_free_and_declined_events(self):
        self.g.events = [self.ITEM,
                         {"id": "ev2", "summary": "Holiday", "start": {"date": "2026-09-25"}, "end": {"date": "2026-09-26"}},
                         {"id": "ev3", "summary": "Optional", "transparency": "transparent",
                          "start": {"dateTime": "2026-09-25T10:00:00+05:30"}, "end": {"dateTime": "2026-09-25T11:00:00+05:30"}},
                         {"id": "ev4", "summary": "Declined", "start": {"dateTime": "2026-09-25T12:00:00+05:30"},
                          "end": {"dateTime": "2026-09-25T13:00:00+05:30"},
                          "attendees": [{"email": ME, "self": True, "responseStatus": "declined"}]},
                         {"id": "ev5", "summary": "Gone", "status": "cancelled", "start": {"date": "2026-09-25"}, "end": {"date": "2026-09-26"}}]
        events = self.cal.list_events(NOW, NOW + timedelta(days=2))
        self.assertEqual([e["id"] for e in events], ["ev1", "ev2", "ev3", "ev4"], "cancelled events are dropped")
        e1, e2, e3, e4 = events
        self.assertEqual((e1["title"], e1["start"], e1["all_day"]), ("Project review", "2026-09-25T15:00:00+05:30", False))
        self.assertEqual([a["email"] for a in e1["attendees"]], [ME, "rahul@example.com"])
        self.assertTrue(e2["all_day"])
        self.assertEqual((e3["free"], e4["declined"]), (True, True))
        p = self.g.calls[0]["params"]
        self.assertEqual((p["singleEvents"], p["orderBy"], p["timeZone"]), ("true", "startTime", "Asia/Kolkata"))

    def test_create_meeting_sends_invites_personal_event_does_not(self):
        start = NOW.replace(day=25, hour=15, minute=0)
        self.cal.create_event("Project discussion", start, start + timedelta(minutes=30), ["rahul@example.com"])
        self.cal.create_event("Focus", start, start + timedelta(hours=1))
        meeting, solo = [c for c in self.g.calls if c["method"] == "POST"]
        self.assertEqual(meeting["params"]["sendUpdates"], "all")
        self.assertEqual(meeting["body"]["attendees"], [{"email": "rahul@example.com"}])
        self.assertEqual(meeting["body"]["start"], {"dateTime": "2026-09-25T15:00:00+05:30", "timeZone": "Asia/Kolkata"})
        self.assertEqual(solo["params"]["sendUpdates"], "none")
        self.assertNotIn("attendees", solo["body"])

    def test_video_call_requests_a_meet_link(self):
        start = NOW.replace(day=25, hour=15, minute=0)
        self.cal.create_event("Sync", start, start + timedelta(minutes=30), ["a@x.com"], add_video_call=True)
        call = self.g.calls[-1]
        self.assertEqual(call["params"]["conferenceDataVersion"], 1)
        self.assertEqual(call["body"]["conferenceData"]["createRequest"]["conferenceSolutionKey"], {"type": "hangoutsMeet"})

    def test_update_merges_attendees_instead_of_replacing_them(self):
        self.g.events = [self.ITEM]
        self.cal.update_event("ev1", {"start": NOW.replace(day=25, hour=16, minute=0), "end": NOW.replace(day=25, hour=16, minute=30),
                                      "add_attendees": ["priya@example.com"]}, notify=True)
        patch = next(c for c in self.g.calls if c["method"] == "PATCH")
        self.assertEqual(patch["params"]["sendUpdates"], "all")
        self.assertEqual([a["email"] for a in patch["body"]["attendees"]], [ME, "rahul@example.com", "priya@example.com"])
        self.assertEqual(patch["body"]["start"]["dateTime"], "2026-09-25T16:00:00+05:30")

    def test_delete_notifies_only_when_asked(self):
        self.cal.delete_event("ev1", notify=False)
        self.assertEqual(self.g.calls[-1]["params"], {"sendUpdates": "none"})

    def test_freebusy_reports_own_busy_time_and_unshared_calendars(self):
        fb = self.cal.freebusy(["rahul@example.com"], NOW, NOW + timedelta(days=7))
        self.assertEqual(sorted(fb), sorted([ME, "rahul@example.com"]), "'primary' is filed under the owner's address")
        self.assertEqual(len(fb[ME]["busy"]), 1)
        self.assertEqual(fb["rahul@example.com"]["error"], "notFound")
        items = self.g.calls[-1]["body"]["items"]
        self.assertEqual(items, [{"id": "primary"}, {"id": "rahul@example.com"}])


class Errors(GoogleBase):
    def test_a_401_refreshes_once_and_retries(self):
        refreshed = []
        with mock.patch.object(connections, "expire_access_token", lambda pid: refreshed.append(pid)):
            self.g.fail_with = net.ApiError("401: expired", status=401)
            self.assertEqual(len(self.mail.search("x")), 1)
        self.assertEqual(refreshed, ["google"])

    def test_a_second_401_says_to_reconnect(self):
        def always(*a, **k):
            raise net.ApiError("401: nope", status=401)
        with mock.patch.object(net, "request", always):
            with self.assertRaises(ProviderError) as cm:
                self.mail.search("x")
        self.assertIn("Connect the account again", str(cm.exception))

    def test_common_google_errors_become_instructions(self):
        cases = [(net.ApiError("403: x", status=403, code="accessNotConfigured"), "switched on"),
                 (net.ApiError("403: x", status=403, code="insufficientPermissions"), "tick every"),
                 (net.ApiError("404: x", status=404), "couldn't find"),
                 (net.ApiError("429: x", status=429), "rate-limiting")]
        for err, expect in cases:
            with self.subTest(expect):
                self.g.fail_with = err
                with self.assertRaises(ProviderError) as cm:
                    self.mail.search("x")
                self.assertIn(expect, str(cm.exception))

    def test_not_connected_is_a_provider_error_with_directions(self):
        def raises(pid):
            raise connections.NotConnected("Google isn't connected. Connect it under Personal > Connections.")
        with mock.patch.object(connections, "access_token", raises):
            with self.assertRaises(ProviderError) as cm:
                self.cal.list_events(NOW, NOW + timedelta(days=1))
        self.assertIn("Personal > Connections", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
