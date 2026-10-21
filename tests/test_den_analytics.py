"""
Analytics report (modules/den_analytics): it reads the Den's data read-only and sums it up right.

Uses the shared throwaway database from tests/helpers, seeded with a few tasks, emails and
usage rows, so no real data is touched.
"""

import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import TestBase

import db
from modules.den_analytics import report


class AnalyticsTest(TestBase):
    def setUp(self):
        super().setUp()
        self.project = db.add_project("Acme", self._tmp.name if hasattr(self, "_tmp") else "/tmp")

    def _task(self, status="done", account="acc1", model="claude-code", agent="coding", cost=0.5):
        t = db.add_task(self.project["id"], "do a thing")
        with db._lock:
            conn = db.get_conn()
            conn.execute("UPDATE tasks SET status=?, claude_account=?, model_id=?, agent_id=?, cost_usd=?, "
                         "input_tokens=100, output_tokens=50, duration_ms=2000 WHERE id=?",
                         (status, account, model, agent, cost, t["id"]))
            conn.commit()
        return t

    def _activity(self, tool, action, error=None):
        with db._lock:
            conn = db.get_conn()
            conn.execute("INSERT INTO assistant_activity (created_at, tool, action, error) VALUES (?,?,?,?)",
                         (db._now(), tool, action, error))
            conn.commit()

    def test_overview_and_sections(self):
        for _ in range(3):
            self._task(status="done", account="acc1", model="claude-code-sonnet", agent="coding", cost=0.2)
        self._task(status="error", account="acc2", model="gpt-6-sol", agent="general", cost=0.1)
        self._task(status="done", account="acc2", model="claude-code-sonnet", agent="personal", cost=0.3)
        for _ in range(4):
            self._activity("email", "send")
        self._activity("email", "send", error="SMTP failed")
        self._activity("email", "read")
        self._activity("workspace", "projects")
        db.log_usage("classifier", "gpt-6-luna", 10, 2, cost_usd=0.001)
        db.log_usage("title", "gpt-6-luna", 5, 1, cost_usd=0.0005)

        r = report.build(30)
        ov = r["overview"]
        self.assertEqual(ov["tasks"], 5)
        self.assertEqual(ov["done"], 4)
        self.assertEqual(ov["failed"], 1)
        self.assertEqual(ov["success_rate"], 80)
        self.assertEqual(ov["emails_sent"], 5)           # 4 send + 1 failed send; read not counted
        self.assertEqual(ov["accounts_used"], 2)
        self.assertAlmostEqual(ov["cost_usd"], 1.0, places=4)

        acc = {a["account"]: a for a in r["by_account"]}
        self.assertEqual(acc["acc1"]["tasks"], 3)
        self.assertEqual(acc["acc2"]["tasks"], 2)
        self.assertEqual(acc["acc2"]["done"], 1)

        models = {m["model"]: m for m in r["by_model"]}
        self.assertEqual(models["claude-code-sonnet"]["tasks"], 4)
        self.assertEqual(models["claude-code-sonnet"]["done"], 4)
        self.assertEqual(models["gpt-6-sol"]["failed"], 1)

        email = r["emails"]
        self.assertEqual(email["sent"], 5)
        by_action = {e["action"]: e for e in email["by_action"]}
        self.assertEqual(by_action["send"]["count"], 5)
        self.assertEqual(by_action["send"]["failed"], 1)
        self.assertEqual(by_action["read"]["count"], 1)

        feats = {f["agent"]: f["count"] for f in r["features"]}
        self.assertEqual(feats["coding"], 3)
        self.assertEqual(feats["personal"], 1)

        tools = {t["tool"]: t["count"] for t in r["tools"]}
        self.assertEqual(tools["email"], 6)
        self.assertEqual(tools["workspace"], 1)

        calls = {m["model"]: m["calls"] for m in r["model_calls"]}
        self.assertEqual(calls["gpt-6-luna"], 2)

        self.assertEqual(len(r["trend"]), 30)
        self.assertEqual(sum(d["count"] for d in r["trend"]), 5)
        self.assertEqual(r["trend"][-1]["count"], 5)     # all seeded today

        names = {m["name"] for m in r["modules"]}
        self.assertIn("den_analytics", names)
        self.assertIn("hello", names)

    def test_installs_from_updates_state(self):
        now = datetime.now(timezone.utc)
        db.set_setting("module_state:den_updates:installs", [
            {"id": "a", "name": "Asha", "version": "2.1.0", "last_seen": now.isoformat()},
            {"id": "b", "name": "Ravi", "version": "2.0.0", "last_seen": (now - timedelta(days=40)).isoformat()},
            {"id": "c", "name": "New", "version": None, "last_seen": None},
        ])
        inst = report.build(30)["installs"]
        self.assertEqual(inst["total"], 3)
        self.assertEqual(inst["active"], 1)              # only Asha is recent
        self.assertEqual(report.build(30)["overview"]["installs_active"], 1)

    def test_empty_is_safe(self):
        r = report.build(7)
        self.assertEqual(r["overview"]["tasks"], 0)
        self.assertIsNone(r["overview"]["success_rate"])
        self.assertEqual(r["overview"]["emails_sent"], 0)
        self.assertEqual(len(r["trend"]), 7)
        self.assertEqual(r["installs"]["total"], 0)

    def test_days_is_clamped(self):
        self.assertEqual(report.build(0)["days"], 1)
        self.assertEqual(report.build(9999)["days"], 365)


if __name__ == "__main__":
    unittest.main()
