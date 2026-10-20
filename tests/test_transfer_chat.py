"""Moving a chat that isn't under way - finished, or still in line - to another project."""

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

from fastapi import HTTPException

import api
import db
import worker


class TransferChat(TestBase):
    def setUp(self):
        super().setUp()
        self.other = db.add_project("Other", self._tmp.name + "/other")

    def queue(self, prompt, thread_id=None):
        return db.add_task(self.project["id"], prompt, 0, thread_id, lane="agent")

    def move(self, task_id, project_id):
        return api.transfer_task(task_id, api.TransferIn(project_id=project_id))

    def test_a_finished_chat_moves_with_all_its_messages(self):
        root = self.queue("root")
        db.mark_done(root["id"], "did it")
        reply = self.queue("reply", thread_id=root["id"])
        db.mark_done(reply["id"], "did more")
        out = self.move(reply["id"], self.other["id"])
        self.assertEqual(sorted(out["moved"]), [root["id"], reply["id"]])
        for t in (root, reply):
            self.assertEqual(db.get_task(t["id"])["project_id"], self.other["id"])
            self.assertEqual(db.get_task(t["id"])["status"], "done")

    def test_the_next_reply_starts_fresh_with_the_chat_written_out(self):
        root = self.queue("fix the login bug")
        db.mark_done(root["id"], "fixed it")
        db.set_task_session(root["id"], "sess-old-folder")
        self.move(root["id"], self.other["id"])
        self.assertIsNone(db.get_thread_session_id(root["id"]))
        reply = db.add_task(self.other["id"], "now add a test", 0, root["id"], lane="agent")
        catch_up = worker.catch_up_prompt(db.get_task(reply["id"]), root["id"], None)
        self.assertIn("fix the login bug", catch_up)
        self.assertIn("fixed it", catch_up)

    def test_the_old_projects_environment_is_dropped(self):
        env = db.add_env(self.project["id"], "wt", self._tmp.name + "/wt")
        root = self.queue("root")
        db.get_conn().execute("UPDATE tasks SET env_id = ? WHERE id = ?", (env["id"], root["id"]))
        db.mark_done(root["id"], "ok")
        self.move(root["id"], self.other["id"])
        self.assertIsNone(db.get_task(root["id"])["env_id"])

    def test_a_reply_in_the_bin_goes_along(self):
        root = self.queue("root")
        db.mark_done(root["id"], "ok")
        reply = self.queue("reply", thread_id=root["id"])
        db.mark_done(reply["id"], "ok")
        api.delete_task(reply["id"])
        self.move(root["id"], self.other["id"])
        api.restore_task(reply["id"])
        self.assertEqual(db.get_task(reply["id"])["project_id"], self.other["id"])

    def test_a_chat_under_way_stays_put(self):
        root = self.queue("root")
        db.mark_done(root["id"], "ok")
        reply = self.queue("reply", thread_id=root["id"])
        db.mark_running(reply["id"])
        with self.assertRaises(HTTPException) as e:
            self.move(root["id"], self.other["id"])
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual(db.get_task(root["id"])["project_id"], self.project["id"])

    def test_bad_targets(self):
        root = self.queue("root")
        db.mark_done(root["id"], "ok")
        with self.assertRaises(HTTPException) as e:
            self.move(root["id"], 9999)
        self.assertEqual(e.exception.status_code, 404)
        with self.assertRaises(HTTPException) as e:
            self.move(root["id"], self.project["id"])
        self.assertEqual(e.exception.status_code, 400)
        with self.assertRaises(HTTPException) as e:
            self.move(9999, self.other["id"])
        self.assertEqual(e.exception.status_code, 404)
