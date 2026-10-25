"""modules/stop_reply: the Stop button - a running Claude Code task is killed, a running model
answer is cancelled on the worker's loop, and what's waiting in the chat never starts."""

import asyncio
import importlib.util
import json
import logging
import threading
from unittest import mock

import db
import worker
from tests.helpers import ROOT, TestBase

_spec = importlib.util.spec_from_file_location("stop_reply_module", ROOT / "modules/stop_reply/module.py")
stop_reply = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stop_reply)


class FakeCtx:
    def __init__(self):
        self.commands = {}
        self.log = logging.getLogger("test.stop_reply")

    def register_command(self, name, fn, description):
        self.commands[name] = fn


class StopReply(TestBase):
    def setUp(self):
        super().setUp()
        self.ctx = FakeCtx()
        stop_reply.Module().initialize(self.ctx)
        self.stop = lambda tid: self.ctx.commands["stop-reply"](json.dumps({"task_id": tid}))
        self.pid = self.project["id"]

    def status(self, tid):
        return db.get_task(tid)["status"]

    def test_running_claude_code_task_is_killed_and_waiting_reply_cancelled(self):
        root = db.add_task(self.pid, "Build the page")["id"]
        db.mark_running(root)
        waiting = db.add_task(self.pid, "Then add tests", thread_id=root)["id"]
        with mock.patch.object(worker, "stop_task", return_value=True) as kill:
            out = self.stop(waiting)
        kill.assert_called_once_with(root)
        self.assertEqual(out, {"stopped": [root], "cancelled": [waiting]})
        self.assertEqual(self.status(waiting), "cancelled")

    def test_claude_code_task_not_started_yet_says_try_again(self):
        root = db.add_task(self.pid, "Build the page")["id"]
        db.mark_running(root)
        with mock.patch.object(worker, "stop_task", return_value=False):
            self.assertIn("try Stop again", self.stop(root)["error"])

    def test_running_model_answer_is_cancelled_on_the_worker_loop(self):
        tid = db.add_task(self.pid, "What is a closure?", lane="api", model_id="gpt-test")["id"]
        db.mark_running(tid)
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        self.addCleanup(lambda: loop.call_soon_threadsafe(loop.stop))
        run = asyncio.run_coroutine_threadsafe(self._make_run(), loop).result(timeout=5)
        with mock.patch.object(worker, "_api_lane_wake", (loop, None)), \
             mock.patch.dict(worker._api_runs, {"gpt-test": run}):
            out = self.stop(tid)
        self.assertEqual(out, {"stopped": [tid], "cancelled": []})
        self.assertEqual(self.status(tid), "cancelled")
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)
        self.assertTrue(run.cancelled())

    @staticmethod
    async def _make_run():
        return asyncio.get_running_loop().create_task(asyncio.sleep(3600))

    def test_model_answer_that_already_finished_is_left_alone(self):
        tid = db.add_task(self.pid, "What is a closure?", lane="api", model_id="gpt-test")["id"]
        db.mark_running(tid)
        with mock.patch.dict(worker._api_runs, {}, clear=True):
            self.assertEqual(self.stop(tid), {"error": "It had already finished"})
        self.assertEqual(self.status(tid), "running")

    def test_assistant_request_cant_be_stopped_midway(self):
        tid = db.add_task(self.pid, "Email Sam", lane="assistant", model_id="assistant")["id"]
        db.mark_running(tid)
        self.assertIn("can't be stopped midway", self.stop(tid)["error"])

    def test_nothing_running(self):
        tid = db.add_task(self.pid, "Hello")["id"]
        db.mark_running(tid)
        db.mark_done(tid, "Hi")
        self.assertEqual(self.stop(tid), {"error": "Nothing is running in this chat"})
        self.assertEqual(self.stop(999999), {"error": "That message isn't there any more"})
