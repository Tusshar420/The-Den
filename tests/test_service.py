"""The request pipeline: submit -> plan -> (review) -> queue, plus Run / Cancel / follow-ups / instant commands."""

import os
import unittest
from unittest import mock

from tests.helpers import TestBase

import db
import router
from assistant import capabilities as cap
from octopus import service


class Pipeline(TestBase):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("KodeAura7", "/tmp/kodeaura7")
        env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)
        no_llm = mock.patch.object(router, "_llm_classify", lambda *a, **k: None)
        no_llm.start()
        self.addCleanup(no_llm.stop)

    # ---- straight through
    def test_a_normal_request_is_queued_and_the_decision_is_recorded(self):
        out = service.submit("Fix the login bug in KodeAura7", source="voice")
        self.assertEqual(out["status"], "queued")
        task = out["task"]
        self.assertEqual((task["agent_id"], task["lane"], task["model_id"], task["source"], task["risk"]),
                         ("coding", "agent", "claude-code", "voice", "medium"))
        self.assertEqual(task["tools"], ["filesystem", "git", "terminal"])
        row = db.get_routing(out["routing_id"])
        self.assertEqual((row["status"], row["task_id"], row["source"], row["decided_by"]), ("queued", task["id"], "voice", "auto"))
        self.assertEqual(out["decision"]["agent"], "Coding Agent")
        story = [e["text"] for e in db.list_task_events([task["id"]])[task["id"]]]
        self.assertEqual(story[0], "Received request by voice")
        self.assertIn("Classified as a coding task", story[1])
        self.assertIn("Selected KodeAura7 (named in the request)", story[2])
        self.assertTrue(any(s.startswith("Chose the Coding Agent on Claude Code") for s in story))
        self.assertIn("Tools: Filesystem, Git, Terminal", story)

    def test_writing_goes_to_the_api_lane_without_touching_claude(self):
        out = service.submit("Write me a 1500-word essay about AI in education.")
        self.assertEqual((out["task"]["lane"], out["task"]["agent_id"], out["task"]["tools"]), ("api", "writing", []))
        self.assertNotEqual(out["task"]["model_id"], "claude-code")

    def test_bad_input(self):
        for kwargs, status in (({"text": "  "}, 400), ({"text": "hi", "permission_mode": "yolo"}, 400),
                               ({"text": "hi", "thread_id": 9999}, 404), ({"text": "hi", "project_id": 9999}, 404),
                               ({"text": "hi", "review": "maybe"}, 400), ({"text": "hi", "model": "nope"}, 400)):
            with self.assertRaises(service.ServiceError) as ctx:
                service.submit(**kwargs)
            self.assertEqual(ctx.exception.status, status, kwargs)

    def test_no_usable_model_is_a_409_and_is_logged_as_failed(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}):
            with self.assertRaises(service.ServiceError) as ctx:
                service.submit("Generate a futuristic image of an AI assistant.")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("OPENAI_API_KEY", ctx.exception.detail)
        self.assertEqual(db.list_routing()[0]["status"], "failed")

    # ---- review
    def test_a_risky_request_waits_for_run_and_runs_exactly_once(self):
        out = service.submit("Deploy KodeAura7 to production")
        self.assertEqual((out["status"], out["task"]), ("proposed", None))
        rid = out["routing_id"]
        self.assertEqual(db.list_tasks(statuses=["queued"]), [])
        ran = service.run_plan(rid)
        self.assertEqual(ran["status"], "queued")
        self.assertEqual(db.get_routing(rid)["decided_by"], "user")
        self.assertIn("You approved the plan", [e["text"] for e in db.list_task_events([ran["task"]["id"]])[ran["task"]["id"]]])
        with self.assertRaises(service.ServiceError) as ctx:
            service.run_plan(rid)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(len(db.list_tasks(statuses=["queued"])), 1, "a double-tapped Run must not queue twice")

    def test_cancel_a_plan_then_run_is_refused(self):
        rid = service.submit("Deploy KodeAura7 to production")["routing_id"]
        self.assertEqual(service.cancel_plan(rid)["status"], "cancelled")
        with self.assertRaises(service.ServiceError) as ctx:
            service.run_plan(rid)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(db.list_tasks(statuses=["queued"]), [])

    def test_review_force_and_skip(self):
        self.assertEqual(service.submit("Fix the login bug in KodeAura7", review="force")["status"], "proposed")
        self.assertEqual(service.submit("Deploy KodeAura7 to production", review="skip")["status"], "queued")

    def test_run_can_override_project_model_and_agent(self):
        other = db.add_project("Other", "/tmp/other")
        rid = service.submit("Fix the login bug")["routing_id"]     # asks which project
        with self.assertRaises(service.ServiceError) as ctx:
            service.run_plan(rid)
        self.assertEqual((ctx.exception.status, ctx.exception.detail), (409, "Which project is this for?"))
        ran = service.run_plan(rid, project_id=other["id"])
        self.assertEqual(ran["task"]["project_id"], other["id"])

        rid = service.submit("Deploy KodeAura7 to production")["routing_id"]
        ran = service.run_plan(rid, agent="research")
        self.assertEqual((ran["task"]["agent_id"], ran["plan"]["overrides"]), ("research", {"agent": "research"}))

    def test_a_denied_capability_blocks_the_run(self):
        cap.set_policy("DEPLOY", cap.DENY)
        out = service.submit("Deploy KodeAura7 to production")
        self.assertEqual(out["status"], "proposed")
        self.assertEqual(out["plan"]["blocked"], ["Deploy or publish"])
        with self.assertRaises(service.ServiceError) as ctx:
            service.run_plan(out["routing_id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(service.ServiceError) as ctx:
            service.submit("Deploy KodeAura7 to production", review="skip")
        self.assertEqual(ctx.exception.status, 403)

    # ---- answering a plan that is waiting (voice: "yes, go ahead")
    def test_yes_runs_the_waiting_plan_and_no_cancels_it(self):
        rid = service.submit("Deploy KodeAura7 to production")["routing_id"]
        out = service.submit("Yes, go ahead")
        self.assertEqual((out["status"], out["routing_id"]), ("queued", rid))
        rid = service.submit("Deploy KodeAura7 to production")["routing_id"]
        out = service.submit("no")
        self.assertEqual((out["status"], db.get_routing(rid)["status"]), ("cancelled", "cancelled"))
        self.assertIn("nothing was run", out["result"]["text"])

    def test_naming_the_project_answers_which_project(self):
        service.submit("Fix the login bug")
        out = service.submit("KodeAura7")
        self.assertEqual((out["status"], out["task"]["project_id"]), ("queued", self.proj["id"]))
        self.assertEqual(out["task"]["prompt"], "Fix the login bug")

    def test_a_bare_yes_does_not_pick_a_project(self):
        service.submit("Fix the login bug")            # asks which project
        self.assertIsNone(service._answer_pending("yes", "api", 0, None))

    def test_yes_and_no_phrasings(self):
        for t in ("Yes, go ahead", "yeah do it", "ok", "Sure!", "go for it", "yes please", "run it", "proceed"):
            self.assertTrue(service._is_yes(t), t)
        for t in ("Fix the second one", "yes but change the project to Other", "no", "cancel that", "run the tests afterward please now ok"):
            self.assertFalse(service._is_yes(t), t)
        for t in ("no", "Nope.", "don't", "no thanks", "never mind", "not now"):
            self.assertTrue(service._is_no(t), t)
        for t in ("cancel that", "note that", "nothing works", "do the thing"):
            self.assertFalse(service._is_no(t), t)

    # ---- instant commands
    def test_control_commands_run_immediately_and_are_recorded(self):
        out = service.submit("Octopus, what's happening?", source="voice")
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["result"]["text"], "Nothing is running right now. The queue is empty.")
        task = out["task"]
        self.assertEqual((task["lane"], task["status"], task["result_summary"], task["agent_id"]),
                         ("control", "done", out["result"]["text"], "operator"))
        self.assertEqual(db.get_routing(out["routing_id"])["status"], "done")
        self.assertEqual(db.list_tasks(statuses=["queued", "running"]), [])

    def test_cancel_that_cancels_the_waiting_plan(self):
        rid = service.submit("Deploy KodeAura7 to production")["routing_id"]
        out = service.submit("cancel that")
        self.assertEqual(db.get_routing(rid)["status"], "cancelled")
        self.assertIn("Cancelled the plan", out["result"]["text"])

    def test_add_to_backlog_by_voice(self):
        out = service.submit("Add fix the footer link to the backlog of KodeAura7")
        self.assertEqual(out["status"], "done")
        self.assertEqual([t["text"] for t in db.list_todos(self.proj["id"])], ["fix the footer link"])

    # ---- conversation
    def test_a_follow_up_joins_the_chat_but_a_new_topic_does_not(self):
        root = service.submit("Check KodeAura7 for problems")["task"]
        db.mark_running(root["id"])
        db.mark_done(root["id"], "I found three issues.")
        follow = service.submit("Fix the second one", continue_recent=True, source="voice")["task"]
        self.assertEqual((follow["thread_id"], follow["agent_id"], follow["project_id"], follow["lane"]),
                         (root["id"], "coding", self.proj["id"], "agent"))
        again = service.submit("Run the tests afterward", continue_recent=True)["task"]
        self.assertEqual(again["thread_id"], root["id"])
        fresh = service.submit("Write me an essay about tea", continue_recent=True)["task"]
        self.assertIsNone(fresh["thread_id"])
        self.assertEqual(fresh["agent_id"], "writing")

    def test_a_reply_inherits_the_chats_permission_mode_unless_told_otherwise(self):
        root = service.submit("Fix the login bug in KodeAura7", permission_mode="plan")["task"]
        reply = service.submit("and the footer", thread_id=root["id"])["task"]
        self.assertEqual(reply["permission_mode"], "plan")
        self.assertEqual(service.submit("and the header", thread_id=root["id"], permission_mode="acceptEdits")["task"]["permission_mode"],
                         "acceptEdits")

    def test_is_followup(self):
        for t in ("Fix the second one", "Run the tests afterward.", "and then push it", "do that again", "what about the last one"):
            self.assertTrue(service.is_followup(t), t)
        for t in ("Check KodeAura7", "Write me an essay about AI", "Generate an image of a cat"):
            self.assertFalse(service.is_followup(t), t)


if __name__ == "__main__":
    unittest.main()
