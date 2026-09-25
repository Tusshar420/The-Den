import json
import unittest
from datetime import timedelta

import db
from tests.helpers import TestBase, FakeCalendar, FakeEmail, event
from assistant import actions, capabilities as cap


def activity(**kw):
    return [r for r in db.list_activity(200) if all(r[k] == v for k, v in kw.items())]


class Policy(TestBase):
    def test_defaults_match_the_brief(self):
        want = {"EMAIL_READ": "allow", "EMAIL_DRAFT": "allow", "EMAIL_SEND": "ask", "EMAIL_DELETE": "deny",
                "CALENDAR_READ": "allow", "CALENDAR_CREATE": "allow", "CALENDAR_UPDATE": "ask",
                "CALENDAR_DELETE": "ask", "CALENDAR_INVITE": "ask"}
        for name, policy in want.items():
            self.assertEqual(cap.policy_for(name), policy, name)

    def test_strictest_wins_and_unknown_capabilities_are_denied(self):
        self.assertEqual(cap.resolve(["CALENDAR_CREATE", "CALENDAR_INVITE"]), "ask")
        self.assertEqual(cap.resolve(["EMAIL_READ", "EMAIL_DELETE"]), "deny")
        self.assertEqual(cap.policy_for("MADE_UP"), "deny")

    def test_overrides_persist_and_validate(self):
        cap.set_policy("EMAIL_SEND", "allow")
        self.assertEqual(cap.policy_for("EMAIL_SEND"), "allow")
        with self.assertRaises(ValueError):
            cap.set_policy("EMAIL_SEND", "maybe")
        with self.assertRaises(ValueError):
            cap.set_policy("NOPE", "allow")

    def test_taint_forces_outbound_actions_to_ask_but_not_reads_or_personal_events(self):
        cap.set_policy("EMAIL_SEND", "allow")
        self.assertEqual(cap.effective(["EMAIL_SEND"], tainted=False), "allow")
        self.assertEqual(cap.effective(["EMAIL_SEND"], tainted=True), "ask")
        self.assertEqual(cap.effective(["EMAIL_READ"], tainted=True), "allow")
        self.assertEqual(cap.effective(["CALENDAR_CREATE"], tainted=True), "allow")
        cap.set_policy("EMAIL_SEND", "deny")
        self.assertEqual(cap.effective(["EMAIL_SEND"], tainted=True), "deny", "taint never loosens a policy")


class Gate(TestBase):
    def setUp(self):
        super().setUp()
        self.task = self.make_task("send it")
        self.email, self.cal = FakeEmail(), FakeCalendar([event("e1", "Project review", attendees=["rahul@example.com"])])
        self.ctx = self.ctx_for()
        self.pushed = []

    def ctx_for(self):
        return self.ctx_obj(self.task)

    def ctx_obj(self, task):
        return super().ctx(task, self.email, self.cal)

    def invoke(self, tool, action, args, tainted=False):
        return actions.invoke(tool, action, args, self.ctx, tainted=tainted, notify=lambda t, m: self.pushed.append((t, m)))

    SEND = {"to": "rahul@example.com", "subject": "Proposal update", "body": "I'll send the proposal tomorrow."}

    # -- reads and drafts run on their own
    def test_reads_and_drafts_run_without_asking(self):
        out = self.invoke("email", "search", {"query": "from:rahul"})
        self.assertEqual(out.status, "ok")
        out = self.invoke("email", "create_draft", self.SEND)
        self.assertEqual(out.status, "ok")
        self.assertEqual(len(self.email.drafts), 1)
        self.assertEqual(self.email.sent, [])

    # -- send needs approval; nothing leaves until approved
    def test_send_waits_for_approval_and_nothing_is_sent(self):
        out = self.invoke("email", "send", self.SEND)
        self.assertEqual(out.status, "awaiting_approval")
        self.assertEqual(self.email.sent, [])
        a = db.get_approval(out.approval_id)
        self.assertEqual((a["status"], a["tool_name"], a["task_id"]), ("pending", "assistant:email.send", self.task["id"]))
        card = json.loads(a["tool_input"])["preview"]
        self.assertEqual(card["title"], "EMAIL APPROVAL")
        self.assertEqual({f["label"]: f["value"] for f in card["fields"]},
                         {"To": "rahul@example.com", "Subject": "Proposal update", "Message": "I'll send the proposal tomorrow."})
        self.assertEqual((card["approve"], card["deny"]), ("Send", "Cancel"))
        self.assertEqual(self.pushed, [("Approval needed", "Send an email to rahul@example.com: Proposal update")])

    def test_approving_sends_exactly_once_and_reports_back(self):
        a = db.get_approval(self.invoke("email", "send", self.SEND).approval_id)
        result = actions.decide(a, True, ctx_factory=self.ctx_obj_factory)
        self.assertEqual((result["status"], result["ok"]), ("approved", True))
        self.assertEqual(len(self.email.sent), 1)
        self.assertEqual(self.email.sent[0]["to"], ["rahul@example.com"])
        again = actions.decide(db.get_approval(a["id"]), True, ctx_factory=self.ctx_obj_factory)
        self.assertEqual(again["status"], "approved")
        self.assertEqual(len(self.email.sent), 1, "a second click must not send again")
        self.assertIn("Sent to rahul@example.com", db.get_task(self.task["id"])["result_summary"])

    def ctx_obj_factory(self, task, memory=None):
        return super().ctx(task, self.email, self.cal, memory)

    def test_denying_sends_nothing(self):
        a = db.get_approval(self.invoke("email", "send", self.SEND).approval_id)
        result = actions.decide(a, False, "not yet", ctx_factory=self.ctx_obj_factory)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.email.sent, [])
        self.assertEqual(db.get_approval(a["id"])["status"], "denied")

    def test_the_decision_is_idempotent_across_a_race(self):
        a = db.get_approval(self.invoke("email", "send", self.SEND).approval_id)
        self.assertTrue(db.claim_approval(a["id"], True))          # someone else got there first
        result = actions.decide(a, True, ctx_factory=self.ctx_obj_factory)
        self.assertEqual(result["status"], "already_decided")
        self.assertEqual(self.email.sent, [])

    def test_content_is_redacted_after_the_decision_but_the_card_summary_stays(self):
        a = db.get_approval(self.invoke("email", "send", self.SEND).approval_id)
        actions.decide(a, True, ctx_factory=self.ctx_obj_factory)
        stored = json.loads(db.get_approval(a["id"])["tool_input"])
        self.assertNotIn("args", stored)
        self.assertTrue(stored["redacted"])
        self.assertEqual(stored["describe"], "Send an email to rahul@example.com: Proposal update")

    def test_asking_twice_for_the_same_thing_asks_once(self):
        first = self.invoke("email", "send", self.SEND)
        second = self.invoke("email", "send", self.SEND)
        self.assertEqual(first.approval_id, second.approval_id)
        self.assertEqual(len(db.list_pending_approvals()), 1)

    # -- policy variations
    def test_deny_policy_blocks_and_logs(self):
        cap.set_policy("EMAIL_SEND", "deny")
        out = self.invoke("email", "send", self.SEND)
        self.assertEqual(out.status, "denied")
        self.assertEqual(self.email.sent, [])
        self.assertEqual(len(db.list_pending_approvals()), 0)
        self.assertEqual(activity(action="send")[0]["result"], "denied")

    def test_trusted_capability_runs_without_asking_and_the_log_says_so(self):
        cap.set_policy("EMAIL_SEND", "allow")
        out = self.invoke("email", "send", self.SEND)
        self.assertEqual(out.status, "ok")
        self.assertEqual(len(self.email.sent), 1)
        self.assertEqual(activity(action="send")[0]["approval"], "trusted")

    def test_policy_tightened_while_waiting_is_respected_at_approval_time(self):
        a = db.get_approval(self.invoke("email", "send", self.SEND).approval_id)
        cap.set_policy("EMAIL_SEND", "deny")
        result = actions.decide(a, True, ctx_factory=self.ctx_obj_factory)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.email.sent, [])

    def test_prompt_injection_defence_after_reading_untrusted_text_send_cannot_skip_approval(self):
        cap.set_policy("EMAIL_SEND", "allow")  # even a trusted send...
        out = self.invoke("email", "send", self.SEND, tainted=True)  # ...asks once untrusted text has been read
        self.assertEqual(out.status, "awaiting_approval")
        self.assertEqual(self.email.sent, [])

    # -- calendar
    def test_personal_event_is_automatic_but_a_meeting_with_attendees_asks(self):
        solo = self.invoke("calendar", "create_event", {"title": "Project work", "start": "2026-09-25T15:00:00+05:30", "duration_minutes": 60})
        self.assertEqual(solo.status, "ok")
        meeting = self.invoke("calendar", "create_event", {"title": "Project discussion", "start": "2026-09-25T15:00:00+05:30",
                                                          "duration_minutes": 30, "attendees": "rahul@example.com"})
        self.assertEqual(meeting.status, "awaiting_approval")
        self.assertEqual(len(self.cal.created), 1, "only the personal event was created")
        card = json.loads(db.get_approval(meeting.approval_id)["tool_input"])["preview"]
        fields = {f["label"]: f["value"] for f in card["fields"]}
        self.assertEqual(card["title"], "MEETING APPROVAL")
        self.assertEqual((fields["Title"], fields["Date"], fields["Time"], fields["Attendees"], fields["Action"]),
                         ("Project discussion", "September 25, 2026", "3:00 PM – 3:30 PM", "rahul@example.com",
                          "Create meeting and send invitation"))
        self.assertTrue(any("Project work" in w for w in card["warnings"]), "warns about the clash with the event just created")

    def test_move_and_cancel_ask_and_show_before_and_after(self):
        moved = self.invoke("calendar", "update_event", {"event_id": "e1", "start": "2026-09-25T16:00:00+05:30"})
        self.assertEqual(moved.status, "awaiting_approval")
        self.assertEqual(self.cal.updated, [])
        pv = json.loads(db.get_approval(moved.approval_id)["tool_input"])
        self.assertEqual(sorted(pv["capabilities"]), ["CALENDAR_INVITE", "CALENDAR_UPDATE"], "attendees get notified")
        time_field = next(f["value"] for f in pv["preview"]["fields"] if f["label"] == "Time")
        self.assertEqual(time_field, "3:00 PM – 3:30 PM → 4:00 PM – 4:30 PM", "keeps the 30 minute length")
        actions.decide(db.get_approval(moved.approval_id), True, ctx_factory=self.ctx_obj_factory)
        self.assertEqual(len(self.cal.updated), 1)
        self.assertTrue(self.cal.updated[0][2], "notify attendees")

        cancel = self.invoke("calendar", "cancel_event", {"event_id": "e1"})
        self.assertEqual(cancel.status, "awaiting_approval")
        self.assertEqual(self.cal.deleted, [])
        actions.decide(db.get_approval(cancel.approval_id), True, ctx_factory=self.ctx_obj_factory)
        self.assertEqual(self.cal.deleted, [("e1", True)])

    def test_changing_a_solo_event_needs_update_permission_only(self):
        self.cal.events["solo"] = event("solo", "Focus", attendees=[])
        out = self.invoke("calendar", "update_event", {"event_id": "solo", "title": "Deep work"})
        pv = json.loads(db.get_approval(out.approval_id)["tool_input"])
        self.assertEqual(pv["capabilities"], ["CALENDAR_UPDATE"])

    # -- audit
    def test_activity_log_tells_the_whole_story_and_holds_no_content(self):
        out = self.invoke("email", "search", {"query": "from:rahul"})
        sent = self.invoke("email", "send", self.SEND)
        actions.decide(db.get_approval(sent.approval_id), True, ctx_factory=self.ctx_obj_factory)
        rows = list(reversed(db.list_activity(50)))
        story = [(r["tool"], r["action"], r["approval"], r["result"]) for r in rows]
        self.assertEqual(story, [("email", "search", "not_required", "ok"),
                                 ("email", "send", "requested", "pending"),
                                 ("email", "send", "approved", "ok")])
        blob = json.dumps(rows)
        self.assertNotIn("I'll send the proposal tomorrow", blob, "message body must not be logged")
        self.assertIn("rahul@example.com", blob)
        for r in rows:
            self.assertTrue(r["created_at"])

    def test_errors_are_logged_with_the_reason_and_scrubbed_of_tokens(self):
        class Boom(FakeEmail):
            def search(self, q, n=10):
                from assistant.providers.base import ProviderError
                raise ProviderError("Google API error: 403: bad Bearer ya29.a0AfH6SMBsecretvalue token")
        self.email = Boom()
        self.ctx = self.ctx_obj(self.task)
        out = self.invoke("email", "search", {"query": "x"})
        self.assertEqual(out.status, "error")
        self.assertNotIn("ya29", out.text)
        row = activity(action="search")[0]
        self.assertEqual(row["result"], "error")
        self.assertNotIn("ya29", row["error"])

    def test_not_connected_gives_an_instruction_not_a_stack_trace(self):
        from assistant import connections
        ctx = super().ctx(self.task)
        ctx.email = lambda: (_ for _ in ()).throw(connections.NotConnected("There's no connected account for email - connect it."))
        out = actions.invoke("email", "search", {"query": "x"}, ctx)
        self.assertEqual(out.status, "error")
        self.assertIn("connect", out.text)

    def test_expired_approvals_do_nothing_and_say_so(self):
        a = db.get_approval(self.invoke("email", "send", self.SEND).approval_id)
        from datetime import datetime, timezone
        later = datetime.now(timezone.utc) + timedelta(hours=actions.APPROVAL_TTL_HOURS + 1)  # approvals carry real timestamps
        n = actions.expire_stale(now=later)
        self.assertEqual(n, 1)
        self.assertEqual(db.get_approval(a["id"])["status"], "timed_out")
        self.assertEqual(self.email.sent, [])
        self.assertIn("No answer", db.get_task(self.task["id"])["result_summary"])

    def test_unknown_tools_and_actions_and_bad_args_are_answers_not_crashes(self):
        self.assertEqual(self.invoke("nope", "x", {}).status, "error")
        self.assertEqual(self.invoke("email", "delete", {"message_id": "m1"}).status, "error", "there is no delete action")
        self.assertEqual(self.invoke("email", "send", {"to": "Rahul", "subject": "s", "body": "b"}).status, "error")
        self.assertEqual(self.invoke("email", "send", {"to": "a@x.com\nBcc: evil@x.com", "subject": "s", "body": "b"}).status, "error")
        self.assertEqual(self.invoke("email", "send", {"to": "a@x.com", "subject": "hi\nBcc: evil@x.com", "body": "b"}).status, "error")
        self.assertEqual(len(db.list_pending_approvals()), 0, "rejected before anyone is asked to approve it")


if __name__ == "__main__":
    unittest.main()
