"""What Octopus adds around a Claude Code run: tool limits, live plan, tests, git artifacts, validation."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import TestBase

import db
from octopus import execution


def git(path, *args):
    subprocess.run(["git", "-C", path, *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class ClaudeArgs(unittest.TestCase):
    def args(self, **task):
        return execution.claude_args(task)

    def test_a_task_with_no_recorded_tools_runs_exactly_as_before(self):
        self.assertEqual(self.args(agent_id=None, tools=None), [])
        self.assertEqual(self.args(agent_id="coding", tools=None), [])          # coding has no extra instructions

    def test_only_granted_tools_stay_on(self):
        a = self.args(agent_id="coding", tools=["filesystem", "git", "terminal"])
        denied = a[a.index("--disallowedTools") + 1].split(",")
        self.assertEqual(sorted(denied), ["WebFetch", "WebSearch"])

    def test_a_writing_task_that_fell_back_to_claude_code_gets_no_tools_and_its_instructions(self):
        a = self.args(agent_id="writing", tools=[])
        self.assertIn("--append-system-prompt", a)
        self.assertIn("Writing Agent", a[a.index("--append-system-prompt") + 1])
        denied = a[a.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Edit", "Write", "Read", "WebSearch"):
            self.assertIn(tool, denied)

    def test_research_keeps_only_the_web(self):
        denied = self.args(agent_id="research", tools=["web"])
        denied = denied[denied.index("--disallowedTools") + 1].split(",")
        self.assertNotIn("WebSearch", denied)
        self.assertIn("Bash", denied)


class Observer(TestBase):
    def msg(self, kind, *blocks):
        return {"type": kind, "message": {"content": list(blocks)}}

    def test_todowrite_becomes_the_live_plan(self):
        t = self.make_task("x")
        obs = execution.RunObserver(t["id"])
        obs.observe(self.msg("assistant", {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
            {"content": "Inspect auth", "status": "completed", "activeForm": "Inspecting"},
            {"content": "Fix bug", "status": "in_progress"}, {"content": "Run tests", "status": "weird"}]}}))
        steps = db.get_task(t["id"])["plan_steps"]
        self.assertEqual([(s["content"], s["status"]) for s in steps],
                         [("Inspect auth", "completed"), ("Fix bug", "in_progress"), ("Run tests", "pending")])

    def test_test_runs_are_recorded_with_their_outcome_and_logged(self):
        t = self.make_task("x")
        obs = execution.RunObserver(t["id"])
        obs.observe(self.msg("assistant", {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "npm test -- --watch=false"}},
                             {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "ls"}}))
        obs.observe(self.msg("user", {"type": "tool_result", "tool_use_id": "b", "content": "files"},
                             {"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": [{"type": "text", "text": "1 failed"}]}))
        obs.observe(self.msg("assistant", {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "pytest -q"}}))
        obs.observe(self.msg("user", {"type": "tool_result", "tool_use_id": "c", "content": "3 passed"}))
        self.assertEqual([(x["command"], x["ok"]) for x in obs.tests], [("npm test -- --watch=false", False), ("pytest -q", True)])
        self.assertEqual([e["text"] for e in db.list_task_events([t["id"]])[t["id"]]],
                         ["Tests failed: npm test -- --watch=false", "Tests passed: pytest -q"])

    def test_validation_verdicts(self):
        ok = [{"command": "pytest", "ok": True}]
        self.assertEqual(execution.validation(ok, [{"path": "a"}], "coding")["status"], "passed")
        self.assertEqual(execution.validation(ok + [{"command": "pytest", "ok": False}], [], "coding")["status"], "failed")
        self.assertEqual(execution.validation([], [{"path": "a"}], "coding")["status"], "unverified")
        self.assertEqual(execution.validation([], [], "writing")["status"], "n/a")


class GitArtifacts(TestBase):
    def setUp(self):
        super().setUp()
        self.repo = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.repo]))
        git(self.repo, "init", "-q", "-b", "main")
        (Path(self.repo) / "auth.py").write_text("x = 1\n")
        git(self.repo, "add", "."), git(self.repo, "commit", "-qm", "start")

    def test_not_a_repo(self):
        self.assertIsNone(execution.git_snapshot(tempfile.mkdtemp()))
        self.assertFalse(execution.full_diff({"git_base": None}, "/tmp")["available"])

    def test_snapshot_then_changes_commits_and_diff(self):
        snap = execution.git_snapshot(self.repo)
        self.assertEqual((snap["branch"], snap["started_dirty"]), ("main", 0))
        (Path(self.repo) / "auth.py").write_text("x = 2\ny = 3\n")
        (Path(self.repo) / "new.txt").write_text("hi\n")
        git(self.repo, "checkout", "-qb", "fix/auth")
        git(self.repo, "add", "auth.py"), git(self.repo, "commit", "-qm", "Fix auth")
        task = {"category": "coding", "git_base": snap}
        art = execution.collect_artifacts(task, self.repo, execution.RunObserver(self.make_task("x")["id"]))
        files = {f["path"]: f for f in art["files"]}
        self.assertEqual((files["auth.py"]["status"], files["auth.py"]["additions"], files["auth.py"]["deletions"]), ("M", 2, 1))
        self.assertEqual((files["new.txt"]["status"], files["new.txt"]["additions"]), ("A", 1), "untracked files get a line count too")
        self.assertEqual((art["git"]["branch"], art["git"]["base_branch"]), ("fix/auth", "main"))
        self.assertEqual(art["git"]["commits"][0].split(" ", 1)[1], "Fix auth")
        self.assertTrue(art["git"]["uncommitted"])
        self.assertEqual(art["validation"]["status"], "unverified")
        diff = execution.full_diff(task, self.repo)
        self.assertTrue(diff["available"])
        self.assertIn("+y = 3", diff["diff"])


if __name__ == "__main__":
    unittest.main()
