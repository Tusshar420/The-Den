"""
Product-usage analytics (modules/den_usage): recording section opens/time and building the report.

Loads the module with a tiny fake ctx (like tests/test_approval_reminder.py) against the shared
throwaway database from tests/helpers, so nothing real is touched.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import TestBase

import db
from modules.den_usage.module import Module, SESSION


class FakeCtx:
    def __init__(self):
        self.commands = {}
        self.config = {}

    def register_command(self, name, fn, desc=""):
        self.commands[name] = fn


class UsageTest(TestBase):
    def setUp(self):
        super().setUp()
        self.ctx = FakeCtx()
        self.mod = Module()
        self.mod.initialize(self.ctx)
        self.track = self.ctx.commands["den-usage-track"]
        self.rep = self.ctx.commands["den-usage"]

    def _track(self, **payload):
        return self.track(json.dumps(payload))

    def _rowcount(self, section):
        with db._lock:
            r = db.get_conn().execute("SELECT opens, seconds FROM den_usage_daily WHERE day=date('now') AND section=?",
                                      (section,)).fetchone()
        return (r["opens"], r["seconds"]) if r else (0, 0)

    def test_records_and_accumulates(self):
        self._track(events=[{"section": "projects", "opens": 1, "seconds": 120}], new_session=True)
        self._track(events=[{"section": "projects", "opens": 2, "seconds": 60},
                            {"section": "crm", "opens": 1, "seconds": 300}])
        self.assertEqual(self._rowcount("projects"), (3, 180))
        self.assertEqual(self._rowcount("crm"), (1, 300))
        self.assertEqual(self._rowcount(SESSION), (1, 0))

    def test_report_shape(self):
        self._track(events=[{"section": "crm", "opens": 2, "seconds": 600},
                            {"section": "projects", "opens": 5, "seconds": 300},
                            {"section": "email", "opens": 1, "seconds": 60}], new_session=True)
        self._track(new_session=True)
        r = self.rep(json.dumps({"days": 7}))
        self.assertEqual(r["top"], "CRM")                       # most time, even though fewer opens
        self.assertEqual(r["totals"]["sessions"], 2)
        self.assertEqual(r["totals"]["opens"], 8)
        self.assertEqual(r["totals"]["minutes"], 16.0)
        self.assertEqual(r["totals"]["active_days"], 1)
        self.assertEqual(r["by_section"][0]["label"], "CRM")
        self.assertEqual(r["by_section"][0]["share_time"], 62)  # 600/960
        self.assertEqual(len(r["trend"]), 7)
        self.assertEqual(r["trend"][-1]["minutes"], 16.0)
        crm = next(a for a in r["adoption"] if a["section"] == "crm")
        clients = next(a for a in r["adoption"] if a["section"] == "clients")
        self.assertTrue(crm["used"])
        self.assertFalse(clients["used"])

    def test_rejects_bad_sections_and_clamps(self):
        self._track(events=[
            {"section": "../etc", "opens": 1, "seconds": 10},      # bad chars -> dropped
            {"section": SESSION, "opens": 1, "seconds": 10},        # reserved -> dropped
            {"section": "x" * 80, "opens": 1, "seconds": 10},       # too long -> dropped
            {"section": "projects", "opens": 999999, "seconds": 999999},  # clamped
        ])
        self.assertEqual(self._rowcount("projects"), (10000, 86400))
        with db._lock:
            n = db.get_conn().execute("SELECT COUNT(*) c FROM den_usage_daily").fetchone()["c"]
        self.assertEqual(n, 1)                                  # only projects got in

    def test_empty_report_is_safe(self):
        r = self.rep(json.dumps({"days": 30}))
        self.assertEqual(r["totals"]["opens"], 0)
        self.assertEqual(r["totals"]["sessions"], 0)
        self.assertIsNone(r["top"])
        self.assertEqual(len(r["trend"]), 30)
        self.assertEqual(r["totals"]["sections_total"], len(r["adoption"]))

    def test_days_clamped(self):
        self.assertEqual(self.rep(json.dumps({"days": 0}))["days"], 1)
        self.assertEqual(self.rep(json.dumps({"days": 99999}))["days"], 365)
        self.assertEqual(self.rep("")["days"], 30)

    def test_multi_day_trend(self):
        y = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        with db._lock:
            conn = db.get_conn()
            conn.execute("INSERT INTO den_usage_daily (day, section, opens, seconds) VALUES (?,?,?,?)",
                         (y, "notes", 3, 240))
            conn.execute("INSERT INTO den_usage_daily (day, section, opens, seconds) VALUES (?,?,?,?)",
                         (y, SESSION, 2, 0))
            conn.commit()
        self._track(events=[{"section": "notes", "opens": 1, "seconds": 60}], new_session=True)
        r = self.rep(json.dumps({"days": 7}))
        self.assertEqual(r["totals"]["active_days"], 2)
        self.assertEqual(r["totals"]["sessions"], 3)
        self.assertEqual(r["by_section"][0]["seconds"], 300)
        self.assertEqual(r["trend"][-2]["minutes"], 4.0)
        self.assertEqual(r["trend"][-1]["minutes"], 1.0)


if __name__ == "__main__":
    unittest.main()
