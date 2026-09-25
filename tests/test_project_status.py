"""Project status (not started / in progress / need info / ...) and the "mark in progress when a task starts" automation."""

from fastapi import HTTPException

import api
import db
from tests.helpers import TestBase


class ProjectStatus(TestBase):
    def setUp(self):
        super().setUp()
        self.app = db.add_project("shop", self._tmp.name)

    def status(self):
        return db.get_project(self.app["id"])["status"]

    def queue(self, prompt="Fix it"):
        return db.add_task(self.app["id"], prompt, 0, None)

    def test_new_projects_start_not_started(self):
        self.assertEqual(self.status(), "not_started")

    def test_set_status_through_the_api(self):
        for s in db.PROJECT_STATUSES:
            self.assertEqual(api.update_project(self.app["id"], api.ProjectActiveIn(status=s))["status"], s)

    def test_unknown_status_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            api.update_project(self.app["id"], api.ProjectActiveIn(status="finished-ish"))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.status(), "not_started")

    def test_starting_a_task_marks_the_project_in_progress(self):
        task = self.queue()
        self.assertEqual(self.status(), "not_started")  # queued is not started
        self.assertTrue(db.claim_task(task["id"]))
        self.assertEqual(self.status(), "in_progress")

    def test_mark_running_does_it_too(self):
        db.set_project_status(self.app["id"], "completed")
        db.mark_running(self.queue()["id"])
        self.assertEqual(self.status(), "in_progress")

    def test_only_the_project_that_ran_changes(self):
        other = db.add_project("blog", self._tmp.name)
        db.claim_task(self.queue()["id"])
        self.assertEqual(db.get_project(other["id"])["status"], "not_started")

    def test_switched_off_leaves_status_alone(self):
        db.update_project_settings(self.app["id"], auto_in_progress=False)
        db.set_project_status(self.app["id"], "need_info")
        db.claim_task(self.queue()["id"])
        self.assertEqual(self.status(), "need_info")

    def test_on_by_default_without_a_settings_row(self):
        self.assertTrue(db.get_project_settings(self.app["id"])["auto_in_progress"])

    def test_a_resume_after_rate_limit_does_not_override_a_manual_status(self):
        task = self.queue()
        db.claim_task(task["id"])
        db.set_project_status(self.app["id"], "on_hold")
        with db._lock:
            conn = db.get_conn()
            conn.execute("UPDATE tasks SET status = 'rate_limited' WHERE id = ?", (task["id"],))
            conn.commit()
        self.assertTrue(db.claim_task(task["id"]))
        self.assertEqual(self.status(), "on_hold")

    def test_a_task_that_cannot_start_changes_nothing(self):
        task = self.queue()
        db.claim_task(task["id"])
        db.set_project_status(self.app["id"], "need_info")
        self.assertFalse(db.claim_task(task["id"]))  # already running
        self.assertEqual(self.status(), "need_info")
