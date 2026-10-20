"""The two Restart buttons: waiting tasks are held and let go again on the next start (only the
ones the restart held), a hard restart's killed runs are marked stopped, and tasks the old
process left "running" are marked interrupted on start."""

import asyncio
import unittest
from pathlib import Path
from unittest import mock

import api
import db
import restart_control
import worker
from tests.helpers import TestBase


class RestartCase(TestBase):
    def setUp(self):
        super().setUp()
        old = restart_control.HELD_FILE
        restart_control.HELD_FILE = Path(self._tmp.name) / "restart_held.json"
        self.addCleanup(lambda: setattr(restart_control, "HELD_FILE", old))
        self.pid = self.project["id"]

    def task(self, status="queued", paused=False):
        t = db.add_task(self.pid, f"a {status} task")
        conn = db.get_conn()
        conn.execute("UPDATE tasks SET status = ?, paused = ? WHERE id = ?", (status, int(paused), t["id"]))
        conn.commit()
        return t["id"]

    def test_holds_waiting_and_lets_go_only_of_those(self):
        mine = self.task(paused=True)                 # paused by you before the restart
        waiting = self.task()
        limited = self.task("rate_limited")
        done = self.task("done")
        self.assertEqual(restart_control.hold_waiting(), [waiting, limited])
        self.assertTrue(db.get_task(waiting)["paused"])
        late = self.task()                            # queued while draining (a reply, a schedule)
        self.assertEqual(restart_control.hold_waiting(), [late])
        self.assertEqual(restart_control.held_ids(), [waiting, limited, late])

        restart_control.on_startup()
        self.assertFalse(db.get_task(waiting)["paused"])
        self.assertFalse(db.get_task(limited)["paused"])
        self.assertFalse(db.get_task(late)["paused"])
        self.assertTrue(db.get_task(mine)["paused"])  # stays paused
        self.assertEqual(db.get_task(done)["status"], "done")
        self.assertFalse(restart_control.HELD_FILE.exists())

    def test_startup_marks_orphans_interrupted(self):
        orphan = self.task("running")
        restart_control.on_startup()
        t = db.get_task(orphan)
        self.assertEqual(t["status"], "error")
        self.assertEqual(t["error_message"], restart_control.INTERRUPTED_NOTE)

    def test_startup_without_a_restart_changes_nothing(self):
        waiting = self.task()
        restart_control.on_startup()
        self.assertEqual(db.get_task(waiting)["status"], "queued")
        self.assertFalse(db.get_task(waiting)["paused"])

    def test_hard_restart_marks_killed_runs_stopped_unless_finished(self):
        killed, errored, finished = self.task("running"), self.task("error"), self.task("done")
        restart_control.mark_stopped([killed, errored, finished])
        for tid in (killed, errored):
            t = db.get_task(tid)
            self.assertEqual((t["status"], t["error_message"]), ("cancelled", restart_control.STOPPED_NOTE))
            self.assertIn(t["status"], db.REQUEUEABLE)  # Retry continues it
        self.assertEqual(db.get_task(finished)["status"], "done")

    def test_phase_autopilot_holds_off_while_draining(self):
        with mock.patch.object(worker, "draining", True), mock.patch.object(worker.phases, "tick") as tick:
            self.assertEqual(worker.run_phase_autopilot(), 0)
        tick.assert_not_called()


class RestartRequestCase(RestartCase):
    """worker.request_restart without exiting the test run."""

    def setUp(self):
        super().setUp()
        for name, value in (("draining", False), ("restart_hard", False), ("_restart_task", None)):
            self.addCleanup(setattr, worker, name, getattr(worker, name))
            setattr(worker, name, value)
        self.exit = mock.patch.object(worker.os, "_exit").start()
        self.addCleanup(mock.patch.stopall)

    def test_normal_restart_holds_and_waits(self):
        waiting = self.task()

        async def go():
            r = await api.restart_service(None)
            await asyncio.sleep(0.7)
            return r
        r = asyncio.run(go())
        self.assertTrue(r["restarting"])
        self.assertFalse(r["hard"])
        self.assertEqual(r["held"], [waiting])
        self.assertTrue(db.get_task(waiting)["paused"])
        self.exit.assert_called_once_with(0)          # nothing running: right away

    def test_hard_restart_kills_running(self):
        running = self.task("running")
        proc = mock.Mock()
        kill = mock.patch.object(worker, "_kill_tree").start()

        async def go():
            worker._running[running] = proc
            r = await api.restart_service(api.RestartIn(hard=True))
            worker._running.pop(running, None)        # the lane winds down after the kill
            await asyncio.sleep(1)
            return r
        r = asyncio.run(go())
        kill.assert_called_once_with(proc)
        self.assertEqual(r["stopped"], [running])
        self.assertTrue(r["hard"])
        self.assertEqual(db.get_task(running)["error_message"], restart_control.STOPPED_NOTE)
        self.exit.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
