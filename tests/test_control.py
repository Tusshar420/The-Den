"""Instant commands about Octopus itself: recognised by rules, run without a model."""

import unittest
from unittest import mock

from tests.helpers import TestBase

import db
from octopus import control


class Matching(unittest.TestCase):
    def action(self, text):
        found = control.match(text)
        return found and found["action"]

    def test_status_phrasings(self):
        for t in ("what's happening?", "Octopus, what's happening?", "What are you currently working on?",
                  "what is the status", "status", "how's it going", "what's the queue", "show me the queue"):
            self.assertEqual(self.action(t), "status", t)

    def test_stop_and_continue_phrasings(self):
        for t in ("Octopus, stop the current task.", "stop", "cancel that", "cancel it", "please stop the task", "abort", "never mind"):
            self.assertEqual(self.action(t), "stop", t)
        for t in ("continue the previous task", "Octopus, continue the previous task", "resume", "keep going", "continue"):
            self.assertEqual(self.action(t), "continue", t)

    def test_projects_and_backlog(self):
        self.assertEqual(self.action("list my projects"), "projects")
        self.assertEqual(self.action("what projects do I have"), "projects")
        add = control.match("Add Fix the footer link to the backlog of KodeAura7")
        self.assertEqual((add["action"], add["item"], add["project_name"]), ("backlog_add", "Fix the footer link", "kodeaura7"))
        self.assertEqual(control.match("add dark mode to the todo list")["project_name"], None)
        self.assertEqual(self.action("what's in the backlog for KodeAura7"), "backlog_show")

    def test_real_requests_are_not_mistaken_for_commands(self):
        for t in ("cancel my meeting tomorrow", "stop using var in the parser", "cancel the subscription flow in the app",
                  "continue the essay about tea in a friendlier tone", "what's happening in the news today",
                  "add a dark mode toggle to the settings page", "fix the login bug", "Write me an essay about AI",
                  "run the tests", "x" * 300, "stop\nand think"):
            self.assertIsNone(control.match(t), t)


class Running(TestBase):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("KodeAura7", "/tmp/kodeaura7")

    def test_status_when_idle_and_when_busy(self):
        self.assertEqual(control.run(control.match("what's happening"))["text"], "Nothing is running right now. The queue is empty.")
        t = db.add_task(self.proj["id"], "Fix the auth bug", 0, None)
        db.mark_running(t["id"])
        db.add_task(self.proj["id"], "Later", 0, None)
        out = control.run(control.match("what are you working on"))
        self.assertIn("Working on", out["text"])
        self.assertIn("in KodeAura7", out["text"])
        self.assertIn("1 waiting in the queue", out["text"])
        self.assertEqual(out["running"], [t["id"]])

    def test_status_counts_approvals(self):
        t = db.add_task(self.proj["id"], "x", 0, None)
        db.mark_running(t["id"])
        db.create_approval(t["id"], "Bash", '{"command": "ls"}')
        self.assertIn("1 approval needs your attention", control.run(control.match("status"))["text"])

    def test_stop_prefers_a_plan_waiting_on_you(self):
        from octopus import plan
        p = plan.build_plan("Deploy KodeAura7 to production")
        r = db.add_routing(p["request"], p)
        out = control.run(control.match("cancel that"))
        self.assertEqual(out["acted"], "plan")
        self.assertEqual(db.get_routing(r["id"])["status"], "cancelled")

    def test_stop_stops_the_running_agent_task(self):
        t = db.add_task(self.proj["id"], "Long job", 0, None)
        db.mark_running(t["id"])
        with mock.patch("worker.stop_task", return_value=True) as stopper:
            out = control.run(control.match("stop the current task"))
        stopper.assert_called_once_with(t["id"])
        self.assertEqual((out["ok"], out["acted"]), (True, "task"))

    def test_stop_with_nothing_to_stop_says_so(self):
        out = control.run(control.match("stop"))
        self.assertFalse(out["ok"])
        self.assertIn("nothing running", out["text"])

    def test_stop_drops_a_queued_task_that_has_not_started(self):
        t = db.add_task(self.proj["id"], "Not yet", 0, None)
        out = control.run(control.match("cancel it"))
        self.assertEqual(db.get_task(t["id"])["status"], "cancelled")
        self.assertIn("hadn't started", out["text"])

    def test_continue_replies_in_the_stopped_tasks_own_chat(self):
        t = db.add_task(self.proj["id"], "Big refactor", 0, None, permission_mode="acceptEdits")
        db.mark_running(t["id"])
        db.mark_stopped(t["id"])
        out = control.run(control.match("continue the previous task"))
        new = db.get_task(out["task_ids"][0])
        self.assertEqual((new["thread_id"], new["status"], new["project_id"]), (t["id"], "queued", self.proj["id"]))
        self.assertEqual(new["permission_mode"], "acceptEdits")

    def test_continue_with_nothing_stopped(self):
        self.assertFalse(control.run(control.match("continue"))["ok"])

    def test_projects_and_backlog(self):
        db.add_todo(self.proj["id"], "Fix footer")
        self.assertIn("KodeAura7 - 1 to-do", control.run(control.match("list my projects"))["text"])
        out = control.run(control.match("add Dark mode to the backlog of kodeaura7"))
        self.assertEqual(out["acted"], "todo")
        self.assertEqual([t["text"] for t in db.list_todos(self.proj["id"])], ["Fix footer", "Dark mode"])
        shown = control.run(control.match("what's in the backlog for KodeAura7"))
        self.assertIn("• Dark mode", shown["text"])

    def test_backlog_add_with_one_project_needs_no_name_and_with_none_asks(self):
        out = control.run(control.match("add Write docs to the backlog"))
        self.assertEqual(out["acted"], "todo")
        db.add_project("Other", "/tmp/other")
        out = control.run(control.match("add More docs to the backlog"))
        self.assertFalse(out["ok"])
        self.assertIn("Which project", out["text"])


if __name__ == "__main__":
    unittest.main()
