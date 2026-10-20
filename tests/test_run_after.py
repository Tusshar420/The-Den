"""Run after: a queued task can wait for another one to finish successfully."""

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import api
import db


class RunAfter(TestBase):
    def queue(self, prompt):
        return db.add_task(self.project["id"], prompt, 0, None, lane="agent")

    def test_waits_until_the_other_task_succeeds(self):
        a, b = self.queue("first"), self.queue("second")
        api.move_task_to_top(b["id"])
        api.set_task_run_after(b["id"], api.RunAfterIn(after_id=a["id"]))
        self.assertEqual(db.get_next_queued_task("agent")["id"], a["id"])   # b is first in line but waits
        db.mark_running(a["id"])
        self.assertIsNone(db.get_next_queued_task("agent"))
        db.mark_done(a["id"], "ok")
        self.assertEqual(db.get_next_queued_task("agent")["id"], b["id"])

    def test_a_failed_dependency_keeps_it_waiting(self):
        a, b = self.queue("first"), self.queue("second")
        api.set_task_run_after(b["id"], api.RunAfterIn(after_id=a["id"]))
        db.mark_running(a["id"])
        db.mark_error(a["id"], "boom")
        self.assertIsNone(db.get_next_queued_task("agent"))
        api.set_task_run_after(b["id"], api.RunAfterIn(after_id=None))      # cleared: back to queue order
        self.assertEqual(db.get_next_queued_task("agent")["id"], b["id"])

    def test_refuses_loops_and_unknown_tasks(self):
        a, b, c = self.queue("a"), self.queue("b"), self.queue("c")
        api.set_task_run_after(b["id"], api.RunAfterIn(after_id=a["id"]))
        api.set_task_run_after(c["id"], api.RunAfterIn(after_id=b["id"]))
        for task_id, after in ((a["id"], c["id"]), (a["id"], a["id"]), (a["id"], 99999)):
            with self.assertRaises(HTTPException) as e:
                api.set_task_run_after(task_id, api.RunAfterIn(after_id=after))
            self.assertEqual(e.exception.status_code, 400)

    def test_only_waiting_tasks(self):
        a, b = self.queue("first"), self.queue("second")
        db.mark_running(a["id"])
        with self.assertRaises(HTTPException) as e:
            api.set_task_run_after(a["id"], api.RunAfterIn(after_id=b["id"]))
        self.assertEqual(e.exception.status_code, 409)
