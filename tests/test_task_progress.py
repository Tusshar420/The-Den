"""Progress bars: /tasks tells the dashboard how long a run in each project usually takes."""

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import api
import db


class TaskProgress(TestBase):
    def finished(self, project_id, ms, lane="agent"):
        t = db.add_task(project_id, "done before", 0, None, lane=lane)
        db.mark_running(t["id"])
        db.mark_done(t["id"], "ok")
        db.set_task_usage(t["id"], duration_ms=ms)

    def test_median_per_project_with_an_overall_fallback(self):
        other = db.add_project("Other", self._tmp.name + "/other")
        for ms in (60_000, 120_000, 600_000):
            self.finished(self.project["id"], ms)
        self.finished(other["id"], 30_000)            # too few runs to judge on its own
        self.finished(self.project["id"], 5, lane="api")  # other lanes don't count
        typical = db.typical_run_ms("agent")
        self.assertEqual(typical[self.project["id"]], 120_000)
        self.assertNotIn(other["id"], typical)
        self.assertEqual(typical[None], 120_000)       # median of 30k, 60k, 120k, 600k (upper middle)

    def test_only_recent_runs_count(self):
        for ms in [1_000_000] * 5 + [60_000] * 3:
            self.finished(self.project["id"], ms)
        self.assertEqual(db.typical_run_ms("agent", per_project=3)[self.project["id"]], 60_000)

    def test_running_tasks_carry_typical_ms_and_queued_ones_dont(self):
        for ms in (90_000, 90_000, 90_000):
            self.finished(self.project["id"], ms)
        run = db.add_task(self.project["id"], "working", 0, None, lane="agent")
        db.mark_running(run["id"])
        waiting = db.add_task(self.project["id"], "waiting", 0, None, lane="agent")
        by_id = {t["id"]: t for t in api.list_all_tasks(status="queued,running")}
        self.assertEqual(by_id[run["id"]]["typical_ms"], 90_000)
        self.assertNotIn("typical_ms", by_id[waiting["id"]])

    def test_no_history_means_no_estimate(self):
        run = db.add_task(self.project["id"], "first ever", 0, None, lane="agent")
        db.mark_running(run["id"])
        self.assertIsNone(api.list_all_tasks(status="running")[0]["typical_ms"])
