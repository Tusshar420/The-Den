"""Sleep mode ("I'm going to sleep"): the queue keeps going and makes full use of the limits."""

from datetime import datetime, timedelta, timezone

import api
import db
import worker
from tests.helpers import TestBase


def five_hour_at(util):
    resets = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    db.set_setting("claude_usage", {"windows": {"five_hour": {"utilization": util, "resets_at": resets}}})


class SleepMode(TestBase):
    def test_off_by_default_and_toggled_from_the_api(self):
        self.assertFalse(db.sleep_mode_active())
        view = api.start_sleep(api.SleepIn(hours=6))
        self.assertTrue(db.sleep_mode_active())
        self.assertEqual(view["sleep_mode"], db.get_setting("sleep_mode"))
        span = datetime.fromisoformat(view["sleep_mode"]["until"]) - datetime.fromisoformat(view["sleep_mode"]["since"])
        self.assertEqual(span, timedelta(hours=6))
        self.assertIsNone(api.end_sleep()["sleep_mode"])
        self.assertFalse(db.sleep_mode_active())

    def test_ends_by_itself(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.set_setting("sleep_mode", {"since": (past - timedelta(hours=8)).isoformat(), "until": past.isoformat()})
        self.assertFalse(db.sleep_mode_active())

    def test_low_priority_work_is_no_longer_held_back(self):
        five_hour_at(0.95)
        self.assertTrue(worker._hold_low_priority())
        db.start_sleep_mode()
        self.assertFalse(worker._hold_low_priority())
        db.end_sleep_mode()
        self.assertTrue(worker._hold_low_priority())

    def test_rate_limit_without_a_reset_time_is_retried_soon_and_flat(self):
        def wait(task_id):
            return (worker._next_backoff(task_id) - datetime.now(timezone.utc)).total_seconds() / 60
        self.assertAlmostEqual(wait(1), worker.DEFAULT_RATE_LIMIT_BACKOFF_MINUTES, delta=1)
        worker._backoff_minutes.pop(1, None)
        db.start_sleep_mode()
        for _ in range(3):  # never doubles
            self.assertAlmostEqual(wait(1), worker.SLEEP_RATE_LIMIT_BACKOFF_MINUTES, delta=1)
