"""A message sent while Claude Code is mid-run is handed straight to it."""
import json
import unittest

import worker


class FakeStdin:
    def __init__(self):
        self.lines, self.closed = [], False

    def write(self, b):
        self.lines.append(json.loads(b.decode()))

    def is_closing(self):
        return self.closed


class FakeProc:
    def __init__(self):
        self.stdin = FakeStdin()


class Inject(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        worker._running[99] = self.proc
        worker._turns[99] = {"sent": 0, "done": 0}
        self.addCleanup(lambda: (worker._running.pop(99, None), worker._turns.pop(99, None)))

    def test_a_running_task_takes_the_message_as_another_turn(self):
        self.assertTrue(worker._send_user_message(99, self.proc, "first"))
        self.assertTrue(worker.inject_message(99, "also this"))
        self.assertEqual([m["message"]["content"] for m in self.proc.stdin.lines], ["first", "also this"])
        self.assertEqual(worker._turns[99]["sent"], 2)

    def test_a_run_that_has_answered_everything_does_not_take_one(self):
        worker._send_user_message(99, self.proc, "first")
        worker._turns[99]["done"] = 1
        self.assertFalse(worker.inject_message(99, "too late"))

    def test_nothing_running_means_no(self):
        self.assertFalse(worker.inject_message(12345, "hello"))


if __name__ == "__main__":
    unittest.main()
