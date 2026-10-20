"""Queue items are either tasks (run to completion) or messages (a question whose answer is read)."""

from fastapi import HTTPException

import api
import db
from tests.helpers import TestBase


class ItemKinds(TestBase):
    def msg(self, prompt="what is a monad?"):
        return db.add_task(self.project["id"], prompt, lane="api", model_id="gpt-6-sol")

    def test_default_is_task_and_api_lane_infers_message(self):
        self.assertEqual(db.add_task(self.project["id"], "fix the bug")["kind"], "task")
        self.assertEqual(self.make_task("check my mail")["kind"], "task")       # assistant lane
        self.assertEqual(self.msg()["kind"], "message")                          # api lane, same rule as the migration

    def test_explicit_kind_wins_and_is_validated(self):
        self.assertEqual(db.add_task(self.project["id"], "q", lane="api", model_id="gpt-6-sol", kind="task")["kind"], "task")
        with self.assertRaises(ValueError):
            db.add_task(self.project["id"], "x", kind="chore")

    def test_a_message_on_claude_code_is_read_only(self):
        m = db.add_task(self.project["id"], "why is this failing?", kind="message", permission_mode="acceptEdits")
        self.assertEqual((m["lane"], m["permission_mode"]), ("agent", "plan"))
        self.assertEqual(db.add_task(self.project["id"], "fix it", permission_mode="acceptEdits")["permission_mode"], "acceptEdits")

    def test_replies_keep_their_chats_kind(self):
        root = self.msg()
        self.assertEqual(db.add_task(self.project["id"], "and why?", thread_id=root["id"], kind="task")["kind"], "message")
        task = db.add_task(self.project["id"], "fix it")
        self.assertEqual(db.add_task(self.project["id"], "also this", thread_id=task["id"], kind="message")["kind"], "task")

    def test_create_endpoint_message_routes_to_a_model(self):
        out = api.create_task(self.project["id"], api.TaskIn(prompt="explain CORS", kind="message", model="gpt-6-sol"))
        self.assertEqual((out["kind"], out["lane"], out["model_id"], out["routed_by"]), ("message", "api", "gpt-6-sol", "manual"))
        plain = api.create_task(self.project["id"], api.TaskIn(prompt="fix the login bug"))
        self.assertEqual((plain["kind"], plain["lane"], plain["model_id"]), ("task", "agent", "claude-code"))

    def test_create_endpoint_rejects_bad_kind_and_image_models(self):
        for body in (api.TaskIn(prompt="x", kind="chore"), api.TaskIn(prompt="x", kind="message", model="gpt-image-1")):
            with self.assertRaises(HTTPException) as cm:
                api.create_task(self.project["id"], body)
            self.assertEqual(cm.exception.status_code, 400)

    def test_unread_until_read(self):
        m, t = self.msg(), db.add_task(self.project["id"], "fix it")
        self.assertFalse(db.get_task(m["id"])["unread"])            # not answered yet
        db.mark_done(m["id"], "A monad is...")
        db.mark_done(t["id"], "Fixed")
        self.assertTrue(db.get_task(m["id"])["unread"])
        self.assertFalse(db.get_task(t["id"])["unread"])            # tasks have no read state
        self.assertEqual(db.queue_status()["unread_messages"], 1)
        self.assertEqual([c["id"] for c in db.list_chats(unread=True)["chats"]], [m["id"]])
        api.mark_message_read(m["id"])
        self.assertFalse(db.get_task(m["id"])["unread"])
        self.assertEqual(db.list_chats(unread=True)["total"], 0)
        api.mark_message_read(m["id"], read=False)
        self.assertTrue(db.get_task(m["id"])["unread"])
        with self.assertRaises(HTTPException) as cm:
            api.mark_message_read(t["id"])
        self.assertEqual(cm.exception.status_code, 400)

    def test_reading_a_chat_covers_every_reply(self):
        root = self.msg()
        db.mark_done(root["id"], "one")
        reply = db.add_task(self.project["id"], "more", thread_id=root["id"])
        db.mark_done(reply["id"], "two")
        self.assertEqual(api.mark_chat_read(reply["id"]), {"marked": 2, "unread": 0})

    def test_filters(self):
        m, t = self.msg(), db.add_task(self.project["id"], "fix it")
        self.assertEqual([x["id"] for x in db.list_tasks(kind="message")], [m["id"]])
        self.assertEqual([x["id"] for x in db.list_all_tasks(kind="task")], [t["id"]])
        self.assertEqual([c["id"] for c in db.list_chats(kind="task")["chats"]], [t["id"]])
        self.assertEqual(api.list_project_tasks(self.project["id"])[0]["kind"] in ("task", "message"), True)
        with self.assertRaises(HTTPException):
            api.list_project_tasks(self.project["id"], kind="bogus")

    def test_migration_classifies_old_rows_without_rerouting(self):
        old_msg = self.msg("old question")
        db.mark_done(old_msg["id"], "old answer")
        old_task = db.add_task(self.project["id"], "old fix")
        old_assist = self.make_task("old mail request")
        conn = db.get_conn()
        before = {r["id"]: tuple(r) for r in conn.execute("SELECT id, lane, model_id, status, queue_pos FROM tasks")}
        conn.execute("ALTER TABLE tasks DROP COLUMN kind")          # the pre-split schema
        conn.execute("ALTER TABLE tasks DROP COLUMN read_at")
        db._migrate(conn)
        kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM tasks")}
        self.assertEqual(kinds, {old_msg["id"]: "message", old_task["id"]: "task", old_assist["id"]: "task"})
        self.assertEqual(before, {r["id"]: tuple(r) for r in conn.execute("SELECT id, lane, model_id, status, queue_pos FROM tasks")})
        self.assertFalse(db.get_task(old_msg["id"])["unread"])      # old answers aren't a wall of unread
        db._migrate(conn)                                           # idempotent: a later start changes nothing
        self.assertEqual(db.get_task(old_task["id"])["kind"], "task")
