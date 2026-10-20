"""To-do checklist: one item per pasted line, tick / untick, move between projects, delete."""

from fastapi import HTTPException

import api
import checklist
import db
from tests.helpers import TestBase


class Checklist(TestBase):
    def setUp(self):
        super().setUp()
        checklist._ready = False
        self.pid = db.add_project("shop", self._tmp.name + "/shop")["id"]

    def test_pasted_lines_become_separate_items_and_markers_are_stripped(self):
        r = api.checklist_create(api.ChecklistIn(text="- buy milk\n\n2) call Sam\n[ ] ship it\r\n* [x] done already\n  \n"))
        self.assertEqual([i["text"] for i in r["items"]], ["buy milk", "call Sam", "ship it", "done already"])
        self.assertEqual([i["position"] for i in r["items"]], [0, 1, 2, 3])

    def test_blank_text_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            api.checklist_create(api.ChecklistIn(text=" \n \n"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_project_filter_and_unknown_project(self):
        api.checklist_create(api.ChecklistIn(text="a\nb", project_id=self.pid))
        api.checklist_create(api.ChecklistIn(text="loose"))
        self.assertEqual([i["text"] for i in api.checklist_list(self.pid)["items"]], ["a", "b"])
        self.assertEqual(len(api.checklist_list()["items"]), 3)
        with self.assertRaises(HTTPException) as cm:
            api.checklist_create(api.ChecklistIn(text="x", project_id=999))
        self.assertEqual(cm.exception.status_code, 404)

    def test_tick_untick_edit_move_delete(self):
        i = api.checklist_create(api.ChecklistIn(text="a\nb"))["items"]
        t = api.checklist_update(i[0]["id"], api.ChecklistUpdateIn(done=True))
        self.assertEqual((t["done"], bool(t["done_at"])), (1, True))
        # ticked items sink below open ones
        self.assertEqual([x["text"] for x in api.checklist_list()["items"]], ["b", "a"])
        t = api.checklist_update(i[0]["id"], api.ChecklistUpdateIn(done=False))
        self.assertEqual((t["done"], t["done_at"]), (0, None))
        t = api.checklist_update(i[0]["id"], api.ChecklistUpdateIn(text="  a2 ", project_id=self.pid))
        self.assertEqual((t["text"], t["project_id"]), ("a2", self.pid))
        t = api.checklist_update(i[0]["id"], api.ChecklistUpdateIn(project_id=None))
        self.assertIsNone(t["project_id"])
        api.checklist_delete(i[1]["id"])
        self.assertEqual(len(api.checklist_list()["items"]), 1)
        with self.assertRaises(HTTPException):
            api.checklist_delete(i[1]["id"])

    def test_clear_done_and_deleting_project_unfiles_items(self):
        i = api.checklist_create(api.ChecklistIn(text="a\nb\nc", project_id=self.pid))["items"]
        api.checklist_update(i[0]["id"], api.ChecklistUpdateIn(done=True))
        self.assertEqual(api.checklist_clear_done(self.pid)["removed"], 1)
        db.delete_project(self.pid)
        left = api.checklist_list()["items"]
        self.assertEqual(([x["text"] for x in left], {x["project_id"] for x in left}), (["b", "c"], {None}))

    def test_in_progress_status_and_done_stay_in_step(self):
        i = api.checklist_create(api.ChecklistIn(text="a"))["items"][0]
        self.assertEqual(i["status"], "todo")
        t = api.checklist_update(i["id"], api.ChecklistUpdateIn(status="doing"))
        self.assertEqual((t["status"], t["done"], t["done_at"]), ("doing", 0, None))
        t = api.checklist_update(i["id"], api.ChecklistUpdateIn(status="done"))
        self.assertEqual((t["status"], t["done"], bool(t["done_at"])), ("done", 1, True))
        t = api.checklist_update(i["id"], api.ChecklistUpdateIn(done=False))   # the old tick box still works
        self.assertEqual((t["status"], t["done"]), ("todo", 0))
        with self.assertRaises(HTTPException) as cm:
            api.checklist_update(i["id"], api.ChecklistUpdateIn(status="later"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_old_table_gets_a_status_column(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE IF EXISTS checklist_items")
        conn.execute("""CREATE TABLE checklist_items (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
            project_id INTEGER, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, done_at TEXT)""")
        conn.execute("INSERT INTO checklist_items (text, done, created_at) VALUES ('open', 0, 'x'), ('ticked', 1, 'x')")
        conn.commit()
        checklist._ready = False
        self.assertEqual({i["text"]: i["status"] for i in api.checklist_list()["items"]}, {"open": "todo", "ticked": "done"})

    def test_apply_puts_deleted_items_back_and_reorders(self):
        a, b, c = api.checklist_create(api.ChecklistIn(text="a\nb\nc", project_id=self.pid))["items"]
        api.checklist_delete(b["id"])
        # undo the delete: same id, same place; and move c to the top
        r = api.checklist_apply(api.ChecklistApplyIn(upsert=[b, {"id": c["id"], "position": -1}]))
        self.assertEqual([(x["id"], x["text"]) for x in r["items"]], [(c["id"], "c"), (a["id"], "a"), (b["id"], "b")])
        # a project deleted in the meantime leaves the restored item unfiled
        api.checklist_delete(a["id"])
        db.delete_project(self.pid)
        api.checklist_apply(api.ChecklistApplyIn(upsert=[{**a, "status": "doing"}], delete=[c["id"]]))
        back = checklist.get_item(a["id"])
        self.assertEqual((back["project_id"], back["status"], back["done"]), (None, "doing", 0))
        self.assertIsNone(checklist.get_item(c["id"]))
        with self.assertRaises(HTTPException):
            api.checklist_apply(api.ChecklistApplyIn(upsert=[{"id": a["id"], "status": "nope"}]))


class PriorityAndDue(TestBase):
    def setUp(self):
        super().setUp()
        checklist._ready = False          # each test has a fresh database

    def test_parsed_items_keep_priority_due_and_done(self):
        items = checklist.add_items("", items=[{"text": "Renew cert", "priority": 3, "due_date": "2026-10-14"},
                                               {"text": "Old one", "done": True}, {"text": "  "}], first=True)
        self.assertEqual([(i["text"], i["priority"], i["due_date"], i["done"]) for i in items],
                         [("Renew cert", 3, "2026-10-14", 0), ("Old one", 0, None, 1)])
        it = checklist.update_item(items[0]["id"], {"priority": 9, "due_date": "not a date"})
        self.assertEqual((it["priority"], it["due_date"]), (3, None))
        later = checklist.add_items("Last")[0]
        top = checklist.add_items("Top", first=True)[0]
        self.assertLess(top["position"], items[0]["position"])
        self.assertGreater(later["position"], items[1]["position"])


class SendToOctopus(TestBase):
    """Hand to-dos to Up next or the Backlog, in bulk; they follow the work and tick off when it's done."""

    def setUp(self):
        super().setUp()
        checklist._ready = False
        self.pid = db.add_project("shop", self._tmp.name + "/shop")["id"]
        self.crm = db.add_project("crm only")["id"]          # no folder

    def _items(self, text, **kw):
        return [i["id"] for i in api.checklist_create(api.ChecklistIn(text=text, **kw))["items"]]

    def test_bulk_up_next_follows_the_task_and_ticks_off_when_done(self):
        a, b = self._items("fix login\nship it", project_id=self.pid)
        r = api.checklist_send(api.ChecklistSendIn(ids=[a, b], to="queue"))
        self.assertEqual((len(r["sent"]), r["skipped"]), (2, []))
        tid = checklist.get_item(a)["task_id"]
        task = db.get_task(tid)
        self.assertEqual((task["project_id"], task["prompt"], task["status"]), (self.pid, "fix login", "queued"))
        self.assertEqual(checklist.get_item(a)["link"]["state"], "queued")
        # sending again while it's in Up next does nothing
        self.assertEqual(api.checklist_send(api.ChecklistSendIn(ids=[a], to="backlog"))["skipped"][0]["reason"], "Already in Up next")
        db.mark_running(tid)
        self.assertEqual(checklist.get_item(a)["link"]["state"], "running")
        db.mark_done(tid, "All fixed.")
        it = checklist.get_item(a)
        self.assertEqual((it["link"]["state"], it["status"], it["done"]), ("done", "done", 1))
        # unticking by hand sticks: it isn't ticked again on the next read
        api.checklist_update(a, api.ChecklistUpdateIn(done=False))
        self.assertEqual(checklist.get_item(a)["status"], "todo")
        self.assertEqual(checklist.get_item(b)["status"], "todo")

    def test_a_question_shows_as_waiting_and_a_reply_moves_it_on(self):
        (a,) = self._items("pick a colour", project_id=self.pid)
        api.checklist_send(api.ChecklistSendIn(ids=[a], to="queue"))
        tid = checklist.get_item(a)["task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "Looked at the theme.\nBlue or green?")
        db.set_awaiting_reply(tid, True)
        link = checklist.get_item(a)["link"]
        self.assertEqual((link["state"], link["question"]), ("waiting", "Blue or green?"))
        self.assertEqual(checklist.get_item(a)["status"], "todo")      # not done while it waits on you
        reply = db.add_task(self.pid, "Blue", thread_id=tid)
        link = checklist.get_item(a)["link"]
        self.assertEqual((link["state"], link["last_id"], link["question"]), ("queued", reply["id"], None))
        db.mark_running(reply["id"])
        db.mark_done(reply["id"], "Done, it's blue.")
        self.assertEqual(checklist.get_item(a)["status"], "done")

    def test_pending_approval_shows(self):
        (a,) = self._items("deploy", project_id=self.pid)
        api.checklist_send(api.ChecklistSendIn(ids=[a], to="queue"))
        tid = checklist.get_item(a)["task_id"]
        db.mark_running(tid)
        db.create_approval(tid, "Bash", "rm -rf build")
        self.assertEqual(checklist.get_item(a)["link"]["state"], "approval")

    def test_backlog_then_queued_from_the_backlog(self):
        a, b = self._items("write docs\n!done one", project_id=self.pid)
        api.checklist_update(b, api.ChecklistUpdateIn(done=True))
        r = api.checklist_send(api.ChecklistSendIn(ids=[a, b], to="backlog"))
        self.assertEqual([s["reason"] for s in r["skipped"]], ["Already done"])
        it = checklist.get_item(a)
        todo = db.get_todo(it["todo_id"])
        self.assertEqual((todo["text"], todo["project_id"], it["link"]["state"], it["link"]["via"]), ("write docs", self.pid, "backlog", "backlog"))
        db.update_todo(todo["id"], checked=True)
        db.run_checked_todos(self.pid)
        link = checklist.get_item(a)["link"]
        self.assertEqual((link["state"], link["via"]), ("queued", "backlog"))
        db.mark_running(link["task_id"])
        db.mark_done(link["task_id"], "Written.")
        self.assertEqual(checklist.get_item(a)["status"], "done")
        # deleting the backlog item breaks the link quietly
        (c,) = self._items("later", project_id=self.pid)
        api.checklist_send(api.ChecklistSendIn(ids=[c], to="backlog"))
        db.delete_todo(checklist.get_item(c)["todo_id"])
        self.assertIsNone(checklist.get_item(c)["link"])

    def test_unfiled_items_and_projects_without_a_folder(self):
        (a,) = self._items("loose")
        api.checklist_send(api.ChecklistSendIn(ids=[a], to="backlog", project_id=self.pid))
        self.assertEqual(checklist.get_item(a)["project_id"], self.pid)      # filed where it went
        (b,) = self._items("no folder", project_id=self.crm)
        r = api.checklist_send(api.ChecklistSendIn(ids=[b], to="queue"))
        self.assertIn("Link a folder", r["skipped"][0]["reason"])
        r = api.checklist_send(api.ChecklistSendIn(ids=[b], to="backlog"))  # the backlog doesn't need one
        self.assertEqual(len(r["sent"]), 1)
        with self.assertRaises(HTTPException):
            api.checklist_send(api.ChecklistSendIn(ids=[b], to="elsewhere"))
