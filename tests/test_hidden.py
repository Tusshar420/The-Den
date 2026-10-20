"""Projects and queue items can be hidden; they drop out of the normal lists and show under hidden=true."""

import api
import db
from tests.helpers import TestBase


class Hidden(TestBase):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("shop", self._tmp.name)
        self.task = db.add_task(self.proj["id"], "do a thing")

    def test_hiding_a_project_flags_it(self):
        api.update_project(self.proj["id"], api.ProjectActiveIn(hidden=True))
        self.assertEqual(db.get_project(self.proj["id"])["hidden"], 1)
        api.update_project(self.proj["id"], api.ProjectActiveIn(hidden=False))
        self.assertEqual(db.get_project(self.proj["id"])["hidden"], 0)

    def test_hidden_task_is_filtered_by_the_hidden_flag(self):
        api.set_tasks_hidden(api.HiddenIn(ids=[self.task["id"]], hidden=True))
        self.assertEqual([t["id"] for t in db.list_all_tasks(hidden=True)], [self.task["id"]])
        self.assertEqual(db.list_all_tasks(hidden=False), [])
        api.set_tasks_hidden(api.HiddenIn(ids=[self.task["id"]], hidden=False))
        self.assertEqual(db.list_all_tasks(hidden=True), [])

    def test_hidden_chat_leaves_messages(self):
        self.assertEqual(db.list_chats()["total"], 1)
        api.set_tasks_hidden(api.HiddenIn(ids=[self.task["id"]]))
        self.assertEqual(db.list_chats()["total"], 0)
