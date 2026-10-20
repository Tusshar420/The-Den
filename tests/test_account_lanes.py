"""Per-project Claude accounts: each project's Claude Code tasks always run on its
own account, projects on different accounts run side by side, and an account's
usage limit holds only its own projects - until it resets, never moving them to
another account."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

from tests.helpers import TestBase  # first: it clears the shell's real API keys before worker reads them
import accounts
import api
import db
import worker

A1, A2 = "acct1aaaaa", "acct2bbbbb"


class Base(TestBase):
    def setUp(self):
        super().setUp()
        self._old_file, accounts.FILE = accounts.FILE, Path(self._tmp.name) / "accounts.json"
        self.addCleanup(lambda: setattr(accounts, "FILE", self._old_file))
        accounts._mac_cache.update(at=9e18, info={"logged_in": True, "email": "mac@example.com"})
        self.addCleanup(lambda: accounts._mac_cache.update(at=0.0, info=None))
        self._old_bg, accounts.probe_in_background = accounts.probe_in_background, lambda i: None
        self.addCleanup(lambda: setattr(accounts, "probe_in_background", self._old_bg))
        accounts._save([{"id": A1, "label": "Account 1", "token": "sk-ant-oat01-" + "1" * 40},
                        {"id": A2, "label": "Account 2", "token": "sk-ant-oat01-" + "2" * 40}])
        for name in ("a", "b"):
            os.makedirs(Path(self._tmp.name) / name)
        self.a = db.add_project("Project A", str(Path(self._tmp.name) / "a"))
        self.b = db.add_project("Project B", str(Path(self._tmp.name) / "b"))
        db.update_project_settings(self.a["id"], claude_account=A1)
        db.update_project_settings(self.b["id"], claude_account=A2)

        # No real Claude Code: each run is recorded and waits until the test lets it finish
        # with the result set for its account.
        self.runs: list[tuple[int, str]] = []
        self.released = False
        self.gates: dict[int, asyncio.Event] = {}
        self.outcome: dict[str, worker.TaskResult] = {}
        self._old_key, worker.ACTIVITYSMITH_API_KEY = worker.ACTIVITYSMITH_API_KEY, None   # no real pushes
        self.addCleanup(lambda: setattr(worker, "ACTIVITYSMITH_API_KEY", self._old_key))
        self._old_run, worker.run_claude_task = worker.run_claude_task, self.fake_run
        self.addCleanup(lambda: setattr(worker, "run_claude_task", self._old_run))
        worker._account_runs.clear()
        self.addCleanup(worker._account_runs.clear)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.addCleanup(self._let_runs_finish)

    def _let_runs_finish(self):
        self.released = True
        for gate in self.gates.values():
            gate.set()
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        if pending:
            self.loop.run_until_complete(asyncio.wait(pending, timeout=5))
        self.loop.run_until_complete(self.loop.shutdown_default_executor())

    async def fake_run(self, prompt, path, task_id, session_id=None, account_id=None, **kw):
        self.runs.append((task_id, account_id))
        gate = self.gates.setdefault(task_id, asyncio.Event())
        if not self.released:
            await gate.wait()
        return self.outcome.get(account_id) or worker.TaskResult(True, summary="done")

    def queue(self, project, prompt, priority=0):
        return db.add_task(project["id"], prompt, priority, None, lane="agent", model_id="claude-code")

    @staticmethod
    async def until(cond, timeout=10.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting")

    def tick(self):
        """One pass of the queue, then wait for the started runs to reach Claude Code."""
        async def go():
            before = len(self.runs)
            started = worker.start_ready_tasks()
            await self.until(lambda: len(self.runs) >= before + len(started))
            return started
        return self.loop.run_until_complete(go())

    def finish(self, *task_ids):
        """Let these runs end, and wait until the queue has filed their results."""
        async def go():
            for tid in task_ids:
                self.gates.setdefault(tid, asyncio.Event()).set()
            await self.until(lambda: all(db.get_task(tid)["status"] != "running" for tid in task_ids))
            await asyncio.sleep(0)   # the run's asyncio task ends right after it files the result
        self.loop.run_until_complete(go())

    def limit(self, account_id, hours=1.0):
        self.outcome[account_id] = worker.TaskResult(
            False, rate_limited=True, error="You've hit your limit",
            resume_at=datetime.now(timezone.utc) + timedelta(hours=hours))


class AccountLanes(Base):
    def test_projects_on_different_accounts_run_at_the_same_time(self):
        a1, a2, b1 = self.queue(self.a, "a1"), self.queue(self.a, "a2"), self.queue(self.b, "b1")
        self.assertEqual(sorted(self.tick()), sorted([(a1["id"], A1), (b1["id"], A2)]))
        self.assertEqual(db.get_task(a1["id"])["status"], "running")
        self.assertEqual(db.get_task(b1["id"])["status"], "running")
        # One task at a time per account: a2 waits for a1, not for b1.
        self.assertEqual(db.get_task(a2["id"])["status"], "queued")
        self.assertEqual(self.tick(), [])
        self.finish(a1["id"])
        self.assertEqual(self.tick(), [(a2["id"], A1)])

    def test_a_limit_pauses_only_its_projects_and_never_switches_account(self):
        a1, a2 = self.queue(self.a, "a1"), self.queue(self.a, "a2")
        b1, b2 = self.queue(self.b, "b1"), self.queue(self.b, "b2")
        self.limit(A1)
        self.tick()
        self.finish(a1["id"], b1["id"])

        t = db.get_task(a1["id"])
        self.assertEqual((t["status"], t["claude_account"]), ("rate_limited", A1))
        self.assertIn(A1, db.claude_account_limits(accounts.active_id()))
        # Project B carries on on account 2; project A's next task waits even though account 2 is free.
        self.assertEqual(self.tick(), [(b2["id"], A2)])
        self.finish(b2["id"])
        self.assertEqual(self.tick(), [])
        self.assertEqual(db.get_task(a2["id"])["status"], "queued")
        self.assertTrue(all(acct == A1 for tid, acct in self.runs if tid in (a1["id"], a2["id"])))

        # Account 1 resets: the interrupted task resumes first, on account 1 again.
        self.outcome.pop(A1)
        db.mark_rate_limited(a1["id"], (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        self.assertEqual(self.tick(), [(a1["id"], A1)])
        self.finish(a1["id"])
        self.assertEqual(db.get_task(a1["id"])["status"], "done")
        self.assertEqual(self.tick(), [(a2["id"], A1)])

    def test_retry_now_ends_the_wait(self):
        a1, a2 = self.queue(self.a, "a1"), self.queue(self.a, "a2")
        self.limit(A1)
        self.tick()
        self.finish(a1["id"])
        self.assertEqual(self.tick(), [])
        self.outcome.pop(A1)
        db.requeue_task(a1["id"], front=True)
        self.assertEqual(self.tick(), [(a1["id"], A1)])

    def test_projects_without_their_own_account_share_the_default(self):
        c = db.add_project("Project C", str(Path(self._tmp.name) / "a"))
        c1, c2 = self.queue(c, "c1"), self.queue(c, "c2")
        self.assertEqual(self.tick(), [(c1["id"], accounts.MAC)])
        api.activate_account(A2)             # the default is account 2 now - which project B also uses
        b1 = self.queue(self.b, "b1")
        self.finish(c1["id"])
        started = self.tick()
        self.assertEqual(len(started), 1, "C and B share account 2 now, so only one of them starts")
        self.assertIn(started[0], [(c2["id"], A2), (b1["id"], A2)])

    def test_switching_the_default_does_not_move_a_project_with_its_own_account(self):
        c = db.add_project("Project C", str(Path(self._tmp.name) / "a"))
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        a1, c1 = self.queue(self.a, "a1"), self.queue(c, "c1")
        for t, acct in ((a1, A1), (c1, accounts.MAC)):
            db.claim_task(t["id"], acct)
            db.mark_rate_limited(t["id"], soon)
        resumed = api.activate_account(A2)["resumed"]
        self.assertEqual(resumed, 1)
        self.assertEqual(db.get_task(a1["id"])["resume_at"], soon, "project A keeps waiting for account 1")
        self.assertLess(db.get_task(c1["id"])["resume_at"], soon)
        self.assertEqual(self.tick(), [(c1["id"], A2)])

    def test_moving_a_project_to_another_account_lets_it_go_on_at_once(self):
        a1 = self.queue(self.a, "a1")
        self.limit(A1)
        self.tick()
        self.finish(a1["id"])
        self.assertEqual(self.tick(), [])
        self.outcome.pop(A1)
        s = api.update_project_settings(self.a["id"], api.ProjectSettingsIn(claude_account=A2))
        self.assertEqual((s["claude_account"], s["claude_account_in_use"]), (A2, A2))
        self.assertEqual(self.tick(), [(a1["id"], A2)])

    def test_a_task_pinned_to_an_account_runs_there_whatever_its_project_uses(self):
        t = db.add_task(self.a["id"], "pinned", claude_account=A2)
        self.assertEqual(db.get_task(t["id"])["pinned_account"], A2)
        self.assertEqual(self.tick(), [(t["id"], A2)])

    def test_low_priority_is_held_per_account(self):
        db.set_setting("hold_low_when_busy", True)
        resets = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        accounts.record_usage(A1, {"windows": {"five_hour": {"utilization": 0.95, "resets_at": resets}}})
        accounts.record_usage(A2, {"windows": {"five_hour": {"utilization": 0.10, "resets_at": resets}}})
        a1, b1 = self.queue(self.a, "a1", priority=-1), self.queue(self.b, "b1", priority=-1)
        self.assertEqual(self.tick(), [(b1["id"], A2)])
        self.assertEqual(db.get_task(a1["id"])["status"], "queued")

    def test_proceed_now_uses_the_projects_account(self):
        a1 = self.queue(self.a, "a1")
        run = self.loop.create_task(worker.run_agent_task(db.get_task(a1["id"])))
        self.loop.run_until_complete(self.until(lambda: self.runs))
        self.assertEqual(self.runs, [(a1["id"], A1)])
        self.finish(a1["id"])
        self.loop.run_until_complete(run)

    def test_claude_code_counts_as_limited_only_when_the_default_account_is(self):
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        a1 = self.queue(self.a, "a1")
        db.claim_task(a1["id"], A1)
        db.mark_rate_limited(a1["id"], soon)
        self.assertIsNone(db.model_rate_limited_until("claude-code"))
        api.activate_account(A1)
        self.assertEqual(db.model_rate_limited_until("claude-code"), soon)


class ProjectAccountSetting(Base):
    def test_set_and_clear_from_the_api(self):
        s = api.get_project_settings(self.a["id"])
        self.assertEqual((s["claude_account"], s["claude_account_in_use"]), (A1, A1))
        s = api.update_project_settings(self.a["id"], api.ProjectSettingsIn(claude_account=""))
        self.assertEqual((s["claude_account"], s["claude_account_in_use"]), (None, accounts.MAC))
        with self.assertRaises(HTTPException) as e:
            api.update_project_settings(self.a["id"], api.ProjectSettingsIn(claude_account="nope"))
        self.assertEqual(e.exception.status_code, 400)
        listed = {p["id"]: p["claude_account"] for p in api.list_projects()}
        self.assertEqual((listed[self.a["id"]], listed[self.b["id"]]), (accounts.MAC, A2))

    def test_accounts_list_their_projects_and_limits(self):
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        a1 = self.queue(self.a, "a1")
        db.claim_task(a1["id"], A1)
        db.mark_rate_limited(a1["id"], soon)
        by_id = {a["id"]: a for a in accounts.public_list()}
        self.assertEqual(by_id[A1]["projects"], [{"id": self.a["id"], "name": "Project A"}])
        self.assertEqual(by_id[A1]["limited_until"], soon)
        self.assertIsNone(by_id[A2]["limited_until"])

    def test_removing_an_account_puts_its_projects_on_the_default(self):
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        a1 = self.queue(self.a, "a1")
        db.claim_task(a1["id"], A1)
        db.mark_rate_limited(a1["id"], soon)
        accounts.remove(A1)
        self.assertIsNone(db.get_project_settings(self.a["id"])["claude_account"])
        self.assertEqual(accounts.for_project(self.a["id"]), accounts.MAC)
        self.assertLess(db.get_task(a1["id"])["resume_at"], soon)


class OnlyAccount(Base):
    def test_task_limited_to_another_account_is_never_started_or_sent(self):
        t = db.add_task(self.a["id"], "x", 0, None, lane="agent", model_id="claude-code", only_account=A2)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.runs, [])
        self.assertEqual(db.get_task(t["id"])["status"], "queued")
        # Proceed now doesn't get around it either.
        self.loop.run_until_complete(worker.run_agent_task(db.get_task(t["id"])))
        self.assertEqual(self.runs, [])

    def test_blocked_task_does_not_hold_up_the_rest_of_its_account(self):
        db.add_task(self.a["id"], "x", 0, None, lane="agent", model_id="claude-code", only_account=A2)
        ok = self.queue(self.a, "y")
        self.assertEqual(self.tick(), [(ok["id"], A1)])

    def test_changing_its_account_releases_it(self):
        t = db.add_task(self.a["id"], "x", 0, None, lane="agent", model_id="claude-code", only_account=A2)
        self.assertEqual(self.tick(), [])
        db.set_only_account(t["id"], A2)
        self.assertEqual(self.tick(), [(t["id"], A2)])

    def test_todo_account_carries_onto_its_task_and_runs_there(self):
        todo = db.add_todo(self.a["id"], "do it")
        db.update_todo(todo["id"], checked=True, settings={"only_account": A2})
        db.run_checked_todos(self.a["id"])
        task = db.get_task(db.get_todo(todo["id"])["task_id"])
        self.assertEqual(task["only_account"], A2)
        self.assertEqual(task["pinned_account"], A2)
        # Its project is on A1, but it runs on the account picked for it rather than waiting.
        self.assertEqual(self.tick(), [(task["id"], A2)])


class PickAccountPerTask(Base):
    """The ACCOUNT row on Edit task and the chat reply box."""

    def test_editing_a_queued_task_moves_it_to_another_account_and_back(self):
        t = self.queue(self.a, "x")
        out = api.edit_task(t["id"], api.TaskEditIn(claude_account=A2))
        self.assertEqual(out["pinned_account"], A2)
        out = api.edit_task(t["id"], api.TaskEditIn(claude_account=""))   # Auto: the project's own again
        self.assertIsNone(out["pinned_account"])
        api.edit_task(t["id"], api.TaskEditIn(claude_account=A2))
        self.assertEqual(self.tick(), [(t["id"], A2)])

    def test_an_edit_moves_a_lock_to_one_account_with_it(self):
        t = db.add_task(self.a["id"], "x", 0, None, lane="agent", model_id="claude-code", only_account=A2)
        out = api.edit_task(t["id"], api.TaskEditIn(claude_account=A1))
        self.assertEqual((out["pinned_account"], out["only_account"]), (A1, A1))
        self.assertEqual(self.tick(), [(t["id"], A1)])

    def test_editing_to_an_unknown_account_is_refused(self):
        t = self.queue(self.a, "x")
        with self.assertRaises(HTTPException) as e:
            api.edit_task(t["id"], api.TaskEditIn(claude_account="nope"))
        self.assertEqual(e.exception.status_code, 400)

    def test_a_reply_can_name_its_account(self):
        root = self.queue(self.a, "first")
        db.mark_done(root["id"], "ok")
        reply = api.create_task(self.a["id"], api.TaskIn(prompt="more", thread_id=root["id"], account=A2))
        self.assertEqual(reply["pinned_account"], A2)
