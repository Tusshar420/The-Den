import unittest

from tests.helpers import TestBase
from assistant import capabilities as cap
from assistant import intent

# (text, intent, needs_ai, requires_approval)
CLAIMED = [
    ("Check my calendar for tomorrow.", "calendar.read", False, False),
    ("What's on my calendar tomorrow?", "calendar.read", False, False),
    ("Do I have anything scheduled at 3 PM?", "calendar.availability", False, False),
    ("Am I free between 2 and 5 PM tomorrow?", "calendar.availability", False, False),
    ("Find a free 30-minute slot with Rahul next week.", "calendar.find_slot", True, False),
    ("Block 3 PM tomorrow for project work.", "calendar.create_event", False, False),
    ("Schedule a meeting with Rahul tomorrow at 3 PM for 30 minutes.", "calendar.create_meeting", True, True),
    ("Move my 3 PM meeting to 4 PM.", "calendar.modify", True, True),
    ("Cancel my 4 PM meeting.", "calendar.cancel", True, True),
    ("Find the latest email from Rahul.", "email.search", False, False),
    ("Read Rahul's latest email.", "email.read", False, False),
    ("Check my inbox", "email.search", False, False),
    ("Draft an email to the client explaining the delay.", "email.draft", True, False),
    ("Draft a response to Rahul saying I'll send the proposal tomorrow.", "email.draft", True, False),
    ("Send Rahul an email saying I'll send the proposal tomorrow.", "email.send", True, True),
    ("Email Rahul and tell him I'll call him later.", "email.send", True, True),
    ("Send Rahul the email.", "email.send", True, True),
    ("Reply to Rahul and tell him I'll check it today.", "email.reply", True, True),
    ("Forward this email to Priya", "email.forward", True, True),
    ("Read this email and tell me what I need to do.", "email.analyze", True, False),
    ("Remind me tomorrow morning to call Rahul.", "reminder.create", False, False),
    ("What reminders do I have?", "reminder.list", False, False),
    ("Find Rahul's latest email about the proposal and schedule a meeting with him next week.", "multi", True, True),
    ("Fix the Salesforce bug mentioned in this email.", "multi", True, True),
]

NOT_CLAIMED = [
    "Add email validation to the signup form",
    "Write an email validation function in utils.py",
    "Update the email template for password reset",
    "Schedule nightly tests to run at 2am",
    "Refactor the calendar component in this project",
    "Fix the bug in the login page",
    "Deploy to staging",
    "Explain how git rebase works",
    "What is the capital of France?",
    "Summarize this article for me",
    "",
]


class Router(TestBase):
    def test_every_example_from_the_brief_is_routed(self):
        for text, name, needs_ai, approval in CLAIMED:
            with self.subTest(text):
                i = intent.classify(text)
                self.assertTrue(i.matched, "not claimed")
                self.assertEqual(i.intent, name)
                self.assertEqual(i.needs_ai, needs_ai, "needs_ai")
                self.assertEqual(i.requires_approval, approval, "requires_approval")
                self.assertEqual(i.auto_executable, not approval)

    def test_coding_requests_that_mention_mail_or_calendars_are_left_alone(self):
        for text in NOT_CLAIMED:
            with self.subTest(text):
                self.assertFalse(intent.classify(text).matched)

    def test_report_carries_what_the_brief_asks_for(self):
        d = intent.classify("Send Rahul an email saying hi").to_dict()
        for key in ("intent", "tool", "capabilities", "policy", "requires_approval", "needs_ai", "auto_executable"):
            self.assertIn(key, d)
        self.assertEqual((d["tool"], d["capabilities"]), ("email", ["EMAIL_READ", "EMAIL_SEND"]))

    def test_policy_changes_flow_into_the_report(self):
        cap.set_policy("CALENDAR_READ", cap.ASK)
        i = intent.classify("What's on my calendar tomorrow?")
        self.assertEqual((i.policy, i.requires_approval, i.auto_executable), ("ask", True, False))
        cap.set_policy("EMAIL_SEND", cap.ALLOW)
        self.assertFalse(intent.classify("Send Rahul an email saying hi").requires_approval)

    def test_follow_ups_are_only_claimed_inside_an_assistant_chat(self):
        self.assertFalse(intent.classify("move it to 4").matched)
        self.assertEqual(intent.classify("move it to 4", in_thread=True).intent, "calendar.modify")

    def test_multi_tool_and_handoff_report_all_tools_and_capabilities(self):
        i = intent.classify("Find Rahul's latest email about the proposal and schedule a meeting with him next week.")
        self.assertEqual(sorted(i.tools), ["calendar", "email"])
        self.assertTrue({"EMAIL_READ", "CALENDAR_CREATE", "CALENDAR_INVITE"} <= set(i.capabilities))
        h = intent.classify("Fix the Salesforce bug mentioned in this email.")
        self.assertEqual(h.tools, ["email", "agent"])
        self.assertIn("AGENT_CODING", h.capabilities)


class Router_Integration(TestBase):
    def test_router_sends_assistant_requests_to_the_assistant_lane_and_leaves_coding_alone(self):
        import router
        pa = router.route("What's on my calendar tomorrow?", None)
        self.assertEqual((pa["category"], pa["lane"], pa["model_id"]), ("assistant", "assistant", "octopus-tools"))
        self.assertIn("plain tool calls, no model", pa["reason"])
        coding = router.route("Fix the bug in the login page", "my-project")
        self.assertEqual((coding["category"], coding["lane"]), ("coding", "agent"))

    def test_a_manually_chosen_model_is_never_overridden(self):
        import router
        d = router.route("What's on my calendar tomorrow?", None, force_model="claude-code")
        self.assertEqual((d["category"], d["lane"]), ("manual", "agent"))


if __name__ == "__main__":
    unittest.main()
