"""Weekly limit early warning: judged on pace, alerted once per level per week."""

from datetime import datetime, timedelta, timezone
from unittest import mock

import db
import worker
from tests.helpers import TestBase

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def usage(util, days_in, now=NOW):
    """A claude_usage snapshot: `util` (0-1) used, `days_in` days into the 7-day window."""
    resets = now + timedelta(days=7 - days_in)
    return {"windows": {"seven_day": {"utilization": util, "resets_at": resets.isoformat()}}}


class WeeklyPace(TestBase):
    def level(self, util, days_in):
        return worker.weekly_pace(usage(util, days_in), NOW)["level"]

    def test_no_reading_or_reset_window_is_none(self):
        self.assertIsNone(worker.weekly_pace(None, NOW))
        self.assertIsNone(worker.weekly_pace({"windows": {}}, NOW))
        past = {"windows": {"seven_day": {"utilization": 0.9, "resets_at": (NOW - timedelta(hours=1)).isoformat()}}}
        self.assertIsNone(worker.weekly_pace(past, NOW))

    def test_eighty_percent_after_four_days_warns(self):
        p = worker.weekly_pace(usage(0.80, 4), NOW)
        self.assertEqual(p["level"], "high")
        self.assertEqual((p["pct"], p["elapsed_pct"], p["projected_pct"]), (80, 57, 140))
        self.assertIsNotNone(p["runs_out_at"])

    def test_steady_use_does_not_warn(self):
        self.assertIsNone(self.level(0.50, 4))   # 57% of the week gone, 50% used: ahead of the clock
        self.assertIsNone(self.level(0.71, 5))   # exactly on pace
        self.assertIsNone(self.level(0.10, 1))

    def test_early_week_spike_is_not_judged_on_pace(self):
        self.assertIsNone(self.level(0.20, 0.5))  # 7% of the week gone: too early to project

    def test_pace_level_needs_real_overspend(self):
        self.assertEqual(self.level(0.50, 3), "pace")    # 43% of the week, 50% used -> projected 117%
        self.assertIsNone(self.level(0.20, 0.9))         # fast, but under 25% used

    def test_high_by_pace_below_eighty(self):
        self.assertEqual(self.level(0.60, 3), "high")    # projected 140%

    def test_critical_at_ninety_five_even_late_in_week(self):
        self.assertEqual(self.level(0.96, 6.9), "critical")

    def test_late_week_eighty_is_still_high_but_run_out_is_after_reset(self):
        p = worker.weekly_pace(usage(0.80, 6.9), NOW)
        self.assertEqual(p["level"], "high")
        self.assertIsNone(p["runs_out_at"])  # would only fill after the reset


class WeeklyAlertPush(TestBase):
    def setUp(self):
        super().setUp()
        self.push = mock.patch.object(worker, "notify_phone", return_value=True).start()
        mock.patch.object(worker, "in_quiet_hours", return_value=False).start()
        self.addCleanup(mock.patch.stopall)

    def reading(self, util, days_in):
        db.set_setting("claude_usage", usage(util, days_in))

    def test_sends_once_then_only_on_escalation(self):
        self.reading(0.50, 3)
        self.assertTrue(worker.check_weekly_pace(NOW))
        self.assertFalse(worker.check_weekly_pace(NOW))          # same level: quiet
        self.reading(0.60, 3)
        self.assertTrue(worker.check_weekly_pace(NOW))            # pace -> high
        self.reading(0.55, 3)
        self.assertFalse(worker.check_weekly_pace(NOW))           # never step back down
        self.assertEqual(self.push.call_count, 2)

    def test_new_week_alerts_again(self):
        self.reading(0.80, 4)
        self.assertTrue(worker.check_weekly_pace(NOW))
        later = NOW + timedelta(days=7)
        db.set_setting("claude_usage", usage(0.80, 4, later))
        self.assertTrue(worker.check_weekly_pace(later))

    def test_quiet_hours_hold_it_back_without_marking_sent(self):
        self.reading(0.80, 4)
        with mock.patch.object(worker, "in_quiet_hours", return_value=True):
            self.assertFalse(worker.check_weekly_pace(NOW))
        self.push.assert_not_called()
        self.assertTrue(worker.check_weekly_pace(NOW))

    def test_failed_push_is_retried(self):
        self.reading(0.80, 4)
        self.push.return_value = False
        self.assertFalse(worker.check_weekly_pace(NOW))
        self.push.return_value = True
        self.assertTrue(worker.check_weekly_pace(NOW))

    def test_setting_off_silences_it(self):
        db.set_setting("notify_weekly_pace", False)
        self.reading(0.90, 4)
        self.assertFalse(worker.check_weekly_pace(NOW))
        self.push.assert_not_called()
