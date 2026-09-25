"""Changing models between messages of a chat: the choice sticks, and the new model gets the conversation so far."""

from unittest import mock

from fastapi import HTTPException

import api
import db
import router
import worker
from tests.helpers import TestBase

API_MODEL = "gpt-6-sol"


def finish(task_id: int, answer: str) -> None:
    db.mark_running(task_id)
    db.mark_done(task_id, answer)


class ModelSwitch(TestBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(router, "availability", return_value=(True, "", None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pid = self.project["id"]
        self.root = db.add_task(self.pid, "what is a mutex?", lane="api", model_id=API_MODEL)
        finish(self.root["id"], "A lock.")

    def reply(self, prompt: str, model=None, **kw):
        return api.create_task(self.pid, api.TaskIn(prompt=prompt, thread_id=self.root["id"], model=model, **kw))

    def test_reply_without_a_model_stays_on_the_chats_model(self):
        r = self.reply("and a semaphore?")
        self.assertEqual((r["model_id"], r["lane"]), (API_MODEL, "api"))

    def test_switch_moves_the_reply_and_sticks(self):
        r1 = self.reply("now look at the code", model="claude-code")
        self.assertEqual((r1["model_id"], r1["lane"]), ("claude-code", "agent"))
        finish(r1["id"], "Looked.")
        r2 = self.reply("and the tests?")                        # no model named: stays on the last one
        self.assertEqual((r2["model_id"], r2["lane"]), ("claude-code", "agent"))
        finish(r2["id"], "Checked.")
        r3 = self.reply("thanks, summarise", model=API_MODEL)   # and back again
        self.assertEqual((r3["model_id"], r3["lane"]), (API_MODEL, "api"))
        self.assertIsNone(r3["permission_mode"])

    def test_cant_switch_while_the_last_message_is_unfinished(self):
        self.reply("one more")                                   # queued, not answered
        with self.assertRaises(HTTPException) as cm:
            self.reply("and switch", model="claude-code")
        self.assertEqual(cm.exception.status_code, 409)

    def test_unknown_or_unusable_models_are_refused(self):
        with self.assertRaises(HTTPException) as cm:
            self.reply("x", model="no-such-model")
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            self.reply("x", model="octopus-tools")               # the assistant isn't a chat model
        self.assertEqual(cm.exception.status_code, 400)

    def test_unavailable_model_is_a_conflict(self):
        with mock.patch.object(router, "availability", return_value=(False, "no API key", None)):
            with self.assertRaises(HTTPException) as cm:
                self.reply("x", model="claude-code")
        self.assertEqual(cm.exception.status_code, 409)

    def test_api_model_is_shown_answers_from_other_models(self):
        r1 = self.reply("look at the code", model="claude-code")
        finish(r1["id"], "Found the lock.")
        r2 = self.reply("explain it simply", model=API_MODEL)
        contents = [m["content"] for m in worker._api_messages(db.get_task(r2["id"]))]
        self.assertEqual(contents, ["what is a mutex?", "A lock.", "look at the code", "Found the lock.", "explain it simply"])

    def test_api_history_is_trimmed_to_the_models_window_oldest_first(self):
        r1 = self.reply("second question")
        finish(r1["id"], "x" * 300)
        r2 = self.reply("third question")
        # 140 tokens * 4 chars * 0.6 = 336 characters of room: the newest two turns need 329, all three 352
        msgs = worker._api_messages(db.get_task(r2["id"]), {"context_tokens": 140})
        contents = [m["content"] for m in msgs]
        self.assertEqual(contents[-1], "third question")
        self.assertNotIn("what is a mutex?", contents)
        self.assertIn("second question", contents)


class CatchUp(TestBase):
    def setUp(self):
        super().setUp()
        self.pid = self.project["id"]

    def test_chat_that_began_on_another_model_is_spelled_out(self):
        root = db.add_task(self.pid, "what is a mutex?", lane="api", model_id=API_MODEL)
        finish(root["id"], "A lock.")
        reply = db.add_task(self.pid, "now fix the race", thread_id=root["id"], lane="agent", model_id="claude-code")
        out = worker.catch_up_prompt(reply, root["id"], None)
        self.assertIn("User: what is a mutex?", out)
        self.assertIn("A lock.", out)
        self.assertIn(router.get_model(API_MODEL)["label"] + ": A lock.", out)
        self.assertTrue(out.endswith("The user's new message:\n"))

    def test_only_what_the_session_has_not_seen_is_added(self):
        root = db.add_task(self.pid, "look at the code", lane="agent", model_id="claude-code")
        finish(root["id"], "Looked.")
        db.set_task_session(root["id"], "sess-1")
        middle = db.add_task(self.pid, "explain it simply", thread_id=root["id"], lane="api", model_id=API_MODEL)
        finish(middle["id"], "It locks things.")
        reply = db.add_task(self.pid, "ok fix it", thread_id=root["id"], lane="agent", model_id="claude-code")
        out = worker.catch_up_prompt(reply, root["id"], "sess-1")
        self.assertIn("explain it simply", out)
        self.assertIn("It locks things.", out)
        self.assertNotIn("look at the code", out)

    def test_nothing_to_add_when_claude_code_has_been_the_only_model(self):
        root = db.add_task(self.pid, "look at the code", lane="agent", model_id="claude-code")
        finish(root["id"], "Looked.")
        db.set_task_session(root["id"], "sess-1")
        reply = db.add_task(self.pid, "and again", thread_id=root["id"], lane="agent", model_id="claude-code")
        self.assertEqual(worker.catch_up_prompt(reply, root["id"], "sess-1"), "")

    def test_first_message_of_a_chat_has_no_catch_up(self):
        root = db.add_task(self.pid, "hello", lane="agent", model_id="claude-code")
        self.assertEqual(worker.catch_up_prompt(root, root["id"], None), "")
