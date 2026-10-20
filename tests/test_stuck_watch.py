"""The stuck watch (worker.run_claude_task): a running Claude Code task that prints nothing for
stuck_after_seconds() is flagged on the task and pushed once, and the flag clears when output
comes again. A fake `claude` script stands in for Claude Code; the clock is shrunk to seconds."""

import asyncio
import os
import stat
import textwrap
from pathlib import Path

from tests.helpers import TestBase

import db
import worker


class StuckWatchTest(TestBase):
    def setUp(self):
        super().setUp()
        self.calls = []        # (task_id, since, reason) for every db.set_stuck
        self.pushes = []
        real_set = db.set_stuck

        def record_set(task_id, since, reason):
            self.calls.append((task_id, since, reason))
            real_set(task_id, since, reason)
        for name, new in (("CLAUDE_BIN", None), ("STUCK_CHECK_SECONDS", 1), ("stuck_after_seconds", lambda: 2),
                          ("notify", lambda kind, pid, title, msg: self.pushes.append((kind, title, msg)))):
            old = getattr(worker, name)
            self.addCleanup(setattr, worker, name, old)
            if new is not None:
                setattr(worker, name, new)
        self.addCleanup(setattr, db, "set_stuck", real_set)
        db.set_stuck = record_set

    def fake_claude(self, script: str) -> None:
        path = Path(self._tmp.name) / "claude"
        path.write_text("#!/bin/sh\n" + textwrap.dedent(script))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        worker.CLAUDE_BIN = str(path)

    def run_task(self):
        task = db.add_task(self.project["id"], "do the thing", 0)
        db.mark_running(task["id"])
        res = asyncio.run(worker.run_claude_task("do the thing", self._tmp.name, task["id"]))
        return task["id"], res

    def test_quiet_run_is_flagged_then_cleared_when_it_speaks_again(self):
        self.fake_claude("""
            echo '{"type":"system","subtype":"init","session_id":"s1"}'
            sleep 4
            echo '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'
            sleep 2
            echo '{"type":"result","result":"done","session_id":"s1"}'
        """)
        tid, _ = self.run_task()
        flagged = [c for c in self.calls if c[2]]
        self.assertEqual(len(flagged), 1)
        self.assertIn("No output from Claude Code", flagged[0][2])
        self.assertIsNotNone(flagged[0][1])
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0][0], "failure")
        self.assertIn(f"#{tid} looks stuck", self.pushes[0][1])
        self.assertIsNone(self.calls[-1][2])             # cleared again
        t = db.get_task(tid)
        self.assertIsNone(t["stuck_reason"])
        self.assertIsNone(t["stuck_since"])

    def test_a_chatty_run_is_never_flagged(self):
        self.fake_claude("""
            for i in 1 2 3 4; do echo '{"type":"system","subtype":"x"}'; sleep 1; done
            echo '{"type":"result","result":"done"}'
        """)
        self.run_task()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pushes, [])

    def test_waiting_on_an_approval_is_not_stuck(self):
        task = db.add_task(self.project["id"], "needs ok", 0)
        db.mark_running(task["id"])
        db.create_approval(task["id"], "Bash", "rm -rf build")
        self.fake_claude("""
            echo '{"type":"system","subtype":"init"}'
            sleep 4
            echo '{"type":"result","result":"done"}'
        """)
        asyncio.run(worker.run_claude_task("needs ok", self._tmp.name, task["id"]))
        self.assertEqual([c for c in self.calls if c[2]], [])

    def test_starting_a_run_clears_an_old_flag(self):
        task = db.add_task(self.project["id"], "again", 0)
        db.set_stuck(task["id"], "2026-01-01T00:00:00+00:00", "old")
        db.mark_running(task["id"])
        self.assertIsNone(db.get_task(task["id"])["stuck_reason"])
