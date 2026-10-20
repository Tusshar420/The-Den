"""Project phases and autopilot (phases.py): validation, one-todo-at-a-time feeding, stopping on a
failure, starting phases by date or by usage room, the API and the prompt context."""

from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi import HTTPException

from tests.helpers import NOW, TestBase

import api
import db
import phases
import worker

D = lambda n: (NOW.date() + timedelta(days=n)).isoformat()   # noqa: E731


class Base(TestBase):
    def setUp(self):
        super().setUp()
        self.pid = db.add_project("shop", self._tmp.name + "/shop")["id"]
        from assistant import timeutil
        clock = mock.patch.object(timeutil, "now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def phase(self, name="Build", todos=("a", "b"), **kw):
        return phases.create(self.pid, name, todos=list(todos), **kw)

    def todo_states(self, phase):
        return [phases.todo_state(t) for t in db.list_todos(self.pid) if t["phase_id"] == phase["id"]]


class Validation(Base):
    def test_rejects_bad_input(self):
        for kwargs, msg in (({"name": "  "}, "name is required"), ({"start_date": "soon"}, "YYYY-MM-DD"),
                            ({"end_date": "2026-13-40"}, "real date"),
                            ({"start_date": D(5), "end_date": D(2)}, "before it starts")):
            args = {"project_id": self.pid, "name": "x", **kwargs}
            with self.assertRaises(phases.PhaseError) as cm:
                phases.create(**args)
            self.assertIn(msg, str(cm.exception))

    def test_creates_in_order_with_todos(self):
        a, b = self.phase("One"), self.phase("Two", todos=("x",))
        self.assertEqual([p["name"] for p in db.list_phases(self.pid)], ["One", "Two"])
        self.assertEqual(len(phases.view(a)["todo_ids"]), 2)
        self.assertEqual(phases.view(b)["total"], 1)

    def test_todo_cannot_join_another_projects_phase(self):
        other = db.add_project("blog", self._tmp.name + "/blog")["id"]
        ph = phases.create(other, "Writing")
        todo = db.add_todo(self.pid, "x")
        with self.assertRaisesRegex(phases.PhaseError, "different project"):
            phases.assign_todo(todo["id"], ph["id"])

    def test_deleting_a_phase_keeps_its_todos(self):
        ph = self.phase()
        db.delete_phase(ph["id"])
        self.assertEqual(len(db.list_todos(self.pid)), 2)
        self.assertTrue(all(t["phase_id"] is None for t in db.list_todos(self.pid)))


class Feeding(Base):
    def test_start_queues_only_the_first_todo(self):
        ph = self.phase(todos=("a", "b", "c"))
        phases.start(ph["id"], NOW)
        self.assertEqual(self.todo_states(ph), ["queued", "pending", "pending"])
        self.assertEqual(db.get_phase(ph["id"])["status"], "active")

    def test_tick_waits_while_one_is_in_flight_then_queues_the_next(self):
        ph = self.phase(todos=("a", "b"))
        phases.start(ph["id"], NOW)
        phases.tick(NOW)
        self.assertEqual(self.todo_states(ph), ["queued", "pending"])
        first = db.list_todos(self.pid)[0]["task_id"]
        db.mark_running(first)
        db.mark_done(first, "ok")
        phases.tick(NOW)
        self.assertEqual(self.todo_states(ph), ["done", "queued"])

    def test_finishing_the_last_todo_closes_the_phase_and_alerts_once(self):
        ph = self.phase(todos=("a",))
        phases.start(ph["id"], NOW)
        tid = db.list_todos(self.pid)[0]["task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "ok")
        alerts = phases.tick(NOW)
        self.assertEqual(db.get_phase(ph["id"])["status"], "done")
        self.assertEqual([a["key"] for a in alerts], ["done"])
        self.assertEqual(phases.tick(NOW), [])

    def test_a_failed_todo_stops_the_phase_and_pushes_once(self):
        ph = self.phase(todos=("a", "b"))
        phases.start(ph["id"], NOW)
        tid = db.list_todos(self.pid)[0]["task_id"]
        db.mark_running(tid)
        db.mark_error(tid, "boom")
        first = phases.tick(NOW)
        self.assertEqual(len(first), 1)
        self.assertEqual(phases.tick(NOW), [])
        self.assertEqual(self.todo_states(ph)[1], "pending")           # b was not queued
        self.assertIn("needs you", db.get_phase(ph["id"])["blocked_note"])
        self.assertEqual(phases.view(db.get_phase(ph["id"]))["failed"], 1)

    def test_start_with_nothing_to_do_is_refused(self):
        ph = phases.create(self.pid, "Empty")
        with self.assertRaisesRegex(phases.PhaseError, "nothing to do"):
            phases.start(ph["id"], NOW)

    def test_starting_another_phase_pauses_the_active_one(self):
        a, b = self.phase("A"), self.phase("B")
        phases.start(a["id"], NOW)
        phases.start(b["id"], NOW)
        self.assertEqual([p["status"] for p in db.list_phases(self.pid)], ["paused", "active"])

    def test_paused_phase_is_not_fed(self):
        ph = self.phase(todos=("a", "b"))
        phases.start(ph["id"], NOW)
        phases.pause(ph["id"])
        tid = db.list_todos(self.pid)[0]["task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "ok")
        phases.tick(NOW)
        self.assertEqual(self.todo_states(ph), ["done", "pending"])

    def test_on_hold_project_is_not_fed(self):
        ph = self.phase(todos=("a", "b"))
        db.update_phase(ph["id"], status="active")
        db.set_project_status(self.pid, "on_hold")
        phases.tick(NOW)
        self.assertEqual(self.todo_states(ph), ["pending", "pending"])

    def test_late_phase_alerts_once(self):
        ph = self.phase(todos=("a", "b"), end_date=D(-1))
        db.update_phase(ph["id"], status="active")
        keys = [a["key"] for a in phases.tick(NOW)]
        self.assertIn("late", keys)
        self.assertNotIn("late", [a["key"] for a in phases.tick(NOW)])


class Autopilot(Base):
    def setMode(self, mode, ceiling=None):
        phases.set_mode(self.pid, mode, ceiling)

    def test_manual_never_starts_a_phase_by_itself(self):
        self.phase()
        self.setMode("manual")
        phases.tick(NOW)
        self.assertEqual(db.list_phases(self.pid)[0]["status"], "planned")

    def test_timeline_starts_on_the_start_date_in_order(self):
        a = self.phase("A", start_date=D(2))
        b = self.phase("B", start_date=D(-1))
        self.setMode("timeline")
        phases.tick(NOW)
        # A is first in order and not due yet, so B (due) waits behind it.
        self.assertEqual([p["status"] for p in db.list_phases(self.pid)], ["planned", "planned"])
        later = NOW + timedelta(days=3)
        with mock.patch.object(phases, "_today", return_value=later.date()):
            alerts = phases.tick(later)
        self.assertEqual(db.get_phase(a["id"])["status"], "active")
        self.assertEqual(db.get_phase(b["id"])["status"], "planned")
        self.assertEqual(alerts[0]["key"], "started")
        self.assertEqual(self.todo_states(a), ["queued", "pending"])

    def test_capacity_runs_phases_back_to_back_ignoring_dates(self):
        a = self.phase("A", todos=("x",), start_date=D(30))
        b = self.phase("B", todos=("y",))
        self.setMode("capacity")
        phases.tick(NOW)
        self.assertEqual(db.get_phase(a["id"])["status"], "active")
        tid = db.list_todos(self.pid)[0]["task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "ok")
        phases.tick(NOW)
        self.assertEqual(db.get_phase(a["id"])["status"], "done")
        self.assertEqual(db.get_phase(b["id"])["status"], "active")

    def usage(self, weekly=None, five=None):
        resets = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        windows = {}
        if weekly is not None:
            windows["seven_day"] = {"utilization": weekly, "resets_at": resets}
        if five is not None:
            windows["five_hour"] = {"utilization": five, "resets_at": resets}
        import accounts
        db.set_setting("claude_usage_by_account", {accounts.for_project(self.pid): {"windows": windows}})

    def test_ceiling_holds_auto_work_but_not_a_command(self):
        ph = self.phase(todos=("a", "b"))
        self.setMode("capacity", ceiling=80)
        self.usage(weekly=0.9)
        cap = phases.capacity(self.pid)
        self.assertFalse(cap["ok"])
        self.assertIn("90%", cap["reason"])
        phases.tick(NOW)
        self.assertEqual(db.get_phase(ph["id"])["status"], "planned")        # not started: no room
        phases.start(ph["id"], NOW)                                          # you said go
        self.assertEqual(self.todo_states(ph), ["queued", "pending"])
        tid = db.list_todos(self.pid)[0]["task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "ok")
        phases.tick(NOW)
        self.assertEqual(self.todo_states(ph), ["done", "pending"])           # auto-feeding waits
        self.assertIn("Waiting", db.get_phase(ph["id"])["blocked_note"])
        self.usage(weekly=0.5)
        phases.tick(NOW)
        self.assertEqual(self.todo_states(ph), ["done", "queued"])
        self.assertIsNone(db.get_phase(ph["id"])["blocked_note"])

    def test_five_hour_window_and_unknown_usage(self):
        self.usage(five=0.95)
        self.assertFalse(phases.capacity(self.pid)["ok"])
        self.usage()
        self.assertTrue(phases.capacity(self.pid)["ok"])

    def test_a_reset_window_no_longer_counts(self):
        import accounts
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.set_setting("claude_usage_by_account", {accounts.for_project(self.pid): {
            "windows": {"seven_day": {"utilization": 0.99, "resets_at": past}}}})
        self.assertTrue(phases.capacity(self.pid)["ok"])

    def test_ceiling_is_validated(self):
        for bad in (0, 5, 101, "x"):
            with self.assertRaises(phases.PhaseError):
                phases.clean_ceiling(bad)


class Api(Base):
    def test_phase_round_trip_through_the_api(self):
        made = api.add_phase(self.pid, api.PhaseIn(name="Design", goal="Wireframes", todos=["sketch", "review"]))
        self.assertEqual(made["total"], 2)
        view = api.project_phases(self.pid)
        self.assertEqual(view["autopilot"]["mode"], "manual")
        started = api.start_phase(made["id"])
        self.assertEqual(started["status"], "active")
        self.assertEqual(started["counts"]["queued"], 1)
        api.pause_phase(made["id"])
        api.update_phase(made["id"], api.PhaseUpdateIn(name="Design v2"))
        self.assertEqual(db.get_phase(made["id"])["name"], "Design v2")
        api.delete_phase(made["id"])
        self.assertEqual(api.project_phases(self.pid)["phases"], [])

    def test_errors_map_to_http(self):
        with self.assertRaises(HTTPException) as cm:
            api.add_phase(self.pid, api.PhaseIn(name=" "))
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            api.start_phase(9999)
        self.assertEqual(cm.exception.status_code, 404)

    def test_autopilot_setting_is_validated_and_saved(self):
        api.update_project_settings(self.pid, api.ProjectSettingsIn(autopilot="timeline", autopilot_ceiling=70))
        s = db.get_project_settings(self.pid)
        self.assertEqual((s["autopilot"], s["autopilot_ceiling"]), ("timeline", 70))
        with self.assertRaises(HTTPException):
            api.update_project_settings(self.pid, api.ProjectSettingsIn(autopilot="turbo"))

    def test_existing_run_selected_still_queues_checked_todos(self):
        t = db.add_todo(self.pid, "do it")
        db.update_todo(t["id"], checked=True)
        out = api.run_checked_todos(self.pid)
        self.assertEqual(out[0]["status"], "queued")

    def test_worker_pushes_alerts_from_the_tick(self):
        ph = self.phase_for_worker()
        with mock.patch.object(worker, "notify_phone") as push:
            worker.run_phase_autopilot(NOW)
        self.assertEqual(push.call_count, 1)
        self.assertIn("Phase started", push.call_args[0][0])
        self.assertEqual(db.get_phase(ph["id"])["status"], "active")

    def phase_for_worker(self):
        ph = phases.create(self.pid, "Go", todos=["a"])
        phases.set_mode(self.pid, "capacity")
        return ph


class Report(Base):
    def setUp(self):
        super().setUp()
        self.a = self.phase("Design", todos=("sketch the cart", "pick fonts"), goal="Wireframes **done**", end_date=D(-2))
        self.b = self.phase("Build", todos=("checkout",))
        db.add_todo(self.pid, "loose idea")
        phases.start(self.a["id"], NOW)
        tid = db.list_todos(self.pid)[0]["task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "Drew the **cart** page.\n\nMore detail here.")
        no_model = mock.patch("router.cheapest_api_model", return_value=None)
        no_model.start()
        self.addCleanup(no_model.stop)

    def test_whole_project_lists_every_phase_and_what_was_done(self):
        r = phases.report(self.pid, now=NOW)
        md = r["markdown"]
        self.assertEqual(r["title"], "shop · progress report")
        for part in ("## At a glance", "- Items: 1 of 3 done", "## 1. Design · In progress", "## 2. Build · Planned",
                     "- sketch the cart — Drew the cart page.", "### Still to do\n- pick fonts", "2 days late",
                     "**Goal:** Wireframes done", "## Not in a phase yet\n- loose idea", "- Autopilot: on your command"):
            self.assertIn(part, md)
        self.assertNotIn("More detail", md)
        self.assertIsNone(r["overview"])
        self.assertIn("No model", r["overview_note"])
        self.assertTrue(r["filename"].endswith(".md"))

    def test_some_phases_only_keep_their_numbers(self):
        md = phases.report(self.pid, [self.b["id"]], now=NOW)["markdown"]
        self.assertIn("## 2. Build", md)
        self.assertNotIn("Design", md)
        self.assertNotIn("Not in a phase", md)
        self.assertNotIn("Autopilot", md)

    def test_overview_from_a_model_goes_on_top(self):
        model = {"id": "cheap", "provider": "openai"}
        with mock.patch("router.cheapest_api_model", return_value=model), \
                mock.patch("providers.complete", return_value=mock.Mock(text="You are **half** way.", input_tokens=5, output_tokens=5)):
            r = phases.report(self.pid, now=NOW)
        self.assertEqual(r["overview"], "You are half way.")
        self.assertLess(r["markdown"].index("## Overview"), r["markdown"].index("## At a glance"))

    def test_a_failed_overview_still_returns_the_report(self):
        import providers
        with mock.patch("router.cheapest_api_model", return_value={"id": "cheap", "provider": "openai"}), \
                mock.patch("providers.complete", side_effect=providers.ProviderError("boom")):
            r = phases.report(self.pid, now=NOW)
        self.assertIn("boom", r["overview_note"])
        self.assertIn("## 1. Design", r["markdown"])

    def test_api_rejects_another_projects_phase(self):
        other = phases.create(db.add_project("blog", self._tmp.name + "/blog")["id"], "Writing")
        with self.assertRaises(HTTPException) as cm:
            api.project_report(self.pid, api.ReportIn(phase_ids=[other["id"]]))
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            api.project_report(9999)
        self.assertEqual(cm.exception.status_code, 404)
        self.assertIn("## 1. Design", api.project_report(self.pid, api.ReportIn(overview=False))["markdown"])


class PromptContext(Base):
    def test_prompt_names_the_active_phase_and_its_goal(self):
        self.assertEqual(phases.prompt_for(self.pid), "")
        a = self.phase("Design", goal="Wireframes only")
        self.phase("Build", start_date=D(3), end_date=D(9))
        db.update_phase(a["id"], status="active")
        text = phases.prompt_for(self.pid)
        self.assertIn("Design [IN PROGRESS]: Wireframes only", text)
        self.assertIn("Build [planned]", text)
        self.assertIn("working in the phase 'Design'", text)


class OctopusCommands(Base):
    def setUp(self):
        super().setUp()
        self.ph = [self.phase("Design", todos=("sketch",)), self.phase("Build", todos=("code", "test"))]

    def say(self, text, thread_project_id=None):
        from octopus import control
        found = control.match(text)
        self.assertIsNotNone(found, text)
        return control.run(found, thread_project_id)

    def test_start_by_number_name_ordinal_and_next(self):
        out = self.say("start phase 2 of shop")
        self.assertTrue(out["ok"])
        self.assertEqual(db.get_phase(self.ph[1]["id"])["status"], "active")
        self.say("Octopus, start the Design phase of shop please")
        self.assertEqual(db.get_phase(self.ph[0]["id"])["status"], "active")
        self.assertEqual(db.get_phase(self.ph[1]["id"])["status"], "paused")
        self.say("start the second phase of shop")
        self.assertEqual(db.get_phase(self.ph[1]["id"])["status"], "active")
        self.say("start the next phase for shop")
        self.assertEqual(db.get_phase(self.ph[0]["id"])["status"], "active")

    def test_start_without_a_project_uses_the_thread_project_or_asks(self):
        self.assertTrue(self.say("start the build phase", self.pid)["ok"])
        db.add_project("blog", self._tmp.name + "/blog")
        self.assertFalse(self.say("start phase 1")["ok"])

    def test_unknown_phase_lists_the_choices(self):
        out = self.say("start phase 9 of shop")
        self.assertFalse(out["ok"])
        self.assertIn("1. Design, 2. Build", out["text"])

    def test_pause_and_status(self):
        self.say("start phase 1 of shop")
        status = self.say("how are the phases going on shop")
        self.assertIn("1. Design: active", status["text"])
        self.assertTrue(self.say("pause the phase of shop")["ok"])
        self.assertEqual(db.get_phase(self.ph[0]["id"])["status"], "paused")
        self.assertFalse(self.say("pause the phase of shop")["ok"])

    def test_ordinary_sentences_are_left_alone(self):
        from octopus import control
        for text in ("phase out the legacy API", "start phasing out the old login", "we should phase out the cache",
                     "run the tests", "pause my meeting"):
            self.assertIsNone(control.match(text), text)
