import unittest
from datetime import date, time

from tests.helpers import NOW
from assistant import timeutil as tu


class ParseWhen(unittest.TestCase):
    def w(self, text):
        return tu.parse_when(text, NOW)

    def test_tomorrow_and_time(self):
        w = self.w("meeting tomorrow at 3 PM")
        self.assertEqual((w.day, w.start), (date(2026, 9, 25), time(15, 0)))

    def test_between_range_infers_pm(self):
        w = self.w("Am I free between 2 and 5 PM tomorrow?")
        self.assertEqual((w.start, w.end, w.is_range), (time(14, 0), time(17, 0), True))

    def test_range_without_meridiem_uses_office_hours(self):
        w = self.w("between 10 and 2 today")
        self.assertEqual((w.start, w.end), (time(10, 0), time(14, 0)))

    def test_weekday_bare_next_and_this(self):
        # Thursday 24th: bare "friday" is tomorrow, "next friday" is the one in next calendar week
        self.assertEqual(self.w("on friday").day, date(2026, 9, 25))
        self.assertEqual(self.w("next friday").day, date(2026, 10, 2))
        self.assertEqual(self.w("this thursday").day, date(2026, 9, 24))
        self.assertEqual(self.w("thursday").day, date(2026, 10, 1))

    def test_next_week_is_monday_to_monday(self):
        self.assertEqual(self.w("what's on next week").days, (date(2026, 9, 28), date(2026, 10, 5)))

    def test_month_day_forms_and_rollover(self):
        self.assertEqual(self.w("on 2 October").day, date(2026, 10, 2))
        self.assertEqual(self.w("September 30").day, date(2026, 9, 30))
        self.assertEqual(self.w("january 5").day, date(2027, 1, 5))  # already passed this year

    def test_relative(self):
        self.assertEqual(self.w("in 30 minutes").relative.isoformat(), "2026-09-24T20:20:00+05:30")
        self.assertEqual(self.w("in an hour").relative.isoformat(), "2026-09-24T20:50:00+05:30")

    def test_nothing_found(self):
        self.assertFalse(self.w("move it to four").found)

    def test_strip_spans_leaves_the_task_text(self):
        w = self.w("Remind me tomorrow morning to call Rahul")
        self.assertEqual(tu.strip_spans("Remind me tomorrow morning to call Rahul", w.spans), "Remind me to call Rahul")


class Resolved(unittest.TestCase):
    def test_calendar_window_day_point_range_week(self):
        s, e, label = tu.calendar_window(tu.parse_when("tomorrow", NOW), NOW)
        self.assertEqual((s.isoformat(), e.isoformat()), ("2026-09-25T00:00:00+05:30", "2026-09-26T00:00:00+05:30"))
        s, e, _ = tu.calendar_window(tu.parse_when("at 3 PM", NOW), NOW)
        self.assertEqual((s.hour, (e - s).seconds), (15, 60))
        s, e, _ = tu.calendar_window(tu.parse_when("next week", NOW), NOW)
        self.assertEqual((s.date(), e.date()), (date(2026, 9, 28), date(2026, 10, 5)))

    def test_no_date_means_today(self):
        s, _, label = tu.calendar_window(tu.parse_when("what's on my calendar", NOW), NOW)
        self.assertEqual((s.date(), label), (date(2026, 9, 24), "today"))

    def test_reminder_time(self):
        rt = lambda t: tu.reminder_time(tu.parse_when(t, NOW), NOW)
        self.assertEqual(rt("tomorrow morning").isoformat(), "2026-09-25T09:00:00+05:30")
        self.assertEqual(rt("tomorrow at 3").isoformat(), "2026-09-25T15:00:00+05:30")
        self.assertEqual(rt("at 8").isoformat(), "2026-09-24T20:00:00+05:30", "'at 8' at 7:50pm means tonight")
        self.assertEqual(rt("at 5 pm").isoformat(), "2026-09-25T17:00:00+05:30", "5pm today has passed")
        self.assertIsNone(rt("next week"), "a week is not a moment")
        self.assertIsNone(rt("sometime"))

    def test_display_helpers(self):
        self.assertEqual(tu.fmt_time(NOW), "7:50 PM")
        self.assertEqual(tu.fmt_day(NOW), "Thu Sep 24")
        self.assertEqual(tu.fmt_long_date(NOW), "September 24, 2026")


if __name__ == "__main__":
    unittest.main()
