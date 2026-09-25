"""Per-task Claude Code permission mode: stored, validated, inherited by replies, passed to the CLI."""

import unittest

from fastapi import HTTPException

import api
import db
import worker
from tests.helpers import TestBase

BASE = ["--output-format", "stream-json", "--permission-mode", "acceptEdits", "--verbose"]


class WorkerFlags(unittest.TestCase):
    def test_mode_replaces_the_default(self):
        out = worker._with_permission_mode(BASE, "plan")
        self.assertEqual(out.count("--permission-mode"), 1)
        self.assertEqual(out[out.index("--permission-mode") + 1], "plan")
        self.assertIn("--verbose", out)

    def test_equals_form_is_replaced_too(self):
        out = worker._with_permission_mode(["--permission-mode=acceptEdits"], "auto")
        self.assertEqual(out, ["--permission-mode", "auto"])

    def test_no_mode_leaves_args_alone(self):
        self.assertEqual(worker._with_permission_mode(BASE, None), BASE)
        self.assertEqual(worker._with_permission_mode(BASE, "bypassPermissions"), BASE)


class TaskModes(TestBase):
    def test_new_task_stores_mode(self):
        t = api.create_task(self.project["id"], api.TaskIn(prompt="look around", permission_mode="plan"))
        self.assertEqual(db.get_task(t["id"])["permission_mode"], "plan")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            api.create_task(self.project["id"], api.TaskIn(prompt="x", permission_mode="bypassPermissions"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_reply_inherits_then_can_switch(self):
        root = api.create_task(self.project["id"], api.TaskIn(prompt="plan it", permission_mode="plan"))
        r1 = api.create_task(self.project["id"], api.TaskIn(prompt="ok", thread_id=root["id"]))
        self.assertEqual(r1["permission_mode"], "plan")
        r2 = api.create_task(self.project["id"], api.TaskIn(prompt="go", thread_id=root["id"], permission_mode="acceptEdits"))
        self.assertEqual(r2["permission_mode"], "acceptEdits")
        r3 = api.create_task(self.project["id"], api.TaskIn(prompt="more", thread_id=root["id"]))
        self.assertEqual(r3["permission_mode"], "acceptEdits")

    def test_queued_task_mode_can_be_edited(self):
        t = api.create_task(self.project["id"], api.TaskIn(prompt="x"))
        self.assertIsNone(t["permission_mode"])
        out = api.edit_task(t["id"], api.TaskEditIn(permission_mode="auto"))
        self.assertEqual(out["permission_mode"], "auto")


if __name__ == "__main__":
    unittest.main()
