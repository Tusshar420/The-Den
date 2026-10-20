"""Edit and retry: a failed or cancelled task takes a new prompt and is requeued in one step."""

from fastapi import HTTPException

import api
import db
from tests.helpers import TestBase


class EditAndRetry(TestBase):
    def failed(self, prompt="first try"):
        t = db.add_task(self.project["id"], prompt, 0, None, lane="agent")
        db.mark_running(t["id"])
        db.mark_error(t["id"], "boom")
        return t

    def status(self, t):
        return db.get_task(t["id"])["status"]

    def test_new_prompt_is_saved_and_task_is_queued(self):
        t = self.failed()
        out = api.requeue_task(t["id"], api.RequeueIn(prompt="  second try  "))
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["prompt"], "second try")
        self.assertIsNone(out["error_message"])

    def test_empty_prompt_is_refused_and_task_untouched(self):
        t = self.failed()
        before = self.status(t)
        with self.assertRaises(HTTPException) as cm:
            api.requeue_task(t["id"], api.RequeueIn(prompt="   "))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.status(t), before)

    def test_plain_retry_keeps_the_prompt(self):
        t = self.failed()
        self.assertEqual(api.requeue_task(t["id"])["prompt"], "first try")

    def test_a_queued_task_cannot_be_edit_retried(self):
        t = db.add_task(self.project["id"], "waiting", 0, None, lane="agent")
        with self.assertRaises(HTTPException) as cm:
            api.requeue_task(t["id"], api.RequeueIn(prompt="x"))
        self.assertEqual(cm.exception.status_code, 409)
