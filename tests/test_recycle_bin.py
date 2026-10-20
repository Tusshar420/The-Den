"""Recycle bin: deleting a queue item hides it and keeps the worker off it; restore puts it back where it was."""

from datetime import datetime, timedelta, timezone

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

from fastapi import HTTPException

import api
import db


class RecycleBin(TestBase):
    def queue(self, prompt, thread_id=None):
        return db.add_task(self.project["id"], prompt, 0, thread_id, lane="agent")

    def test_deleted_task_is_skipped_hidden_and_listed_in_the_bin(self):
        a, b = self.queue("first"), self.queue("second")
        self.assertEqual(api.delete_task(a["id"]), {"deleted": [a["id"]]})
        self.assertEqual(db.get_next_queued_task("agent")["id"], b["id"])
        self.assertFalse(db.claim_task(a["id"]))
        self.assertNotIn(a["id"], [t["id"] for t in db.list_all_tasks()])
        self.assertNotIn(a["id"], [c["id"] for c in db.list_chats()["chats"]])
        self.assertEqual(db.queue_status()["counts"].get("queued"), 1)
        items = api.list_bin()["items"]
        self.assertEqual([i["id"] for i in items], [a["id"]])
        self.assertEqual(items[0]["deleted_from"], "queued")

    def test_restore_puts_it_back_in_its_old_spot(self):
        a, b = self.queue("first"), self.queue("second")
        api.delete_task(a["id"])
        out = api.restore_task(a["id"])
        self.assertEqual(out["task"]["status"], "queued")
        self.assertEqual(out["task"]["position"], 1)
        self.assertEqual(db.get_next_queued_task("agent")["id"], a["id"])
        self.assertEqual(api.list_bin()["items"], [])

    def test_restore_brings_back_a_cancelled_task_as_cancelled(self):
        a = self.queue("first")
        db.cancel_task(a["id"])
        api.delete_task(a["id"])
        self.assertEqual(api.restore_task(a["id"])["task"]["status"], "cancelled")

    def test_a_running_task_cant_be_deleted(self):
        a = self.queue("first")
        db.mark_running(a["id"])
        with self.assertRaises(HTTPException) as e:
            api.delete_task(a["id"])
        self.assertEqual(e.exception.status_code, 409)
        with self.assertRaises(HTTPException) as e:
            api.delete_task(9999)
        self.assertEqual(e.exception.status_code, 404)

    def test_deleting_a_chats_first_message_takes_its_queued_replies(self):
        root = self.queue("root")
        reply = self.queue("reply", thread_id=root["id"])
        self.assertEqual(sorted(api.delete_task(root["id"])["deleted"]), [root["id"], reply["id"]])
        items = api.list_bin()["items"]
        self.assertEqual([i["id"] for i in items], [root["id"]])
        self.assertEqual(items[0]["replies"], 1)
        self.assertEqual(sorted(api.restore_task(root["id"])["restored"]), [root["id"], reply["id"]])
        self.assertEqual(db.get_task(reply["id"])["status"], "queued")

    def test_a_chat_with_a_running_reply_keeps_its_first_message(self):
        root = self.queue("root")
        db.mark_done(root["id"], "ok")
        reply = self.queue("reply", thread_id=root["id"])
        db.mark_running(reply["id"])
        with self.assertRaises(HTTPException) as e:
            api.delete_task(root["id"])
        self.assertEqual(e.exception.status_code, 409)

    def test_a_finished_chat_can_be_deleted_and_restored_as_it_was(self):
        root = self.queue("root")
        db.mark_done(root["id"], "ok")
        reply = self.queue("reply", thread_id=root["id"])
        db.mark_error(reply["id"], "boom")
        self.assertEqual(sorted(api.delete_task(root["id"])["deleted"]), [root["id"], reply["id"]])
        self.assertNotIn(root["id"], [c["id"] for c in db.list_chats()["chats"]])
        api.restore_task(root["id"])
        self.assertEqual(db.get_task(root["id"])["status"], "done")
        self.assertIn(db.get_task(reply["id"])["status"], ("error", "needs_review"))

    def test_deleted_reply_drops_out_of_its_chat(self):
        root = self.queue("root")
        db.mark_done(root["id"], "ok")
        reply = self.queue("reply", thread_id=root["id"])
        api.delete_task(reply["id"])
        self.assertEqual([t["id"] for t in db.get_thread(root["id"])], [root["id"]])
        api.restore_task(reply["id"])
        self.assertEqual([t["id"] for t in db.get_thread(root["id"])], [root["id"], reply["id"]])

    def test_erase_one_and_empty_the_bin(self):
        a, b = self.queue("first"), self.queue("second")
        db.add_task_event(a["id"], "tool", "Read file")
        api.delete_task(a["id"])
        api.delete_task(b["id"])
        self.assertEqual(api.purge_bin_item(a["id"]), {"erased": 1})
        self.assertIsNone(db.get_task(a["id"]))
        self.assertEqual(db.list_task_events([a["id"]]).get(a["id"], []), [])
        with self.assertRaises(HTTPException):
            api.purge_bin_item(a["id"])
        self.assertEqual(api.empty_bin(), {"erased": 1})
        self.assertIsNone(db.get_task(b["id"]))

    def test_a_live_task_cant_be_erased_through_the_bin(self):
        a = self.queue("first")
        with self.assertRaises(HTTPException):
            api.purge_bin_item(a["id"])
        self.assertIsNotNone(db.get_task(a["id"]))

    def test_old_bin_items_expire(self):
        a, b = self.queue("old"), self.queue("new")
        api.delete_task(a["id"])
        api.delete_task(b["id"])
        long_ago = (datetime.now(timezone.utc) - timedelta(days=db.BIN_DAYS + 1)).isoformat()
        db.get_conn().execute("UPDATE tasks SET deleted_at = ? WHERE id = ?", (long_ago, a["id"]))
        self.assertEqual([i["id"] for i in api.list_bin()["items"]], [b["id"]])
        self.assertIsNone(db.get_task(a["id"]))
