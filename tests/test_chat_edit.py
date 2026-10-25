"""modules/chat_edit: rewinding a chat before an edited message - later messages to the bin, the
Claude Code session dropped so the next run starts fresh with the chat so far written out."""

import importlib.util
import json
import logging

import db
import worker
from tests.helpers import ROOT, TestBase

_spec = importlib.util.spec_from_file_location("chat_edit_module", ROOT / "modules/chat_edit/module.py")
chat_edit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat_edit)


class FakeCtx:
    def __init__(self):
        self.commands = {}
        self.log = logging.getLogger("test.chat_edit")

    def register_command(self, name, fn, description):
        self.commands[name] = fn


class ChatEdit(TestBase):
    def setUp(self):
        super().setUp()
        self.ctx = FakeCtx()
        chat_edit.Module().initialize(self.ctx)
        self.rewind = lambda tid: self.ctx.commands["chat-rewind"](json.dumps({"task_id": tid}))
        pid = self.project["id"]
        self.root = self.finished(db.add_task(pid, "Make the header blue"), "Made it blue")
        self.second = self.finished(db.add_task(pid, "Now make it red", thread_id=self.root), "Made it red")
        self.third = self.finished(db.add_task(pid, "Add a logo", thread_id=self.root), "Added a logo")

    def finished(self, task, result):
        db.mark_running(task["id"])
        db.set_task_session(task["id"], "sess-1")
        db.mark_done(task["id"], result)
        return task["id"]

    def test_editing_a_reply_bins_it_and_later_ones_and_drops_the_session(self):
        out = self.rewind(self.second)
        self.assertEqual(out, {"root": self.root, "whole_chat": False, "binned": [self.third, self.second]})
        self.assertEqual([t["id"] for t in db.get_thread(self.root)], [self.root])
        self.assertIsNone(db.get_thread_session_id(self.root))
        # The edited text, sent again, starts fresh and is told only the chat before it.
        reply = db.add_task(self.project["id"], "Now make it green", thread_id=self.root)
        catch_up = worker.catch_up_prompt(reply, self.root, db.get_thread_session_id(self.root))
        self.assertIn("Make the header blue", catch_up)
        self.assertNotIn("make it red", catch_up)
        self.assertNotIn("logo", catch_up)

    def test_binned_messages_can_be_restored(self):
        self.rewind(self.third)
        self.assertEqual(db.restore_task(self.third), [self.third])
        self.assertEqual(len(db.get_thread(self.root)), 3)

    def test_editing_the_first_message_bins_the_whole_chat(self):
        out = self.rewind(self.root)
        self.assertTrue(out["whole_chat"])
        self.assertEqual(sorted(out["binned"]), [self.root, self.second, self.third])
        self.assertEqual(db.get_thread(self.root), [])

    def test_refused_while_a_message_is_under_way(self):
        nxt = db.add_task(self.project["id"], "And a footer", thread_id=self.root)
        db.mark_running(nxt["id"])
        self.assertIn("under way", self.rewind(self.second)["error"])
        self.assertEqual(len(db.get_thread(self.root)), 4)

    def test_bad_input(self):
        self.assertIn("task_id", self.ctx.commands["chat-rewind"]("{}")["error"])
        self.assertIn("isn't there", self.rewind(99999)["error"])
