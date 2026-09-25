"""A backlog item can carry its own model, mode and priority, which become the task's when it is queued."""

from fastapi import HTTPException

import api
import db
from tests.helpers import TestBase


class TodoSettings(TestBase):
    def setUp(self):
        super().setUp()
        self.app = db.add_project("shop", self._tmp.name)
        self.todo = db.add_todo(self.app["id"], "Add rate limiting")

    def patch(self, **body):
        return api.update_todo(self.todo["id"], api.TodoUpdateIn(**body))

    def test_new_item_has_the_defaults(self):
        self.assertEqual((self.todo["model_id"], self.todo["permission_mode"], self.todo["priority"]), (None, None, 0))

    def test_settings_are_saved_and_can_be_cleared(self):
        t = self.patch(permission_mode="plan", priority=1, model="claude-code")
        self.assertEqual((t["model_id"], t["permission_mode"], t["priority"]), ("claude-code", "plan", 1))
        t = self.patch(permission_mode=None, priority=0)
        self.assertEqual((t["model_id"], t["permission_mode"], t["priority"]), ("claude-code", None, 0))

    def test_text_only_edit_leaves_settings_alone(self):
        self.patch(permission_mode="plan")
        self.assertEqual(self.patch(text="Add rate limiting v2")["permission_mode"], "plan")

    def test_bad_values_are_refused(self):
        for body in ({"permission_mode": "yolo"}, {"priority": 5}, {"model": "no-such-model"}):
            with self.assertRaises(HTTPException) as e:
                self.patch(**body)
            self.assertEqual(e.exception.status_code, 400)

    def test_queueing_carries_them_onto_the_task(self):
        self.patch(permission_mode="plan", priority=1, model="claude-code", checked=True)
        out = api.run_checked_todos(self.app["id"])
        task = db.get_task(out[0]["task_id"])
        self.assertEqual((task["permission_mode"], task["priority"], task["model_id"]), ("plan", 1, "claude-code"))

    def test_queueing_without_settings_is_unchanged(self):
        self.patch(checked=True)
        task = db.get_task(api.run_checked_todos(self.app["id"])[0]["task_id"])
        self.assertEqual((task["permission_mode"], task["priority"], task["model_id"]), (None, 0, "claude-code"))
