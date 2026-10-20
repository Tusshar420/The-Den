"""The team: employees, tasks assigned to them, time off, and meeting attendance."""

import unittest

from fastapi import HTTPException

import api
import db
import invites
import team
import timeline
from tests.helpers import TestBase


class TeamTests(TestBase):
    def person(self, **kw):
        return db.add_employee(team.clean_employee({"name": "Asha Rao", "email": "Asha@Example.com", **kw}))

    def test_clean_employee_normalises_and_validates(self):
        e = self.person(job_title="Designer")
        self.assertEqual(e["email"], "asha@example.com")
        self.assertEqual(e["status"], "active")
        for bad in ({"name": ""}, {"email": "nope"}, {"phone": "ab"}, {"status": "x"}, {"start_date": "31/10"}):
            with self.assertRaises(team.TeamError):
                team.clean_employee({"name": "X", **bad} if "name" not in bad else bad, None)
        with self.assertRaises(team.TeamError):
            team.clean_employee({"job_title": "no name"})

    def test_manager_cannot_loop(self):
        a, b = self.person(), self.person(name="Bo")
        db.update_employee(b["id"], team.clean_employee({"manager_id": a["id"]}, b["id"]))
        with self.assertRaises(team.TeamError):
            team.clean_employee({"manager_id": b["id"]}, a["id"])
        with self.assertRaises(team.TeamError):
            team.clean_employee({"manager_id": a["id"]}, a["id"])

    def test_tasks_counts_and_done(self):
        e = self.person()
        past = (timeline.today().replace(year=2020)).isoformat()
        t = db.add_employee_task(team.clean_task({"employee_id": e["id"], "title": "Ship it", "due_date": past}, True))
        self.assertEqual(team.list_all()[0]["overdue_tasks"], 1)
        db.update_employee_task(t["id"], team.clean_task({"status": "done"}))
        self.assertTrue(db.get_employee_task(t["id"])["done_at"])
        self.assertEqual(team.list_all()[0]["open_tasks"], 0)

    def test_leave_marks_away_today(self):
        e = self.person()
        today = timeline.today().isoformat()
        db.add_leave(team.clean_leave({"employee_id": e["id"], "start_date": today}))
        self.assertTrue(team.list_all()[0]["away_today"])
        with self.assertRaises(team.TeamError):
            team.clean_leave({"employee_id": e["id"], "start_date": today, "end_date": "2000-01-01"})

    def test_meeting_attendance_and_invite(self):
        e = self.person()
        p = db.add_project("Proj", "/tmp") if hasattr(db, "add_project") else None
        if not p:
            self.skipTest("no add_project helper")
        ev = timeline.create(p["id"], "meeting", "Standup", timeline.today().isoformat(), employee_ids=[e["id"]])
        self.assertEqual(ev["employee_ids"], [e["id"]])
        self.assertEqual(invites.guest_emails(ev), ["asha@example.com"])
        self.assertEqual(len(team.page(e["id"])["meetings"]), 1)
        with self.assertRaises(timeline.TimelineError):
            timeline.create(p["id"], "meeting", "Bad", timeline.today().isoformat(), employee_ids=[999])
        db.delete_employee(e["id"])
        self.assertEqual(db.get_project_event(ev["id"])["employee_ids"], [])


class TeamApiTests(TestBase):
    def test_api_round_trip(self):
        e = api.create_employee(api.EmployeeIn(name="Dev Patel", job_title="Engineer", email="dev@example.com"))
        self.assertEqual(e["status"], "active")
        t = api.create_employee_task(e["id"], api.EmployeeTaskIn(title="Fix login", priority="high"))
        self.assertEqual(t["employee_id"], e["id"])
        api.update_employee_task(t["id"], api.EmployeeTaskIn(status="done"))
        page = api.get_employee(e["id"])
        self.assertEqual(page["open_tasks"], 0)
        self.assertEqual(page["tasks"][0]["status"], "done")
        api.add_employee_leave(e["id"], api.EmployeeLeaveIn(start_date="2030-01-01", end_date="2030-01-05"))
        self.assertEqual(len(api.get_employee(e["id"])["leave"]), 1)
        self.assertEqual(api.update_employee(e["id"], api.EmployeeIn(department="Eng"))["department"], "Eng")
        self.assertEqual([x["id"] for x in api.list_employees()], [e["id"]])
        api.delete_employee(e["id"])
        self.assertEqual(api.list_employees(), [])
        self.assertEqual(db.list_employee_tasks(), [])

    def test_api_errors(self):
        with self.assertRaises(HTTPException) as cm:
            api.create_employee(api.EmployeeIn(job_title="no name"))
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            api.get_employee(999)
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(HTTPException) as cm:
            api.create_employee_task(999, api.EmployeeTaskIn(title="x"))
        self.assertEqual(cm.exception.status_code, 404)

    def test_meeting_via_api(self):
        e = api.create_employee(api.EmployeeIn(name="Dev Patel", email="dev@example.com"))
        ev = api.add_timeline_event(self.project["id"], api.TimelineEventIn(kind="meeting", title="Sync", date="2030-02-01", employee_ids=[e["id"]]))
        self.assertEqual(ev["employee_ids"], [e["id"]])
        ev = api.update_timeline_event(ev["id"], api.TimelineEventUpdateIn(employee_ids=[]))
        self.assertEqual(ev["employee_ids"], [])


if __name__ == "__main__":
    unittest.main()
