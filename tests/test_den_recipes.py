"""The Den 2.0 recipes: triggers, steps, approvals, and old schedules staying schedules."""

import json
from datetime import datetime
from unittest import mock

from fastapi import HTTPException

from tests.helpers import TZ, TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_bin
import den_recipes as dr


def body(**kw):
    return dr.RecipeIn(**{"name": "R", "enabled": True, "trig": "manual", "steps": [["notify", "hi"]], **kw})


class Recipes(TestBase):
    def test_steps_pass_context_and_run_in_order(self):
        p = db.add_project("Site", self._tmp.name)
        r = dr.create(body(steps=[["ask", "Summarize"], ["run", "Site · Fix the footer"], ["notify", "Done"]]))
        with mock.patch.object(dr, "_model_answer", return_value="the summary") as ask, \
                mock.patch("worker.notify_phone") as push:
            out = dr.run_now(r["id"])
        ask.assert_called_once()
        run = out["runs"][0]
        self.assertEqual(run["status"], "done")
        task = [t for t in db.list_tasks(project_id=p["id"])][0]
        self.assertIn("Fix the footer", task["prompt"])
        self.assertIn("the summary", task["prompt"])           # the ask step's answer travels on
        self.assertIn("the summary", push.call_args[0][1])

    def test_if_stops_the_run(self):
        r = dr.create(body(steps=[["cond", "Octopus is idle"], ["notify", "x"]]))
        t = db.add_task(self.project["id"], "busy", 0, None, lane="agent")
        db.mark_running(t["id"])
        with mock.patch("worker.notify_phone") as push:
            run = dr.run_now(r["id"])["runs"][0]
        self.assertEqual(run["status"], "stopped")
        push.assert_not_called()

    def test_approval_step_waits_then_carries_on(self):
        r = dr.create(body(steps=[["approve", "Send the invoice?"], ["notify", "sent"]]))
        with mock.patch("worker.notify_phone") as push:
            run = dr.run_now(r["id"])["runs"][0]
            self.assertEqual(run["status"], "waiting")
            ap = db.list_pending_approvals()[0]
            self.assertEqual(json.loads(ap["tool_input"])["command"], "Send the invoice?")
            db.decide_approval(ap["id"], True)
            dr.resume_waiting()
        self.assertEqual(dr.runs(int(r["id"][1:]))[0]["status"], "done")
        push.assert_called_once()

    def test_schedule_fires_once_a_day_on_its_days(self):
        r = dr.create(body(trig="schedule", days=[1, 0, 0, 0, 0, 0, 0], time="08:30"))
        monday = datetime(2026, 10, 12, 8, 40, tzinfo=TZ)
        with mock.patch("worker.notify_phone"):
            dr.tick(monday)
            dr.tick(monday)                                       # same day: not again
            dr.tick(datetime(2026, 10, 13, 8, 40, tzinfo=TZ))      # Tuesday: not on
        self.assertEqual(len(dr.runs(int(r["id"][1:]))), 1)

    def test_task_failure_event(self):
        r = dr.create(body(trig="event", event="A task fails"))
        t = db.add_task(self.project["id"], "x", 0, None, lane="agent")
        db.mark_running(t["id"])
        db.mark_error(t["id"], "boom", needs_review=False)
        with mock.patch("worker.notify_phone"):
            dr.tick()
            dr.tick()
        runs = dr.runs(int(r["id"][1:]))
        self.assertEqual(len(runs), 1)
        self.assertIn("boom", runs[0]["trigger_text"])

    def test_old_schedule_stays_a_schedule_until_it_grows(self):
        sc = db.add_schedule("Daily summary", "Summarize", None, "daily", "08:30")
        rid = f"s{sc['id']}"
        self.assertIn(rid, [x["id"] for x in dr.list_all()])
        out = dr.update(rid, body(name="Daily summary", trig="schedule", days=[1, 1, 1, 1, 1, 0, 0], time="09:00", steps=[["run", "every project · Summarize"]]))
        self.assertEqual(out["id"], rid)
        self.assertEqual((db.get_schedule(sc["id"])["cadence"], db.get_schedule(sc["id"])["time_of_day"]), ("weekdays", "09:00"))
        grown = dr.update(rid, body(trig="schedule", days=[1] * 7, steps=[["ask", "Summarize"], ["notify", "x"]]))
        self.assertTrue(grown["id"].startswith("d"))
        self.assertIsNone(db.get_schedule(sc["id"]))
        self.assertTrue(any(i["kind"] == "schedule" for i in den_bin.list_bin()["items"]))   # the old one is in the bin

    def test_validation_and_delete_to_bin(self):
        with self.assertRaises(HTTPException):
            dr.create(body(trig="event", event="nope"))
        with self.assertRaises(HTTPException):
            dr.create(body(steps=[["teleport", "x"]]))
        r = dr.create(body())
        dr.delete(r["id"])
        item = next(i for i in den_bin.list_bin()["items"] if i["kind"] == "den_recipe")
        den_bin.restore("den_recipe", item["id"])
        self.assertIsNotNone(dr.get(int(r["id"][1:])))
