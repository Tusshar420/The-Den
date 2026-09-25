"""The project page's Suggest button: one read-only review task, with the Den's own context in it."""

from fastapi import HTTPException

import api
import db
from tests.helpers import TestBase


class SuggestImprovements(TestBase):
    def setUp(self):
        super().setUp()
        self.app = db.add_project("shop", self._tmp.name)

    def test_queues_a_read_only_review(self):
        task = api.suggest_improvements(self.app["id"])
        self.assertEqual(task["project_id"], self.app["id"])
        self.assertEqual(task["title"], api.SUGGEST_TITLE)
        self.assertEqual(task["permission_mode"], "plan")
        self.assertIn("do not change any files", task["prompt"])

    def test_prompt_carries_backlog_and_recent_work(self):
        db.add_todo(self.app["id"], "Add rate limiting")
        db.add_task(self.app["id"], "Fix the checkout bug", 0, None, title="Checkout bug")
        prompt = api.suggest_improvements(self.app["id"])["prompt"]
        self.assertIn("Add rate limiting", prompt)
        self.assertIn("Checkout bug", prompt)

    def test_second_tap_reuses_the_waiting_review(self):
        first = api.suggest_improvements(self.app["id"])
        again = api.suggest_improvements(self.app["id"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(db.list_tasks(project_id=self.app["id"])), 1)

    def test_general_and_unknown_projects_are_refused(self):
        for pid, code in ((self.project["id"], 400), (9999, 404)):
            with self.assertRaises(HTTPException) as cm:
                api.suggest_improvements(pid)
            self.assertEqual(cm.exception.status_code, code)
