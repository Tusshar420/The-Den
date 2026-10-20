"""The Den 2.0 work pages: review runs (reviewed / changes / revert), hidden tasks with why, and skipping one day of a repeat."""

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_recipes
import den_work
import timeline

ART = {"git": {"branch": "feat/x", "base_branch": "main", "base": "abc123def4567890", "head": "fff000", "commits": ["fff000 Split config"],
               "uncommitted": False},
       "files": [{"path": "src/a.py", "status": "M", "additions": 4, "deletions": 3}, {"path": "src/b.json", "status": "A", "additions": 5, "deletions": 0}],
       "tests": [], "validation": {"status": "passed", "note": "1 test command passed."}}


def _match(pattern, path):
    """{'task_id': 3} when /x/{task_id}/y matches /x/3/y ({} for a plain match), else None."""
    a, b = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(a) != len(b):
        return None
    out = {}
    for x, y in zip(a, b):
        if x.startswith("{"):
            out[x[1:-1]] = int(y)
        elif x != y:
            return None
    return out


class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self.body, self.text = status_code, body, str(body)

    def json(self):
        return self.body


class Work(TestBase):
    def setUp(self):
        super().setUp()
        den_work._ready = False
        self.proj = db.add_project("shop", self._tmp.name)
        self.routes = {(m, r.path): r.endpoint for r in den_work.build_router(lambda: None).routes for m in r.methods}

    def call(self, method, path, **kw):
        """The router's endpoint for path (ids filled in from the path), called directly - no HTTP client needed."""
        key = next(k for k in self.routes if k[0] == method and _match(k[1], path) is not None)
        args = {**_match(key[1], path), **kw}
        try:
            return _Resp(200, self.routes[key](**args))
        except HTTPException as e:
            return _Resp(e.status_code, {"detail": e.detail})

    def run_task(self, prompt="Split the config", thread_id=None, status="done", artifacts=ART):
        t = db.add_task(self.proj["id"], prompt, 0, thread_id)
        db.mark_running(t["id"])
        db.mark_done(t["id"], "Moved the rules into a JSON file.")
        if status == "needs_review":
            with db._lock:
                db.get_conn().execute("UPDATE tasks SET status = 'needs_review' WHERE id = ?", (t["id"],))
                db.get_conn().commit()
        if artifacts is not None:
            db.set_task_artifacts(t["id"], artifacts)
        return db.get_task(t["id"])

    def runs(self):
        return self.call("GET", "/den/work/runs", days=7).json()["runs"]

    # ---------------------------------------------------------------- review runs

    def test_latest_run_per_chat_with_files(self):
        first = self.run_task()
        reply = self.run_task("also fix the tests", thread_id=first["id"])
        self.run_task("just a question", artifacts={"files": []})        # changed nothing: not a review
        runs = self.runs()
        self.assertEqual([(r["id"], r["thread_id"], r["state"]) for r in runs], [(reply["id"], first["id"], "review")])
        r = runs[0]
        self.assertEqual((r["branch"], r["tests"], r["verdict"], r["project_name"]), ("feat/x", "1 test command passed", "passed", "shop"))
        self.assertEqual([(f["path"], f["add"], f["del"]) for f in r["files"]], [("src/a.py", 4, 3), ("src/b.json", 5, 0)])
        self.assertEqual(r["summary"], "Moved the rules into a JSON file.")

    def test_mark_reviewed_and_back(self):
        t = self.run_task(status="needs_review")
        self.assertEqual(self.call("POST", f"/den/work/runs/{t['id']}/reviewed", body=den_work.ReviewedIn(**{"on": True})).status_code, 200)
        self.assertEqual(db.get_task(t["id"])["status"], "done", "needs_review is cleared like POST /tasks/{id}/reviewed")
        self.assertEqual(self.runs()[0]["state"], "reviewed")
        self.call("POST", f"/den/work/runs/{t['id']}/reviewed", body=den_work.ReviewedIn(**{"on": False}))
        self.assertEqual(self.runs()[0]["state"], "review")

    def test_a_follow_up_waiting_is_changes_requested(self):
        t = self.run_task()
        db.add_task(self.proj["id"], "use snake_case please", 0, t["id"])     # what Request changes sends
        self.assertEqual(self.runs()[0]["state"], "changes")

    def test_revert_queues_a_reply_and_runs_no_git(self):
        t = self.run_task()
        out = self.call("POST", f"/den/work/runs/{t['id']}/revert").json()
        new = db.get_task(out["task_id"])
        self.assertEqual((new["status"], new["thread_id"], new["lane"]), ("queued", t["id"], "agent"))
        self.assertIn("git revert --no-edit", new["prompt"])
        self.assertIn("abc123def456", new["prompt"])
        self.assertIn("Don't push", new["prompt"])
        self.assertEqual(self.runs()[0]["state"], "reverted")
        again = self.call("POST", f"/den/work/runs/{t['id']}/revert")
        self.assertEqual(again.status_code, 409, "one revert at a time")

    def test_revert_needs_a_git_starting_point(self):
        t = self.run_task(artifacts={"files": [{"path": "a.txt", "additions": 1, "deletions": 0}], "git": None})
        self.assertEqual(self.call("POST", f"/den/work/runs/{t['id']}/revert").status_code, 409)

    # ---------------------------------------------------------------- hidden

    def hidden(self):
        return {h["id"]: h for h in self.call("GET", "/den/work/hidden").json()["items"]}

    def test_why_its_hidden(self):
        mine = db.add_task(self.proj["id"], "clean old branches")
        den_recipes._ensure()
        with db._lock:
            db.get_conn().execute("INSERT INTO den_recipes (name, enabled, created_at, updated_at) VALUES ('Weekly SEO audit', 0, ?, ?)",
                                  (db._now(), db._now()))
            db.get_conn().commit()
        muted = db.add_task(self.proj["id"], "audit the SEO", 0, None, routed_by="recipe", route_reason="Recipe: Weekly SEO audit")
        sc = db.add_schedule("Nightly links", "check links", self.proj["id"], "daily", "02:00")
        db.set_schedule_enabled(sc["id"], False)
        sched = db.add_task(self.proj["id"], "check links", 0, None, routed_by="schedule")
        later = db.add_task(self.proj["id"], "translate chapter 6")
        reply = db.add_task(self.proj["id"], "more", 0, mine["id"])
        db.set_task_hidden([mine["id"], muted["id"], sched["id"]], True)
        until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.assertEqual(self.call("POST", f"/den/work/hidden/{later['id']}/snooze", body=den_work.SnoozeIn(**{"until": until})).status_code, 200)
        got = self.hidden()
        self.assertNotIn(reply["id"], got, "a reply is listed under its chat")
        self.assertEqual({i: h["why"] for i, h in got.items()},
                         {mine["id"]: "hidden", muted["id"]: "muted", sched["id"]: "muted", later["id"]: "snoozed"})
        self.assertEqual(got[muted["id"]]["recipe"], "Weekly SEO audit")
        self.assertTrue(got[later["id"]]["until"])

    def test_snooze_wakes_up(self):
        t = db.add_task(self.proj["id"], "translate chapter 6")
        den_work.snooze(t["id"], (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.assertEqual(db.get_task(t["id"])["hidden"], 1)
        self.assertEqual(den_work.wake_snoozed(datetime.now(timezone.utc) + timedelta(hours=2)), [t["id"]])
        self.assertEqual(db.get_task(t["id"])["hidden"], 0)
        self.assertNotIn(t["id"], self.hidden())
        past = self.call("POST", f"/den/work/hidden/{t['id']}/snooze", body=den_work.SnoozeIn(**{"until": "2000-01-01T00:00:00+00:00"}))
        self.assertEqual(past.status_code, 400)

    def test_run_now_by_status(self):
        queued = db.add_task(self.proj["id"], "queued one")
        failed = db.add_task(self.proj["id"], "failed one")
        db.mark_running(failed["id"])
        db.mark_error(failed["id"], "boom", needs_review=False)
        done = self.run_task("finished one")
        db.set_task_hidden([queued["id"], failed["id"], done["id"]], True)
        r = self.call("POST", f"/den/work/hidden/{queued['id']}/run").json()
        self.assertEqual((r["mode"], db.get_task(queued["id"])["run_now"], db.get_task(queued["id"])["hidden"]), ("now", 1, 0))
        r = self.call("POST", f"/den/work/hidden/{failed['id']}/run").json()
        self.assertEqual((r["mode"], db.get_task(failed["id"])["status"]), ("retry", "queued"))
        r = self.call("POST", f"/den/work/hidden/{done['id']}/run").json()
        again = db.get_task(r["task_id"])
        self.assertEqual((r["mode"], again["prompt"], again["status"], again["thread_id"]), ("again", "finished one", "queued", None))
        self.assertEqual(self.hidden(), {})

    def test_run_now_refuses_a_paused_project(self):
        t = db.add_task(self.proj["id"], "x")
        with db._lock:
            db.get_conn().execute("UPDATE projects SET active = 0 WHERE id = ?", (self.proj["id"],))
            db.get_conn().commit()
        self.assertEqual(self.call("POST", f"/den/work/hidden/{t['id']}/run").status_code, 409)

    # ---------------------------------------------------------------- calendar

    def test_skip_one_day_of_a_repeat(self):
        first = timeline.today() + timedelta(days=1)
        ev = timeline.create(self.proj["id"], "reminder", "Renew cert", first.isoformat(), "09:00", repeat_rule={"freq": "days", "every": 1})
        day2 = (first + timedelta(days=1)).isoformat()
        out = self.call("POST", f"/den/work/calendar/{ev['id']}/skip", body=den_work.SkipIn(**{"date": day2}))
        self.assertEqual(out.status_code, 200, out.text)
        self.assertEqual(out.json()["skip_dates"], [day2])
        row = db.get_project_event(ev["id"])
        got = [d.isoformat() for d in timeline.days_between(row, first, first + timedelta(days=3))]
        self.assertEqual(got, [first.isoformat(), (first + timedelta(days=2)).isoformat(), (first + timedelta(days=3)).isoformat()])
        self.assertEqual(self.call("POST", f"/den/work/calendar/{ev['id']}/skip", body=den_work.SkipIn(**{"date": "2000-01-01"})).status_code, 422)
        once = timeline.create(self.proj["id"], "deadline", "Ship", first.isoformat())
        self.assertEqual(self.call("POST", f"/den/work/calendar/{once['id']}/skip", body=den_work.SkipIn(**{"date": first.isoformat()})).status_code, 422)
        self.assertEqual(self.call("POST", "/den/work/calendar/9999/skip", body=den_work.SkipIn(**{"date": first.isoformat()})).status_code, 404)


class TimelineRepeats(TestBase):
    """What the Calendar page needs from timeline.py: reminders, repeats on more kinds, skipped days kept."""

    def setUp(self):
        super().setUp()
        self.pid = db.add_project("shop", self._tmp.name)["id"]
        self.first = timeline.today() + timedelta(days=1)

    def test_reminders_and_tasks_repeat_deadlines_dont(self):
        rem = timeline.view(timeline.create(self.pid, "reminder", "Water plants", self.first.isoformat(), "08:00",
                                            repeat_rule={"freq": "weekdays"}))
        self.assertEqual((rem["kind_label"], rem["repeat"]), ("Reminder", "every weekday"))
        task = timeline.create(self.pid, "task", "Backup check", self.first.isoformat(), repeat_rule={"freq": "weeks", "every": 1})
        self.assertEqual(timeline.rule_of(task)["freq"], "weeks")
        with self.assertRaises(timeline.TimelineError):
            timeline.create(self.pid, "deadline", "Launch", self.first.isoformat(), repeat_rule={"freq": "days", "every": 1})

    def test_skips_survive_edits_and_move_a_days_rule_into_repeat_rule(self):
        ev = timeline.create(self.pid, "meeting", "Standup", self.first.isoformat(), "09:30", repeat_days=1)
        self.assertIsNone(db.get_project_event(ev["id"])["repeat_rule"])
        day = (self.first + timedelta(days=2)).isoformat()
        timeline.skip(ev["id"], day)
        row = db.get_project_event(ev["id"])
        self.assertEqual((row["repeat_days"], row["repeat_rule"]), (None, {"freq": "days", "every": 1, "skip": [day]}))
        timeline.update(ev["id"], {"title": "Team standup"})
        self.assertEqual(timeline.skip_dates(db.get_project_event(ev["id"])), [day], "an edit keeps skipped days")
        timeline.update(ev["id"], {"repeat_rule": {"freq": "weekdays"}})
        self.assertEqual(timeline.skip_dates(db.get_project_event(ev["id"])), [day], "so does a new rule")
        timeline.update(ev["id"], {"skip_dates": []})
        self.assertEqual(timeline.skip_dates(db.get_project_event(ev["id"])), [])
        with self.assertRaises(timeline.TimelineError):
            timeline.update(ev["id"], {"skip_dates": ["soon"]})

    def test_a_skipped_next_day_moves_current_on(self):
        ev = timeline.create(self.pid, "meeting", "Standup", self.first.isoformat(), "09:30", repeat_days=1)
        timeline.skip(ev["id"], self.first.isoformat())
        cur = timeline.current(db.get_project_event(ev["id"]))
        self.assertEqual(cur["date"], (self.first + timedelta(days=1)).isoformat())
        self.assertIsInstance(date.fromisoformat(cur["start_date"]), date)
