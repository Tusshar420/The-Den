"""The Den 2.0 Recycle bin: projects, notes, recipes and schedules come back exactly as they were."""

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import api
import db
import den_bin
import pad


class Bin(TestBase):
    def setUp(self):
        super().setUp()
        pad._ready = False                     # pad makes its table on first use - in this test's database too

    def test_project_goes_to_the_bin_and_back(self):
        p = db.add_project("Site", self._tmp.name)
        t = db.add_task(p["id"], "work", 0, None, lane="agent")
        den_bin.delete_project(p["id"])
        self.assertNotIn(p["id"], [x["id"] for x in db.list_projects()])
        self.assertIsNone(db.get_next_queued_task("agent"))          # paused while in the bin
        item = next(i for i in den_bin.list_bin()["items"] if i["kind"] == "project")
        self.assertEqual((item["title"], item["days_left"]), ("Site", db.BIN_DAYS))
        den_bin.restore("project", p["id"])
        self.assertIn(p["id"], [x["id"] for x in db.list_projects()])
        self.assertEqual(db.get_next_queued_task("agent")["id"], t["id"])

    def test_delete_forever_erases_the_project_and_its_history(self):
        p = db.add_project("Old", self._tmp.name)
        t = db.add_task(p["id"], "done long ago", 0, None, lane="agent")
        den_bin.delete_project(p["id"])
        den_bin.erase("project", p["id"])
        self.assertIsNone(db.get_project(p["id"]))
        self.assertIsNone(db.get_task(t["id"]))

    def test_general_cannot_be_deleted(self):
        with self.assertRaises(HTTPException):
            den_bin.delete_project(self.project["id"])

    def test_note_round_trip(self):
        n = pad.create_note("Launch list", "DNS · SSL")
        api.pad_delete(n["id"])
        self.assertIsNone(pad.get_note(n["id"]))
        item = next(i for i in den_bin.list_bin()["items"] if i["kind"] == "note")
        self.assertEqual(item["title"], "Launch list")
        den_bin.restore("note", item["id"])
        back = pad.get_note(n["id"])
        self.assertEqual((back["title"], back["body"]), ("Launch list", "DNS · SSL"))
        self.assertEqual([i for i in den_bin.list_bin()["items"] if i["kind"] == "note"], [])

    def test_recipe_and_schedule_round_trip_and_empty(self):
        r = db.add_recipe("Run tests", "npm test")
        api.delete_recipe(r["id"])
        den_bin.restore("recipe", next(i for i in den_bin.list_bin()["items"] if i["kind"] == "recipe")["id"])
        self.assertEqual(db.get_recipe(r["id"])["name"], "Run tests")
        api.delete_recipe(r["id"])
        den_bin.empty()
        self.assertEqual(den_bin.list_bin()["items"], [])
