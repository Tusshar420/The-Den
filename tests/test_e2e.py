"""The real HTTP API and the real assistant worker loop, end to end, with fake
Gmail/Calendar and a scripted model. This is the path the dashboard, an iOS
Shortcut and a voice client all use - there is no other."""

import asyncio
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import uvicorn

import api
import db
import providers as llm
import worker
from tests.helpers import NOW, ME, TestBase, FakeCalendar, FakeEmail, event, message
from assistant import planner, providers as assistant_providers


class E2E(TestBase):
    KEY = "test-key"

    def setUp(self):
        super().setUp()
        self.email = FakeEmail([message("m1", "Rahul Sharma <rahul@example.com>", "Proposal", "Can you send the proposal by Friday?")])
        self.cal = FakeCalendar([event("e1", "Standup", start=NOW.replace(day=25, hour=10, minute=0), attendees=[]),
                                 event("e3", "Project review with Rahul", start=NOW.replace(day=25, hour=15, minute=0), attendees=["rahul@example.com"])])
        self.script = []
        # fakes in place of Google, a scripted model in place of the planner's
        for patcher in (mock.patch.object(assistant_providers, "email", lambda: self.email),
                        mock.patch.object(assistant_providers, "calendar", lambda: self.cal),
                        mock.patch.object(planner, "choose_model", lambda: {"id": "claude-code", "label": "Claude Code", "provider": "claude_code"}),
                        mock.patch.object(llm, "complete", self._model),
                        mock.patch.object(api, "API_KEY", self.KEY),
                        mock.patch.object(worker, "ASSISTANT_POLL_SECONDS", 0.05),
                        mock.patch("assistant.timeutil.now", lambda: NOW)):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=0, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.addCleanup(self._stop_server)

        self.stop = None
        self.loop = asyncio.new_event_loop()

        def run_worker():
            asyncio.set_event_loop(self.loop)
            self.stop = asyncio.Event()
            self.loop.run_until_complete(worker.assistant_worker_loop(self.stop))
        self.worker_thread = threading.Thread(target=run_worker, daemon=True)
        self.worker_thread.start()
        self.addCleanup(self._stop_worker)

    def _stop_server(self):
        self.server.should_exit = True
        time.sleep(0.2)

    def _stop_worker(self):
        while self.stop is None:
            time.sleep(0.01)
        self.loop.call_soon_threadsafe(self.stop.set)
        self.worker_thread.join(3)

    def _model(self, model_cfg, messages, instructions, timeout):
        reply = self.script.pop(0)
        return llm.Completion(json.dumps(reply), 10, 5)

    def http(self, method, path, body=None, key=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-API-Key": self.KEY if key is None else key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return {"_status": e.code, **json.loads(e.read() or b"{}")}

    def ask(self, text, **extra):
        sent = self.http("POST", "/octopus", {"text": text, **extra})
        result = self.http("GET", f"/octopus/result/{sent['task']['id']}?wait=15")
        return sent, result


class Api(E2E):
    def test_calendar_question_end_to_end_with_no_model(self):
        sent, result = self.ask("What's on my calendar tomorrow?")
        d = sent["decision"]
        self.assertEqual((d["lane"], d["category"], d["model_label"]), ("assistant", "assistant", "Personal Assistant"))
        self.assertFalse(d["intent"]["needs_ai"])
        self.assertEqual(d["intent"]["intent"], "calendar.read")
        self.assertTrue(result["finished"])
        self.assertIn("You have 2 events tomorrow (Fri Sep 25):", result["text"])
        self.assertEqual(result["spoken"], "You have 2 events tomorrow (Fri Sep 25):")
        self.assertEqual(self.script, [])

    def test_coding_requests_still_go_to_the_coding_agent(self):
        sent = self.http("POST", "/octopus", {"text": "Fix the bug in the login page"})
        self.assertEqual((sent["decision"]["category"], sent["decision"]["lane"]), ("coding", "agent"))

    def test_send_email_full_loop_text_or_voice_approval_over_the_same_api(self):
        self.script = [{"action": "call", "tool": "email", "name": "send",
                        "args": {"to": "rahul@example.com", "subject": "Proposal update", "body": "I'll send the proposal tomorrow."}},
                       {"action": "final", "message": "Your email to Rahul is waiting for approval.", "spoken": "x"}]
        sent, result = self.ask("Send Rahul an email saying I'll send the proposal tomorrow.")
        self.assertEqual(self.email.sent, [], "nothing leaves before approval")
        (approval,) = result["awaiting_approval"]
        self.assertEqual(approval["preview"]["title"], "EMAIL APPROVAL")
        self.assertTrue(result["spoken"].startswith("I need your approval to send an email to rahul@example.com"))

        listed = self.http("GET", "/approvals")
        self.assertEqual(listed[0]["tool_name"], "assistant:email.send")
        self.assertNotIn("I'll send the proposal tomorrow.\"", json.dumps(json.loads(listed[0]["tool_input"]).get("args", "")),
                         "the polling list doesn't ship the stored arguments")
        self.assertNotIn("args", json.loads(listed[0]["tool_input"]))
        ctx = self.http("GET", f"/approvals/{approval['id']}/context")
        self.assertEqual((ctx["kind"], ctx["preview"]["approve"]), ("assistant", "Send"))

        decided = self.http("POST", f"/approvals/{approval['id']}/decide", {"approved": True})
        self.assertEqual(decided["assistant_result"]["status"], "approved")
        self.assertEqual(len(self.email.sent), 1)
        self.assertEqual(self.email.sent[0]["subject"], "Proposal update")
        again = self.http("POST", f"/approvals/{approval['id']}/decide", {"approved": True})
        self.assertEqual(again["assistant_result"]["status"], "approved")
        self.assertEqual(len(self.email.sent), 1, "no double send")
        final = self.http("GET", f"/octopus/result/{sent['task']['id']}")
        self.assertIn("✓ You approved. Sent to rahul@example.com", final["text"])
        self.assertEqual(final["awaiting_approval"], [])
        story = [(r["action"], r["approval"], r["result"]) for r in reversed(self.http("GET", "/assistant/activity?tool=email"))]
        self.assertEqual(story, [("send", "requested", "pending"), ("send", "approved", "ok")])

    def test_conversation_continues_by_voice_and_move_it_asks_first(self):
        first, _ = self.ask("What's on my calendar tomorrow?")
        self.script = [{"action": "call", "tool": "calendar", "name": "update_event",
                        "args": {"event_id": "e3", "start": "2026-09-25T16:00:00+05:30"}},
                       {"action": "final", "message": "Moving Project review to 4 PM needs your approval."}]
        # a voice client says "move it to 4" with no thread id - continue_recent attaches it to the chat
        second, result = self.ask("Move it to 4", continue_recent=True)
        self.assertEqual(second["decision"]["routed_by"], "thread")
        self.assertEqual(second["task"]["thread_id"], first["task"]["id"])
        self.assertEqual(self.cal.updated, [])
        (approval,) = result["awaiting_approval"]
        self.assertEqual(approval["preview"]["title"], "EVENT CHANGE APPROVAL")
        self.http("POST", f"/approvals/{approval['id']}/decide", {"approved": True})
        self.assertEqual(self.cal.updated[0][0], "e3")

    def test_denying_over_the_api_does_nothing(self):
        self.script = [{"action": "call", "tool": "calendar", "name": "cancel_event", "args": {"event_id": "e3"}},
                       {"action": "final", "message": "Waiting for approval to cancel."}]
        _, result = self.ask("Cancel my 3 PM meeting.")
        (approval,) = result["awaiting_approval"]
        out = self.http("POST", f"/approvals/{approval['id']}/decide", {"approved": False, "note": "keep it"})
        self.assertEqual(out["assistant_result"]["status"], "denied")
        self.assertEqual(self.cal.deleted, [])
        self.assertIn("You declined", self.http("GET", f"/octopus/result/{result['id']}")["text"])

    def test_the_dry_run_router_and_permissions_endpoints(self):
        r = self.http("POST", "/assistant/route", {"text": "Cancel my 4 PM meeting."})
        self.assertEqual((r["intent"], r["tool"], r["requires_approval"], r["needs_ai"]), ("calendar.cancel", "calendar", True, True))
        pol = self.http("PATCH", "/assistant/policies", {"EMAIL_SEND": "deny"})
        self.assertEqual(next(p["policy"] for p in pol if p["capability"] == "EMAIL_SEND"), "deny")
        bad = self.http("PATCH", "/assistant/policies", {"EMAIL_SEND": "sometimes"})
        self.assertEqual(bad["_status"], 400)
        self.script = [{"action": "call", "tool": "email", "name": "send",
                        "args": {"to": "a@example.com", "subject": "s", "body": "b"}}, {"action": "final", "message": "Blocked."}]
        _, result = self.ask("Send a@example.com an email saying b")
        self.assertEqual(self.email.sent, [])
        self.assertEqual(result["awaiting_approval"], [])

    def test_auth_still_protects_every_assistant_endpoint_but_the_oauth_callback(self):
        for method, path in (("GET", "/assistant/status"), ("GET", "/assistant/activity"), ("GET", "/assistant/policies"),
                             ("POST", "/assistant/connections/google/connect"), ("DELETE", "/assistant/connections/google"),
                             ("GET", "/assistant/email/overview"), ("GET", "/assistant/calendar/overview"),
                             ("GET", "/assistant/reminders"), ("POST", "/octopus/x")):
            with self.subTest(path):
                self.assertEqual(self.http(method, path, {} if method == "POST" else None, key="wrong")["_status"], 401 if "octopus/x" not in path else 404)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/assistant/oauth/google/callback?code=x&state=nope")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("expired or was already used", cm.exception.read().decode())

    def test_status_and_overviews_never_contain_credentials(self):
        self.connect_google()
        from assistant import token_store
        token_store.put("google", {"access_token": "ya29.LEAK-CHECK", "refresh_token": "1//LEAK-CHECK", "expires_at": 9e9, "scope": ""})
        for path in ("/assistant/status", "/assistant/email/overview", "/assistant/calendar/overview", "/assistant/activity"):
            text = json.dumps(self.http("GET", path))
            self.assertNotIn("LEAK-CHECK", text, path)
        st = self.http("GET", "/assistant/status")
        google = next(c for c in st["connections"] if c["id"] == "google")
        self.assertEqual((google["status"], google["account"], google["cost"]), ("connected", ME, "free"))
        self.assertEqual(sorted(google["services_granted"]), ["calendar", "drive", "email"])
        self.assertTrue(st["planner"]["available"])

    def test_reminders_are_listed_and_cancellable(self):
        _, result = self.ask("Remind me tomorrow morning to call Rahul.")
        self.assertIn("Fri Sep 25, 9:00 AM: Call Rahul", result["text"])
        (r,) = self.http("GET", "/assistant/reminders")
        self.assertEqual((r["text"], r["status"]), ("Call Rahul", "pending"))
        self.assertEqual(self.http("DELETE", f"/assistant/reminders/{r['id']}"), {"ok": True})
        self.assertEqual(self.http("GET", "/assistant/reminders"), [], "cancelled reminders drop off the list")

    def test_due_reminders_fire_once(self):
        with mock.patch.object(worker, "notify_phone", return_value=True) as push:
            db.add_reminder("Stretch", "2000-01-01T00:00:00+00:00")
            self.assertEqual(worker.fire_due_reminders(), 1)
            self.assertEqual(worker.fire_due_reminders(), 0)
            push.assert_called_once_with("Reminder", "Stretch", "reminder")


if __name__ == "__main__":
    unittest.main()
