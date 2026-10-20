"""CRM 1: the project record - fields, checks on PATCH, the migration, progress and due dates."""

from tests.helpers import TestBase  # noqa: I001 - first, so no test sees real API keys

import sqlite3
from datetime import date
from pathlib import Path

from fastapi import HTTPException

import api
import crm
import db

TODAY = date(2026, 10, 3)


class RecordBase(TestBase):
    def setUp(self):
        super().setUp()
        self.p = db.add_project("shop", self._tmp.name)

    def patch(self, **fields):
        return api.update_project(self.p["id"], api.ProjectActiveIn(**fields))

    def refused(self, **fields):
        with self.assertRaises(HTTPException) as cm:
            self.patch(**fields)
        self.assertEqual(cm.exception.status_code, 400)
        return cm.exception.detail

    def get(self):
        return db.get_project(self.p["id"])

    def todo(self, phase=None, status="pending"):
        t = db.add_todo(self.p["id"], "item")
        with db._lock:
            db.get_conn().execute("UPDATE todos SET status = ?, phase_id = ? WHERE id = ?",
                                  (status, phase["id"] if phase else None, t["id"]))
            db.get_conn().commit()
        return t


class Defaults(RecordBase):
    def test_a_new_project_has_an_empty_record(self):
        p = self.get()
        self.assertEqual(p["description"], "")
        self.assertEqual((p["priority"], p["health"], p["currency"]), ("medium", "on_track", "USD"))
        self.assertEqual((p["tags"], p["links"]), ([], []))
        for k in ("client_id", "start_date", "due_date", "completed_date", "contract_value", "billing_type", "owner"):
            self.assertIsNone(p[k], k)

    def test_get_returns_the_record_with_progress_and_schedule(self):
        r = api.get_project(self.p["id"])
        self.assertEqual(r["name"], "shop")
        self.assertIsNone(r["progress"]["percent"])
        self.assertEqual(r["schedule"], {"days_left": None, "due_state": None})

    def test_get_unknown_project_is_404(self):
        with self.assertRaises(HTTPException) as cm:
            api.get_project(9999)
        self.assertEqual(cm.exception.status_code, 404)

    def test_the_list_carries_each_projects_schedule(self):
        self.patch(due_date="2999-01-01")
        row = next(p for p in api.list_projects() if p["id"] == self.p["id"])
        self.assertEqual(row["schedule"]["due_state"], "upcoming")


class Patch(RecordBase):
    def test_sets_every_field(self):
        r = self.patch(description="# Shop\nA **store**", priority="critical", health="at_risk",
                       start_date="2026-09-01", due_date="2026-12-01", completed_date=None,
                       contract_value=12500.555, currency="inr", billing_type="retainer", owner=" Asha ",
                       tags=["web", "Web", " client  work ", ""],
                       links=[{"label": "Repo", "url": "github.com/me/shop"}, {"url": "https://shop.example.com"}])
        self.assertEqual(r["priority"], "critical")
        self.assertEqual(r["health"], "at_risk")
        self.assertEqual(r["contract_value"], 12500.56)
        self.assertEqual(r["currency"], "INR")
        self.assertEqual(r["owner"], "Asha")
        self.assertEqual(r["tags"], ["web", "client work"])
        self.assertEqual(r["links"], [{"label": "Repo", "url": "https://github.com/me/shop"},
                                      {"label": "shop.example.com", "url": "https://shop.example.com"}])
        self.assertEqual(self.get()["tags"], ["web", "client work"])   # stored, not just echoed
        self.assertIsNotNone(self.get()["updated_at"])
        self.assertIn("progress", r)

    def test_links_with_a_port_are_fine(self):
        r = self.patch(links=[{"label": "Local", "url": "localhost:3000"}, {"url": "http://staging.example.com:8080/x"}])
        self.assertEqual([x["url"] for x in r["links"]], ["https://localhost:3000", "http://staging.example.com:8080/x"])

    def test_only_sent_fields_change(self):
        self.patch(owner="Asha", priority="high")
        self.patch(health="off_track")
        p = self.get()
        self.assertEqual((p["owner"], p["priority"], p["health"]), ("Asha", "high", "off_track"))

    def test_null_clears_optional_fields(self):
        self.patch(owner="Asha", due_date="2026-12-01", contract_value=10, billing_type="fixed", tags=["a"])
        self.patch(owner=None, due_date="", contract_value=None, billing_type=None, tags=None)
        p = self.get()
        self.assertEqual((p["owner"], p["due_date"], p["contract_value"], p["billing_type"], p["tags"]),
                         (None, None, None, None, []))

    def test_status_patch_alone_leaves_the_record_alone(self):
        self.patch(owner="Asha")
        stamp = self.get()["updated_at"]
        self.patch(status="in_progress")
        self.assertEqual(self.get()["owner"], "Asha")
        self.assertEqual(self.get()["updated_at"], stamp)

    def test_general_has_no_record(self):
        with self.assertRaises(HTTPException):
            api.update_project(self.project["id"], api.ProjectActiveIn(owner="me"))


class Validation(RecordBase):
    def test_due_before_start_is_refused(self):
        self.assertIn("before the start", self.refused(start_date="2026-10-10", due_date="2026-10-01"))

    def test_checked_against_the_saved_dates_too(self):
        self.patch(start_date="2026-10-10")
        self.refused(due_date="2026-10-09")
        self.patch(due_date="2026-10-10")                     # same day is fine
        self.refused(start_date="2026-10-11")                  # moving start past the saved due date

    def test_completed_before_start_is_refused(self):
        self.patch(start_date="2026-10-10")
        self.assertIn("Completed", self.refused(completed_date="2026-10-01"))

    def test_bad_values_are_refused_and_nothing_is_saved(self):
        for bad in (dict(priority="urgent"), dict(priority=None), dict(health="green"), dict(billing_type="weekly"),
                    dict(due_date="31/10/2026"), dict(due_date="2026-02-30"), dict(contract_value=-1),
                    dict(contract_value=float("inf")), dict(currency="RUPEES"), dict(currency=None),
                    dict(owner="x" * 121), dict(tags=["x" * 41]), dict(tags=[str(i) for i in range(31)]),
                    dict(links=[{"url": "javascript:alert(1)"}]), dict(links=[{"url": "ftp://x.com"}]),
                    dict(links=[{"url": "mailto:me@x.com"}]), dict(links=[{"url": "x.com:port"}]),
                    dict(links=[{"url": "  "}]), dict(links=[{"url": "https://a.com", "label": "x" * 61}]),
                    dict(description="x" * 20001)):
            with self.subTest(bad):
                self.refused(**{"owner": "Someone", "status": "on_hold", **bad})
        p = self.get()
        self.assertIsNone(p["owner"])
        self.assertEqual(p["status"], "not_started")           # the status in the same request wasn't saved either

    def test_unknown_record_fields_are_refused_by_clean(self):
        with self.assertRaises(crm.RecordError):
            crm.clean({"client_id": 3}, self.get())
        with self.assertRaises(ValueError):
            db.update_project_record(self.p["id"], {"name": "evil"})


class Migration(TestBase):
    def test_an_old_database_gains_the_columns_and_keeps_its_data(self):
        path = Path(self._tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, "
                    "path TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
        old.execute("INSERT INTO projects (name, path, created_at) VALUES ('legacy', '/tmp', '2026-01-01')")
        old.commit()
        old.close()
        db._conn.close()
        db.DB_PATH, db._conn = path, None
        p = db.get_project_by_name("legacy")
        self.assertEqual((p["path"], p["created_at"]), ("/tmp", "2026-01-01"))
        self.assertEqual((p["priority"], p["health"], p["tags"], p["links"], p["description"]),
                         ("medium", "on_track", [], [], ""))
        db._conn.close()
        db._conn = None
        self.assertEqual(db.get_project_by_name("legacy")["name"], "legacy")   # running it again is a no-op


class Schedule(RecordBase):
    def state(self, **p):
        return crm.schedule({"status": "in_progress", **p}, TODAY)

    def test_states(self):
        self.assertEqual(self.state(), {"days_left": None, "due_state": None})
        self.assertEqual(self.state(due_date="2026-10-02"), {"days_left": -1, "due_state": "overdue"})
        self.assertEqual(self.state(due_date="2026-10-03")["due_state"], "due_soon")
        self.assertEqual(self.state(due_date="2026-10-10"), {"days_left": 7, "due_state": "due_soon"})
        self.assertEqual(self.state(due_date="2026-10-11")["due_state"], "upcoming")

    def test_finished_projects_are_never_overdue(self):
        self.assertEqual(self.state(due_date="2026-01-01", completed_date="2026-01-02")["due_state"], "done")
        self.assertEqual(self.state(due_date="2026-01-01", status="completed")["due_state"], "done")
        self.assertEqual(self.state(due_date="2026-01-01", status="archived")["due_state"], "done")


class Progress(RecordBase):
    def pct(self):
        return crm.progress(self.p["id"])

    def test_nothing_to_measure(self):
        self.assertEqual(self.pct(), {"percent": None, "todos_done": 0, "todos_total": 0,
                                      "phases_done": 0, "phases_total": 0})

    def test_todos_only(self):
        self.todo(status="done")
        self.todo()
        self.todo(status="cancelled")                          # doesn't count
        self.assertEqual(self.pct()["percent"], 50)
        self.assertEqual((self.pct()["todos_done"], self.pct()["todos_total"]), (1, 2))

    def test_phases_and_todos(self):
        a = db.add_phase(self.p["id"], "Build")
        b = db.add_phase(self.p["id"], "Launch")
        db.add_phase(self.p["id"], "Empty")                   # one unit, not done
        self.todo(a, "done")
        self.todo(a)
        self.todo(b)
        self.todo(b)
        db.update_phase(b["id"], status="done")               # done phase: both its todos count as done
        self.todo(status="done")                               # loose todo
        # units: Build 2 (1 done) + Launch 2 (2 done) + Empty 1 + loose 1 (done) = 4/6
        r = self.pct()
        self.assertEqual(r["percent"], 67)
        self.assertEqual((r["phases_done"], r["phases_total"]), (1, 3))
        self.assertEqual((r["todos_done"], r["todos_total"]), (2, 5))
