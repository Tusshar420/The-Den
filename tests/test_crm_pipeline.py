"""CRM 2: the pipeline stage on a project - mapping from status, moving between stages, the list the table reads."""

from tests.helpers import TestBase  # noqa: I001 - first, so no test sees real API keys

import sqlite3
from pathlib import Path

from fastapi import HTTPException

import api
import crm
import db


class PipelineBase(TestBase):
    def setUp(self):
        super().setUp()
        self.p = db.add_project("shop", self._tmp.name)

    def patch(self, **fields):
        return api.update_project(self.p["id"], api.ProjectActiveIn(**fields))

    def listed(self):
        return next(x for x in api.list_projects() if x["id"] == self.p["id"])


class Mapping(PipelineBase):
    def test_a_project_with_no_stage_picked_gets_one_from_its_status(self):
        for status, stage in (("not_started", "lead"), ("in_progress", "active"), ("need_info", "active"),
                              ("on_hold", "on_hold"), ("completed", "done"), ("archived", "done")):
            db.set_project_status(self.p["id"], status)
            self.assertEqual(self.listed()["stage"], stage, status)

    def test_nothing_is_stored_until_a_stage_is_picked(self):
        self.assertIsNone(db.get_project(self.p["id"])["stage"])

    def test_every_stage_maps_to_a_real_status(self):
        self.assertEqual(set(crm.STAGE_STATUS), set(crm.STAGES))
        self.assertTrue(set(crm.STAGE_STATUS.values()) <= set(db.PROJECT_STATUSES))


class Moving(PipelineBase):
    def test_picking_a_stage_saves_it_and_moves_the_status(self):
        r = self.patch(stage="active")
        self.assertEqual((r["stage"], r["status"]), ("active", "in_progress"))
        self.assertEqual(db.get_project(self.p["id"])["status"], "in_progress")
        r = self.patch(stage="done")
        self.assertEqual((r["stage"], r["status"]), ("done", "completed"))
        r = self.patch(stage="on_hold")
        self.assertEqual(r["status"], "on_hold")
        r = self.patch(stage="lost")
        self.assertEqual((r["stage"], r["status"]), ("lost", "archived"))

    def test_lead_and_proposal_are_both_not_started_and_stay_distinct(self):
        self.patch(stage="proposal")
        self.assertEqual((self.listed()["stage"], db.get_project(self.p["id"])["status"]), ("proposal", "not_started"))
        self.patch(stage="lead")
        self.assertEqual(self.listed()["stage"], "lead")

    def test_a_stage_that_already_fits_leaves_the_status_alone(self):
        db.set_project_status(self.p["id"], "need_info")
        self.patch(stage="active")
        self.assertEqual(db.get_project(self.p["id"])["status"], "need_info")

    def test_changing_the_status_moves_the_stage_only_when_it_no_longer_fits(self):
        self.patch(stage="proposal")
        self.assertEqual(self.patch(status="not_started")["stage"], "proposal")      # still fits
        self.assertEqual(self.patch(status="completed")["stage"], "done")
        self.assertEqual(self.patch(status="in_progress")["stage"], "active")
        self.assertEqual(self.patch(status="need_info")["stage"], "active")           # active fits need_info
        self.assertEqual(self.patch(status="on_hold")["stage"], "on_hold")

    def test_a_status_set_with_a_stage_is_taken_as_sent(self):
        r = self.patch(stage="active", status="need_info")
        self.assertEqual((r["stage"], r["status"]), ("active", "need_info"))

    def test_an_unknown_stage_is_refused_and_changes_nothing(self):
        with self.assertRaises(HTTPException) as cm:
            self.patch(stage="won", priority="high")
        self.assertEqual(cm.exception.status_code, 400)
        p = db.get_project(self.p["id"])
        self.assertEqual((p["stage"], p["priority"], p["status"]), (None, "medium", "not_started"))

    def test_other_fields_do_not_disturb_the_stage(self):
        self.patch(stage="proposal")
        self.patch(priority="high", due_date="2026-12-01")
        self.assertEqual(self.listed()["stage"], "proposal")

    def test_a_project_can_be_moved_back(self):
        self.patch(stage="done")
        r = self.patch(stage="active")
        self.assertEqual((r["stage"], r["status"]), ("active", "in_progress"))

    def test_the_record_comes_back_with_the_stage(self):
        self.assertEqual(api.get_project(self.p["id"])["stage"], "lead")
        self.patch(stage="lost")
        self.assertEqual(api.get_project(self.p["id"])["stage"], "lost")


class Create(TestBase):
    def create(self, **body):
        return api.create_project(api.ProjectIn(name=body.pop("name", "client site"), **body))

    def test_a_new_project_can_start_in_a_stage(self):
        p = self.create(stage="proposal")
        self.assertEqual((p["stage"], p["status"]), ("proposal", "not_started"))
        p = self.create(name="b", stage="active")
        self.assertEqual((p["stage"], p["status"]), ("active", "in_progress"))

    def test_a_new_project_without_one_follows_its_status(self):
        self.assertEqual(self.create(name="c", status="on_hold")["stage"], "on_hold")
        self.assertEqual(self.create(name="d")["stage"], "lead")


class ListForTheTable(PipelineBase):
    def test_the_list_carries_what_the_table_shows(self):
        self.patch(stage="active", due_date="2026-12-01", contract_value=1200, tags=["web"], priority="high")
        row = self.listed()
        for k in ("stage", "progress", "schedule", "priority", "health", "due_date", "start_date", "contract_value", "currency", "tags", "category"):
            self.assertIn(k, row)
        self.assertEqual((row["stage"], row["contract_value"], row["tags"]), ("active", 1200, ["web"]))
        self.assertIn("percent", row["progress"])


class Migration(TestBase):
    def test_an_old_database_gains_the_stage_column_and_keeps_its_data(self):
        path = Path(self._tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, "
                    "path TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'not_started')")
        old.execute("INSERT INTO projects (name, path, created_at, status) VALUES ('legacy', '/tmp', '2026-01-01', 'on_hold')")
        old.commit()
        old.close()
        db._conn.close()
        db.DB_PATH, db._conn = path, None
        p = db.get_project_by_name("legacy")
        self.assertEqual((p["status"], p["stage"]), ("on_hold", None))
        self.assertEqual(crm.stage_of(p), "on_hold")


class StatusOnly(PipelineBase):
    def test_a_status_change_with_no_stage_picked_stores_no_stage(self):
        self.patch(status="in_progress")
        self.assertIsNone(db.get_project(self.p["id"])["stage"])
        self.assertEqual(self.listed()["stage"], "active")
