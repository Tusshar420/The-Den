"""Pause / play: a paused queued task is skipped but keeps its place; a running one is frozen."""

import asyncio
import os
import signal
import time

from fastapi import HTTPException

import api
import db
import worker
from tests.helpers import TestBase


class PauseQueued(TestBase):
    def queue(self, prompt):
        return db.add_task(self.project["id"], prompt, 0, None, lane="agent")

    def test_paused_task_is_skipped_and_keeps_its_place(self):
        a, b = self.queue("first"), self.queue("second")
        api.pause_task(a["id"])
        self.assertEqual(db.get_next_queued_task("agent")["id"], b["id"])
        played = api.play_task(a["id"])
        self.assertEqual(played["paused"], 0)
        self.assertEqual(played["position"], 1)
        self.assertEqual(db.get_next_queued_task("agent")["id"], a["id"])

    def test_paused_task_is_not_claimed_or_run_now(self):
        a = self.queue("first")
        api.pause_task(a["id"])
        self.assertFalse(db.claim_task(a["id"]))
        self.assertEqual(db.run_now_tasks(), [])

    def test_proceed_now_plays_it(self):
        a = self.queue("first")
        api.pause_task(a["id"])
        api.run_task_now(a["id"])
        self.assertEqual(db.get_task(a["id"])["paused"], 0)

    def test_cancel_and_requeue_clear_the_hold(self):
        a = self.queue("first")
        api.pause_task(a["id"])
        db.cancel_task(a["id"])
        self.assertEqual(db.get_task(a["id"])["paused"], 0)

    def test_finished_task_cannot_be_paused(self):
        a = self.queue("first")
        db.mark_done(a["id"], "ok")
        for fn in (api.pause_task, api.play_task):
            with self.assertRaises(HTTPException) as cm:
                fn(a["id"])
            self.assertEqual(cm.exception.status_code, 409)

    def test_running_task_without_a_process_cannot_be_paused(self):
        a = self.queue("first")
        db.mark_running(a["id"])
        with self.assertRaises(HTTPException) as cm:
            api.pause_task(a["id"])
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(db.get_task(a["id"])["paused"], 0)


class FreezeRunning(TestBase):
    def test_process_is_frozen_then_continues(self):
        async def go():
            proc = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
            t = db.add_task(self.project["id"], "x", 0, None, lane="agent")
            db.mark_running(t["id"])
            worker._running[t["id"]] = proc
            try:
                out = api.pause_task(t["id"])
                self.assertEqual(out["paused"], 1)
                self.assertEqual(out["status"], "running")
                self.assertIn(t["id"], worker._frozen)
                # 'T' in ps state = stopped
                time.sleep(0.2)
                state = os.popen(f"ps -o stat= -p {proc.pid}").read().strip()
                self.assertTrue(state.startswith("T"), state)
                self.assertEqual(api.play_task(t["id"])["paused"], 0)
                time.sleep(0.2)
                state = os.popen(f"ps -o stat= -p {proc.pid}").read().strip()
                self.assertFalse(state.startswith("T"), state)
            finally:
                worker._running.pop(t["id"], None)
                worker._frozen.discard(t["id"])
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()

        asyncio.run(go())
