"""Schedules: editing what one runs, where and when (PATCH /schedules/{id}),
turning it on and off, and deleting it."""

from datetime import datetime, timedelta

from fastapi import HTTPException

import api
import db
from tests.helpers import TestBase


def local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone()


class Base(TestBase):
    def setUp(self):
        super().setUp()
        self.shop = db.add_project("shop", self._tmp.name + "/shop")

    def make(self, **kw):
        body = {"prompt": "Write the daily summary", "cadence": "daily", "time": "09:00", "project_id": self.project["id"], **kw}
        return api.create_schedule(api.ScheduleIn(**body))

    def edit(self, sc, **kw):
        return api.update_schedule(sc["id"], api.ScheduleUpdateIn(**kw))


class Edit(Base):
    def test_change_time_moves_next_run(self):
        sc = self.edit(self.make(), time="21:30")
        self.assertEqual(sc["time_of_day"], "21:30")
        nxt = local(sc["next_run_at"])
        self.assertEqual((nxt.hour, nxt.minute), (21, 30))
        self.assertGreater(nxt, datetime.now().astimezone())

    def test_change_prompt_project_and_priority(self):
        sc = self.edit(self.make(), prompt="  Check the inbox  ", project_id=self.shop["id"], priority=1, model="claude-code")
        self.assertEqual((sc["prompt"], sc["label"]), ("Check the inbox", "Check the inbox"))   # label followed the prompt
        self.assertEqual((sc["project_id"], sc["project_name"], sc["priority"], sc["model"]), (self.shop["id"], "shop", 1, "claude-code"))

    def test_custom_label_is_kept_when_prompt_changes(self):
        sc = self.edit(self.make(label="Summary"), prompt="Write a longer summary")
        self.assertEqual(sc["label"], "Summary")

    def test_null_project_means_all_projects(self):
        sc = self.edit(self.make(), project_id=None)
        self.assertIsNone(sc["project_id"])

    def test_untouched_fields_stay(self):
        sc = self.make(cadence="weekly", weekday=2, time="07:15")
        after = self.edit(sc, priority=-1)
        for k in ("prompt", "cadence", "weekday", "time_of_day", "next_run_at", "enabled"):
            self.assertEqual(after[k], sc[k], k)

    def test_switch_to_weekly_and_back(self):
        sc = self.edit(self.make(), cadence="weekly", weekday=4)
        self.assertEqual(local(sc["next_run_at"]).weekday(), 4)
        sc = self.edit(sc, cadence="daily")
        self.assertIsNone(sc["weekday"])     # a weekday only means something for weekly

    def test_once_on_a_new_date(self):
        day = (datetime.now() + timedelta(days=5)).date().isoformat()
        sc = self.edit(self.make(cadence="once"), date=day, time="18:00")
        nxt = local(sc["next_run_at"])
        self.assertEqual((nxt.date().isoformat(), nxt.hour), (day, 18))

    def test_once_keeps_its_day_when_only_the_time_changes(self):
        day = (datetime.now() + timedelta(days=3)).date().isoformat()
        sc = self.edit(self.make(cadence="once", date=day), time="06:45")
        nxt = local(sc["next_run_at"])
        self.assertEqual((nxt.date().isoformat(), nxt.hour, nxt.minute), (day, 6, 45))

    def test_turning_a_fired_once_schedule_back_on(self):
        sc = self.make(cadence="once")
        db.mark_schedule_fired(sc["id"])
        self.assertFalse(db.get_schedule(sc["id"])["enabled"])
        sc = self.edit(sc, enabled=True, time="18:00")
        self.assertTrue(sc["enabled"])
        self.assertGreater(local(sc["next_run_at"]), datetime.now().astimezone())

    def test_off_and_on(self):
        sc = self.edit(self.make(), enabled=False)
        self.assertEqual((sc["enabled"], sc["next_run_at"]), (0, None))
        sc = self.edit(sc, enabled=True)
        self.assertEqual(sc["enabled"], 1)
        self.assertIsNotNone(sc["next_run_at"])

    def test_editing_while_off_stays_off(self):
        sc = self.edit(self.edit(self.make(), enabled=False), time="10:00")
        self.assertEqual((sc["enabled"], sc["next_run_at"], sc["time_of_day"]), (0, None, "10:00"))

    def test_rejects_bad_input(self):
        sc = self.make()
        for kw, code in (({"time": "25:00"}, 400), ({"cadence": "hourly"}, 400), ({"cadence": "weekly"}, 400),
                         ({"date": "tomorrow"}, 400), ({"prompt": "   "}, 400), ({"project_id": 9999}, 404),
                         ({"enabled": None}, 400), ({"time": None}, 400)):
            with self.assertRaises(HTTPException, msg=str(kw)) as ctx:
                self.edit(sc, **kw)
            self.assertEqual(ctx.exception.status_code, code, kw)
        self.assertEqual(db.get_schedule(sc["id"])["time_of_day"], "09:00")   # nothing half-saved

    def test_missing_schedule(self):
        with self.assertRaises(HTTPException) as ctx:
            api.update_schedule(424242, api.ScheduleUpdateIn(time="10:00"))
        self.assertEqual(ctx.exception.status_code, 404)


class Delete(Base):
    def test_delete(self):
        sc = self.make()
        api.delete_schedule(sc["id"])
        self.assertIsNone(db.get_schedule(sc["id"]))
        self.assertEqual(db.list_schedules(), [])
