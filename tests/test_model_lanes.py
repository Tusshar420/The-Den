"""A new task starts straight away when its model is free: the api lane runs one
task at a time per model, the models side by side, and adding a task wakes its
lane instead of waiting for the next poll."""

import asyncio
import threading
from unittest import mock

from tests.helpers import TestBase  # first: it clears the shell's real API keys before worker reads them
import db
import providers
import worker

GPT, GEMINI = "gpt-6-luna", "gemini-3.8-flash"


class ModelLanes(TestBase):
    def setUp(self):
        super().setUp()
        # No real model calls: each call is recorded and blocks until the test lets it finish.
        self.calls: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.released = threading.Event()   # set at cleanup: every call, even one not yet begun, returns

        def fake_complete(model_cfg, messages, instructions, timeout, **kw):
            prompt = messages[-1]["content"]
            with self._lock:
                self.calls.append(prompt)
                gate = self.gates.setdefault(prompt, threading.Event())
            if not self.released.is_set():
                gate.wait(5)
            return providers.Completion("answer to " + prompt, 1, 1)

        for p in (mock.patch.object(providers, "complete", fake_complete),
                  mock.patch.object(worker, "ACTIVITYSMITH_API_KEY", None)):
            p.start()
            self.addCleanup(p.stop)
        worker._api_runs.clear()
        self.addCleanup(worker._api_runs.clear)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.addCleanup(self._let_runs_finish)

    def _let_runs_finish(self):
        self.released.set()
        with self._lock:
            for gate in self.gates.values():
                gate.set()
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        if pending:
            self.loop.run_until_complete(asyncio.wait(pending, timeout=5))
        self.loop.run_until_complete(self.loop.shutdown_default_executor())

    def queue(self, prompt, model_id):
        return db.add_task(self.project["id"], prompt, 0, None, lane="api", model_id=model_id)

    async def until(self, cond, timeout=5.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting")

    def tick(self):
        return self.loop.run_until_complete(self._tick())

    async def _tick(self):
        return worker.start_ready_api_tasks()

    def finish(self, prompt, task_id):
        async def go():
            self.gates.setdefault(prompt, threading.Event()).set()
            await self.until(lambda: db.get_task(task_id)["status"] == "done")
            await asyncio.sleep(0)
        self.loop.run_until_complete(go())

    def test_different_models_run_side_by_side_one_task_each(self):
        g1, g2, m1 = self.queue("g1", GPT), self.queue("g2", GPT), self.queue("m1", GEMINI)
        self.assertEqual(sorted(self.tick()), sorted([(g1["id"], GPT), (m1["id"], GEMINI)]))
        self.loop.run_until_complete(self.until(lambda: len(self.calls) == 2))
        self.assertEqual(db.get_task(g1["id"])["status"], "running")
        self.assertEqual(db.get_task(m1["id"])["status"], "running")
        # One at a time per model: g2 waits for g1, not for m1.
        self.assertEqual(db.get_task(g2["id"])["status"], "queued")
        self.assertEqual(self.tick(), [])
        self.finish("g1", g1["id"])
        self.assertEqual(self.tick(), [(g2["id"], GPT)])

    def test_a_rate_limited_model_does_not_hold_up_the_others(self):
        db.set_model_rate_limited(GPT, "2999-01-01T00:00:00+00:00", "limit")
        g1, m1 = self.queue("g1", GPT), self.queue("m1", GEMINI)
        self.assertEqual(self.tick(), [(m1["id"], GEMINI)])
        self.assertEqual(db.get_task(g1["id"])["status"], "queued")

    def test_adding_a_task_starts_it_at_once_when_its_model_is_free(self):
        async def go():
            stop = asyncio.Event()
            with mock.patch.object(worker, "POLL_INTERVAL_SECONDS", 60):
                lane = asyncio.create_task(worker.api_worker_loop(stop))
                await asyncio.sleep(0.05)   # the lane's first look finds nothing and naps for 60s
                t = self.queue("now", GPT)
                await self.until(lambda: db.get_task(t["id"])["status"] == "running", timeout=2)
                self.gates.setdefault("now", threading.Event()).set()
                await self.until(lambda: db.get_task(t["id"])["status"] == "done", timeout=2)
                stop.set()
                worker.wake_api_lane()
                await lane
        try:
            self.loop.run_until_complete(go())
        finally:
            worker._api_lane_wake = None


class AgentLaneWake(TestBase):
    def test_adding_an_agent_task_wakes_the_agent_lane(self):
        with mock.patch.object(worker, "wake_agent_lane") as wake_agent, \
                mock.patch.object(worker, "wake_api_lane") as wake_api:
            db.add_task(self.project["id"], "fix it", 0, None, lane="agent")
        wake_agent.assert_called_once()
        wake_api.assert_not_called()
