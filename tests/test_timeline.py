"""Project calendar and timelines (timeline.py): validation, the alert
schedule, on-track / at-risk / overdue, the "Good morning" briefing, the
worker's pushes, the API and Octopus's timeline commands."""

from datetime import datetime, timedelta
from unittest import mock

from fastapi import HTTPException

import api
import db
import timeline
import worker
from octopus import control
from tests.helpers import NOW, TZ, TestBase, event

# NOW is Thursday 24 Sep 2026, 19:50 IST.
D = lambda n: (NOW.date() + timedelta(days=n)).isoformat()   # noqa: E731
AT = lambda n, h=10, m=0: datetime.combine(NOW.date() + timedelta(days=n), datetime.min.time(), TZ).replace(hour=h, minute=m)  # noqa: E731


class Base(TestBase):
    def setUp(self):
        super().setUp()
        self.shop = db.add_project("shop", self._tmp.name + "/shop")
        self.pid = self.shop["id"]
        from assistant import timeutil
        clock = mock.patch.object(timeutil, "now", return_value=NOW)       # what the API and Octopus see as now
        clock.start()
        self.addCleanup(clock.stop)

    def add(self, kind="deadline", title="Launch", day=3, time_=None, remind=None, pid=None):
        return timeline.create(pid or self.pid, kind, title, D(day), time_, remind=remind, now=NOW)


class Validation(Base):
    def test_rejects_bad_input(self):
        for kwargs, msg in (({"kind": "party"}, "kind"), ({"date_": "2026-13-01"}, "real date"), ({"date_": "tomorrow"}, "YYYY-MM-DD"),
                            ({"time_": "25:00"}, "HH:MM"), ({"remind": [90]}, "between"), ({"title": "  "}, "title is required")):
            args = {"project_id": self.pid, "kind": "deadline", "title": "x", "date_": D(1), **kwargs}
            with self.assertRaises(timeline.TimelineError) as cm:
                timeline.create(**args, now=NOW)
            self.assertIn(msg, str(cm.exception))

    def test_unknown_project_and_item(self):
        with self.assertRaisesRegex(timeline.TimelineError, "not found"):
            timeline.create(9999, "deadline", "x", D(1), now=NOW)
        with self.assertRaisesRegex(timeline.TimelineError, "not found"):
            timeline.update(9999, {"title": "x"})

    def test_title_whitespace_is_collapsed_and_remind_days_sorted(self):
        ev = timeline.create(self.pid, "deadline", "  Ship   v2 ", D(10), remind=[1, 5, 1], now=NOW)
        self.assertEqual((ev["title"], ev["remind_days"]), ("Ship v2", [5, 1]))


class Alerts(Base):
    def keys(self, now):
        return [(a["event"]["title"], a["key"]) for a in timeline.pending_alerts(now)]

    def test_reminders_already_inside_of_are_marked_sent_on_creation(self):
        ev = self.add(day=2)                          # deadline defaults: 7, 3, 1, 0 days ahead
        self.assertEqual(sorted(ev["alerts_sent"]), ["d3", "d7"])
        self.assertEqual(self.keys(NOW), [])

    def test_the_next_reminder_goes_out_once(self):
        self.add(day=2)
        tomorrow = NOW + timedelta(days=1)
        self.assertEqual(self.keys(tomorrow), [("Launch", "d1")])
        for a in timeline.pending_alerts(tomorrow):
            timeline.mark_alerted(a["event"], a["covered"] + [a["key"]])
        self.assertEqual(self.keys(tomorrow), [])
        self.assertEqual(self.keys(NOW + timedelta(days=2)), [("Launch", "d0")])

    def test_a_missed_reminder_is_not_sent_late(self):
        self.add(day=5)                              # d7 covered; d3 due in two days
        # The Mac slept through "in 3 days"; by the time it wakes, only "tomorrow" goes out.
        a = timeline.pending_alerts(NOW + timedelta(days=4))
        self.assertEqual([x["key"] for x in a], ["d1"])
        self.assertIn("d3", a[0]["covered"])

    def test_overdue_alert_once_with_what_is_still_open(self):
        self.add(day=1, title="Launch")
        db.add_todo(self.pid, "Fix checkout")
        later = NOW + timedelta(days=2)
        a = timeline.pending_alerts(later)
        self.assertEqual([x["key"] for x in a], ["overdue"])
        self.assertIn("Overdue", a[0]["title"])
        self.assertIn("1 backlog item not started", a[0]["message"])

    def test_meetings_get_an_hour_warning_and_never_go_overdue(self):
        ev = self.add(kind="meeting", title="Client sync", day=1, time_="10:00")
        self.assertEqual(self.keys(AT(1, 8, 30)), [("Client sync", "d0")])
        timeline.mark_alerted(ev, ["d1", "d0"])
        self.assertEqual(self.keys(AT(1, 9, 15)), [("Client sync", "soon")])
        ev = db.get_project_event(ev["id"])
        self.assertFalse(timeline.is_overdue(ev, AT(3)))
        self.assertEqual(self.keys(AT(1, 11)), [])

    def test_done_items_stop_alerting(self):
        ev = self.add(day=1)
        timeline.update(ev["id"], {"status": "done"}, now=NOW)
        self.assertEqual(self.keys(NOW + timedelta(days=3)), [])
        self.assertIsNotNone(db.get_project_event(ev["id"])["done_at"])

    def test_redating_starts_the_alerts_over(self):
        ev = self.add(day=2)
        moved = timeline.update(ev["id"], {"date": D(20)}, now=NOW)
        self.assertEqual(moved["alerts_sent"], [])
        self.assertEqual(self.keys(NOW + timedelta(days=13)), [("Launch", "d7")])

    def test_custom_reminders(self):
        self.add(day=20, remind=[14])
        self.assertEqual(self.keys(NOW + timedelta(days=5)), [])
        [a] = timeline.pending_alerts(NOW + timedelta(days=6))
        self.assertEqual(a["key"], "d14")
        timeline.mark_alerted(a["event"], [a["key"]])
        self.assertEqual(self.keys(NOW + timedelta(days=19)), [])            # no 7 / 3 / 1 / 0 day ones


class Health(Base):
    def test_no_timeline(self):
        self.assertEqual(timeline.health(self.pid, NOW)["state"], "none")

    def test_on_track_at_risk_overdue_done(self):
        ev = self.add(day=10)
        self.assertEqual(timeline.health(self.pid, NOW)["state"], "on_track")
        db.add_todo(self.pid, "Write docs")
        self.assertEqual(timeline.health(self.pid, NOW)["state"], "on_track")        # far enough away
        self.assertEqual(timeline.health(self.pid, NOW + timedelta(days=8))["state"], "at_risk")
        h = timeline.health(self.pid, NOW + timedelta(days=11))
        self.assertEqual((h["state"], h["overdue"]), ("overdue", 1))
        timeline.update(ev["id"], {"status": "done"}, now=NOW)
        self.assertEqual(timeline.health(self.pid, NOW)["state"], "done")

    def test_earlier_open_items_count_as_outstanding(self):
        self.add(kind="task", title="Design review", day=1)
        demo = self.add(kind="demo", title="Client demo", day=2)
        out = timeline.outstanding(demo)
        self.assertEqual([i["title"] for i in out["items"]], ["Design review"])
        self.assertEqual(timeline.health(self.pid, NOW)["state"], "at_risk")

    def test_view_countdown(self):
        v = timeline.view(self.add(day=1, time_="15:00"), NOW)
        self.assertEqual((v["days_left"], v["kind_label"], v["overdue"]), (1, "Deadline", False))
        self.assertTrue(v["when"].startswith("tomorrow at 3:00"))


class Prompt(Base):
    def test_models_get_the_dates_with_overdue_marked(self):
        self.assertEqual(timeline.prompt_for(self.pid, NOW), "")
        self.add(title="Beta", day=-2)
        self.add(kind="milestone", title="GA", day=5)
        text = timeline.prompt_for(self.pid, NOW)
        self.assertIn("OVERDUE, was due", text)
        self.assertIn("Milestone - GA", text)
        self.assertLess(text.index("Beta"), text.index("GA"))


class Briefing(Base):
    def test_everything_for_the_day(self):
        self.add(title="Old", day=-1)
        self.add(kind="presentation", title="Pitch", day=0, time_="16:00")
        self.add(kind="milestone", title="Beta", day=4)
        self.add(title="Far off", day=30)
        task = db.add_task(self.pid, "deploy it", 0, None)
        db.create_approval(task["id"], "Bash", '{"command": "git push"}')
        cal = lambda now: ([{"id": "c1", "title": "Standup", "start": AT(0, 21).isoformat(), "end": AT(0, 21, 15).isoformat(),  # noqa: E731
                             "all_day": False, "location": None, "link": None}], None)
        b = timeline.briefing(NOW, calendar_fn=cal)
        self.assertEqual([e["title"] for e in b["overdue"]], ["Old"])
        self.assertEqual([e["title"] for e in b["today"]], ["Pitch"])
        self.assertEqual([e["title"] for e in b["upcoming"]], ["Beta"])
        self.assertEqual(len(b["approvals"]), 1)
        self.assertTrue(b["calendar_connected"])
        self.assertEqual(b["greeting"], "Good evening")
        self.assertEqual(b["headline"], "1 overdue · 2 today · 1 coming up this week · 1 approval waiting")
        for part in ("Overdue:", "Today:", "Standup (calendar)", "Coming up this week:", "Needs you: 1 approval"):
            self.assertIn(part, b["text"])
        self.assertIn("Today you have 1 project item and 1 meeting.", b["spoken"])
        self.assertEqual([r["name"] for r in b["at_risk"]], ["shop"])

    def test_a_clear_day_and_no_calendar(self):
        b = timeline.briefing(NOW.replace(hour=8), calendar=False)
        self.assertEqual((b["greeting"], b["calendar_connected"]), ("Good morning", False))
        self.assertEqual(b["headline"], "Nothing urgent - a clear day.")
        self.assertIn("A clear day.", b["text"])

    def test_a_calendar_error_still_gives_the_briefing(self):
        b = timeline.briefing(NOW, calendar_fn=lambda now: ([], "token expired"))
        self.assertIn("Couldn't read your calendar: token expired", b["text"])


class WorkerPushes(Base):
    def setUp(self):
        super().setUp()
        self.push = mock.patch.object(worker, "notify_phone", return_value=True).start()
        mock.patch.object(worker, "in_quiet_hours", return_value=False).start()
        self.addCleanup(mock.patch.stopall)

    def test_timeline_alert_is_pushed_once_and_opens_the_timeline(self):
        self.add(day=2)
        later = NOW + timedelta(days=1)
        self.assertEqual(worker.check_timeline_alerts(later), 1)
        self.assertEqual(worker.check_timeline_alerts(later), 0)
        self.assertEqual(self.push.call_args.kwargs["open_hash"], f"#/project/{self.pid}/timeline")

    def test_setting_off_or_quiet_hours_hold_alerts(self):
        self.add(day=2)
        later = NOW + timedelta(days=1)
        db.set_setting("notify_timeline", False)
        self.assertEqual(worker.check_timeline_alerts(later), 0)
        db.set_setting("notify_timeline", True)
        with mock.patch.object(worker, "in_quiet_hours", return_value=True):
            self.assertEqual(worker.check_timeline_alerts(later), 0)
        self.assertEqual(worker.check_timeline_alerts(later), 1)       # held, not dropped

    def test_morning_briefing_once_a_day_at_its_time(self):
        with mock.patch.object(timeline, "_calendar_today", return_value=(None, None)):
            self.assertFalse(worker.send_morning_briefing(AT(1, 8, 0)))     # before 08:30
            self.assertTrue(worker.send_morning_briefing(AT(1, 8, 45)))
            self.assertFalse(worker.send_morning_briefing(AT(1, 9, 30)))    # already sent today
            self.assertFalse(worker.send_morning_briefing(AT(2, 13, 0)))    # woke up too late to be useful
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.push.call_args.kwargs["open_hash"], "#/briefing")


class Endpoints(Base):
    def test_add_list_edit_delete(self):
        ev = api.add_timeline_event(self.pid, api.TimelineEventIn(kind="demo", title="Client demo", date=D(3), time="15:00"))
        self.assertEqual((ev["kind_label"], ev["project_name"], ev["days_left"]), ("Demo", "shop", 3))
        got = api.list_timeline(start=D(0), end=D(7))
        self.assertEqual([e["id"] for e in got["events"]], [ev["id"]])
        self.assertIn(self.pid, got["health"])
        self.assertNotIn(self.project["id"], got["health"])            # General has no timeline
        self.assertEqual(api.list_timeline(start=D(4))["events"], [])
        changed = api.update_timeline_event(ev["id"], api.TimelineEventUpdateIn(title="Final demo", time=None))
        self.assertEqual((changed["title"], changed["time"]), ("Final demo", None))
        done = api.update_timeline_event(ev["id"], api.TimelineEventUpdateIn(status="done"))
        self.assertEqual(done["status"], "done")
        self.assertEqual(api.list_timeline(status="open")["events"], [])
        self.assertEqual(len(api.project_timeline(self.pid)["events"]), 1)
        api.delete_timeline_event(ev["id"])
        self.assertEqual(api.project_timeline(self.pid)["events"], [])

    def test_errors(self):
        for call, code in ((lambda: api.add_timeline_event(self.pid, api.TimelineEventIn(title="x", date="soon")), 422),
                           (lambda: api.add_timeline_event(9999, api.TimelineEventIn(title="x", date=D(1))), 404),
                           (lambda: api.update_timeline_event(9999, api.TimelineEventUpdateIn(title="x")), 404),
                           (lambda: api.delete_timeline_event(9999), 404),
                           (lambda: api.project_timeline(9999), 404)):
            with self.assertRaises(HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, code)

    def test_briefing_without_calendar(self):
        b = api.get_briefing(calendar=False)
        self.assertIn("headline", b)
        self.assertFalse(b["calendar_connected"])

    def test_settings_accept_briefing_time(self):
        s = api.update_settings(api.SettingsIn(briefing_time="07:15", briefing_push=False))
        self.assertEqual((s["briefing_time"], s["briefing_push"]), ("07:15", False))


class OctopusCommands(Base):
    def test_matching(self):
        self.assertEqual(control.match("Good morning")["action"], "briefing")
        self.assertEqual(control.match("what deadlines are coming up?")["action"], "timeline_show")
        self.assertEqual(control.match("show the timeline for shop")["project_name"], "shop")
        found = control.match("Schedule a client demo for shop on October 3 at 3pm")
        self.assertEqual((found["action"], found["kind"], found["title"], found["date"], found["time"]),
                         ("timeline_add", "demo", "Client demo", "2026-10-03", "15:00"))
        self.assertIsNone(control.match("schedule a meeting with Rahul on my calendar tomorrow"))
        self.assertIsNone(control.match("schedule a demo for someproject tomorrow"))

    def test_adding_and_showing(self):
        found = control.match("add a deadline for shop on October 1: ship the checkout")
        out = control.run(found)
        self.assertTrue(out["ok"])
        self.assertIn("Ship the checkout", out["text"])
        evs = db.list_project_events(self.pid)
        self.assertEqual((evs[0]["kind"], evs[0]["date"], evs[0]["source"]), ("deadline", "2026-10-01", "octopus"))
        self.assertIn("Ship the checkout", control.run(control.match("upcoming deadlines"))["text"])
