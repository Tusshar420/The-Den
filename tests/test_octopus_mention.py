""""@octopus ..." in a chat: Octopus takes the message as a request of its own, and the chat's model never sees it."""

import os
from unittest import mock

from fastapi import HTTPException

import api
import db
import router
from octopus import service
from tests.helpers import TestBase

API_MODEL = "gpt-6-sol"


class Mention(TestBase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
                  mock.patch.object(router, "_llm_classify", lambda *a, **k: None),
                  mock.patch.object(router, "availability", return_value=(True, "", None))):
            p.start()
            self.addCleanup(p.stop)
        self.proj = db.add_project("KodeAura7", "/tmp/kodeaura7")
        self.root = db.add_task(self.proj["id"], "what is a mutex?", lane="api", model_id=API_MODEL)
        db.mark_running(self.root["id"])
        db.mark_done(self.root["id"], "A lock.")

    def reply(self, prompt: str, **kw):
        return api.create_task(self.proj["id"], api.TaskIn(prompt=prompt, thread_id=self.root["id"], **kw))

    def chat(self):
        return db.get_thread(self.root["id"])

    def test_spotting_and_stripping_the_mention(self):
        for text, left in (("@octopus, run the tests", "run the tests"), ("can @Octopus check it", "can check it"),
                           ("@OCTOPUS: deploy\nthen report", "deploy\nthen report")):
            self.assertTrue(service.mentions_octopus(text), text)
            self.assertEqual(service.strip_mention(text), left)
        for text in ("mail me@octopus.com", "the @octopuses", "octopus, run the tests", ""):
            self.assertFalse(service.mentions_octopus(text), text)

    def test_octopus_takes_the_work_and_the_chats_model_never_gets_it(self):
        r = self.reply("@octopus fix the login bug")
        self.assertEqual((r["lane"], r["status"], r["model_id"], r["thread_id"]), ("control", "done", None, self.root["id"]))
        out = r["octopus"]
        self.assertEqual(out["status"], "queued")
        task = out["task"]
        # Octopus's own request: its pick of agent and model, in this chat's project, not a reply in this chat.
        self.assertEqual((task["prompt"], task["thread_id"], task["project_id"], task["agent_id"], task["model_id"]),
                         ("fix the login bug", None, self.proj["id"], "coding", "claude-code"))
        self.assertTrue(r["result_summary"].startswith(f"{service.HANDOFF_QUEUED}{task['id']} · "))
        self.assertEqual(r["routing_id"], out["routing_id"])
        # Nothing in the chat is waiting for the chat's model, and the chat stays on it.
        self.assertEqual([t["status"] for t in self.chat()], ["done", "done"])
        self.assertEqual(db.get_thread_model(self.root["id"]), API_MODEL)
        nxt = self.reply("and a semaphore?")
        self.assertEqual((nxt["lane"], nxt["model_id"]), ("api", API_MODEL))

    def test_the_request_can_name_another_project(self):
        other = db.add_project("Lumen", "/tmp/lumen")
        out = self.reply("@octopus fix the login bug in Lumen")["octopus"]
        self.assertEqual(out["task"]["project_id"], other["id"])

    def test_a_risky_request_waits_for_your_ok(self):
        r = self.reply("@octopus deploy KodeAura7 to production")
        self.assertEqual((r["octopus"]["status"], r["octopus"]["task"]), ("proposed", None))
        self.assertTrue(r["result_summary"].startswith(service.HANDOFF_PLAN))
        self.assertEqual(db.get_routing(r["routing_id"])["status"], "proposed")

    def test_an_instant_command_is_answered_in_the_chat(self):
        r = self.reply("@octopus what's happening?")
        self.assertEqual(r["octopus"]["status"], "done")
        self.assertEqual(r["result_summary"], r["octopus"]["result"]["text"])

    def test_a_bare_mention_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            self.reply("@octopus  ")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(len(self.chat()), 1)

    def test_the_note_doesnt_hide_an_unfinished_message_from_the_model_switch_check(self):
        self.reply("one more")                                   # queued for the chat's model
        self.reply("@octopus fix the login bug")
        with self.assertRaises(HTTPException) as cm:
            self.reply("and switch", model="claude-code")
        self.assertEqual(cm.exception.status_code, 409)

    def test_the_octopus_endpoint_hands_off_too(self):
        out = service.submit("@octopus fix the login bug", thread_id=self.root["id"], source="voice")
        self.assertEqual((out["status"], out["task"]["thread_id"], out["note"]["lane"]), ("queued", None, "control"))
        self.assertEqual(db.get_thread_model(self.root["id"]), API_MODEL)
