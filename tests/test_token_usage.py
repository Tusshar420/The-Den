"""The Token usage page's data (db.token_usage, GET /usage/tokens) and the side-call log it reads."""

from datetime import datetime, timedelta

from fastapi import HTTPException

import api
import db
from tests.helpers import TestBase


def _day(days_ago: int) -> str:
    """Local noon `days_ago` days back, as the UTC ISO the tasks table stores."""
    d = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return db._utc_iso(d)


class TokenUsage(TestBase):
    def setUp(self):
        super().setUp()
        self.other = db.add_project("Site", self._tmp.name + "/site")

    def test_account_filter(self):
        a = self._task(self.project, 1, 100, 10, 0.1)       # no account recorded -> the default (mac)
        b = self._task(self.project, 1, 200, 20, 0.2)
        c = self._task(self.project, 1, 400, 40, 0.0, lane="api")
        with db._lock:
            db.get_conn().execute("UPDATE tasks SET claude_account = 'work' WHERE id = ?", (b["id"],))
            db.get_conn().commit()
        everything = db.token_usage(default_account="mac")
        self.assertEqual({g["key"] for g in everything["by_account"]}, {"mac", "work"})
        self.assertEqual(everything["options"]["accounts"], ["mac", "work"])
        only = db.token_usage(account="work", default_account="mac")
        self.assertEqual(only["totals"]["total_tokens"], 220)
        self.assertEqual(db.token_usage(account="mac", default_account="mac")["totals"]["total_tokens"], 110)

    def _task(self, project, days_ago, tin, tout, cost, lane="agent", model_id=None, thread_id=None, cache=None):
        t = db.add_task(project["id"], "prompt " + str(tin), lane=lane, model_id=model_id, thread_id=thread_id)
        db.set_task_usage(t["id"], 1000, tin, tout, cost, cache_tokens=cache)
        with db._lock:
            db.get_conn().execute("UPDATE tasks SET status = 'done', finished_at = ? WHERE id = ?", (_day(days_ago), t["id"]))
            db.get_conn().commit()
        return t

    def test_totals_split_real_spend_from_the_claude_code_estimate(self):
        self._task(self.project, 1, 1000, 100, 2.5, cache=800)
        self._task(self.other, 2, 200, 50, 0.01, lane="api", model_id="gpt-6-luna")
        db.add_task(self.project["id"], "no usage yet")          # still queued - not counted
        r = db.token_usage()
        self.assertEqual(r["days"], 30)
        self.assertEqual(r["totals"], {"runs": 2, "input_tokens": 1200, "output_tokens": 150, "cache_tokens": 800,
                                       "total_tokens": 1350, "spend_usd": 0.01, "claude_estimate_usd": 2.5})
        self.assertEqual([(g["label"], g["total_tokens"]) for g in r["by_project"]], [("General", 1100), ("Site", 250)])
        self.assertEqual([g["key"] for g in r["by_model"]], ["claude-code", "gpt-6-luna"])
        self.assertEqual(sum(d["total_tokens"] for d in r["by_day"]), 1350)
        self.assertEqual(r["items"][0]["total_tokens"], 1100)      # biggest first
        self.assertEqual(r["options"]["models"], ["claude-code", "gpt-6-luna"])

    def test_range_filters_and_previous_period(self):
        self._task(self.project, 3, 100, 10, None)
        self._task(self.project, 12, 500, 50, None)           # the 7 days before
        self._task(self.other, 3, 40, 4, 0.02, lane="api", model_id="gpt-6-luna")
        today = datetime.now().astimezone().date()
        r = db.token_usage(start=(today - timedelta(days=6)).isoformat(), end=today.isoformat())
        self.assertEqual((r["days"], r["totals"]["total_tokens"], r["previous"]["total_tokens"]), (7, 154, 550))
        self.assertEqual(db.token_usage(project_id=self.other["id"])["totals"]["total_tokens"], 44)
        self.assertEqual(db.token_usage(model_id="claude-code")["totals"]["runs"], 2)
        self.assertEqual(db.token_usage(lane="api")["totals"]["runs"], 1)

    def test_side_calls_count_and_can_be_seen_alone(self):
        t = self._task(self.other, 0, 100, 10, None)
        db.log_usage("title", "gpt-6-luna", 30, 5, 0.0001, task_id=t["id"])
        db.log_usage("classifier", "gpt-6-luna", 20, 2, None)
        r = db.token_usage()
        self.assertEqual(r["totals"]["runs"], 3)
        bg = db.token_usage(lane="background")
        self.assertEqual((bg["totals"]["total_tokens"], bg["totals"]["spend_usd"]), (57, 0.0001))
        title = next(i for i in bg["items"] if i["kind"] == "title")
        # the title call is filed under the task's project and chat
        self.assertEqual((title["project_id"], title["chat_id"]), (self.other["id"], t["id"]))
        self.assertEqual(db.token_usage(project_id=self.other["id"], lane="background")["totals"]["runs"], 1)

    def test_bad_ranges_are_refused(self):
        with self.assertRaises(ValueError):
            db.token_usage(start="2026-09-10", end="2026-09-01")
        with self.assertRaises(ValueError):
            db.token_usage(start="2024-01-01", end="2026-09-01")
        with self.assertRaises(HTTPException) as e:
            api.token_usage(start="not-a-date")
        self.assertEqual(e.exception.status_code, 400)
