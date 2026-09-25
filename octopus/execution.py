"""
What Octopus adds around a run: the plan's tools and the agent's instructions
go to Claude Code, and what it did comes back as something you can read -
the live plan, test results, files changed, the git branch and diff, and a
one-line validation.

worker.py calls into here; nothing in this module talks to the queue except
through db.
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db
import uploads
from octopus import registry

# Claude Code tool names, by the Octopus tool that grants them.
CLAUDE_TOOLS: dict[str, list[str]] = {
    "filesystem": ["Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit"],
    "terminal": ["Bash"],
    "git": ["Bash"],
    "github": ["Bash"],
    "salesforce": ["Bash"],
    "web": ["WebSearch", "WebFetch"],
}
TEST_COMMAND_RE = re.compile(
    r"\b(pytest|py\.test|unittest|nosetests|tox|npm (?:run )?test|yarn test|pnpm (?:run )?test|jest|vitest|mocha|"
    r"go test|cargo test|rspec|phpunit|mvn test|gradle test|make test|sf apex run test|sfdx force:apex:test:run)\b")
GIT_TIMEOUT = 15
DIFF_MAX_BYTES = 200_000


# -------------------------------------------------------- what Claude Code gets

def claude_args(task: dict[str, Any]) -> list[str]:
    """Extra Claude Code arguments for a task Octopus planned: the agent's
    instructions, and the tools it was NOT given switched off. A task with no
    recorded tools (queued the old way) gets none of this and runs as before.
    A task with pictures also gets read access to the upload folder."""
    args: list[str] = []
    agent = registry.get_agent(task.get("agent_id"))
    # One flag for both: the agent's instructions, then what the user has noted about the project.
    appended = [s for s in ((agent or {}).get("instructions"), db.project_notes_prompt(task.get("project_id"))) if s]
    if appended:
        args += ["--append-system-prompt", "\n\n".join(appended)]
    granted = task.get("tools")
    if isinstance(granted, list):
        allowed = {name for t in granted for name in CLAUDE_TOOLS.get(t, [])}
        if task.get("images"):
            allowed.add("Read")               # it has to be able to open the pictures it was sent
        denied = sorted({name for names in CLAUDE_TOOLS.values() for name in names} - allowed)
        if denied:
            args += ["--disallowedTools", ",".join(denied)]
    if task.get("images"):
        # Pictures live outside the project folder; let it read that one folder. Last, because
        # --add-dir takes a list.
        args += ["--add-dir", str(uploads.UPLOAD_DIR)]
    return args


# ------------------------------------------------------------ watching a run

class RunObserver:
    """Reads Claude Code's message stream for the things worth keeping: the
    plan it is following (TodoWrite) and whether the tests it ran passed."""

    def __init__(self, task_id: int):
        self.task_id = task_id
        self.tests: list[dict[str, Any]] = []
        self._pending_tests: dict[str, str] = {}

    def observe(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        blocks = (msg.get("message") or {}).get("content")
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "tool_use":
                self._tool_use(block)
            elif kind == "user" and block.get("type") == "tool_result":
                self._tool_result(block)

    def _tool_use(self, block: dict[str, Any]) -> None:
        inp = block.get("input") or {}
        if block.get("name") == "TodoWrite" and isinstance(inp.get("todos"), list):
            steps = [{"content": str(t.get("content") or t.get("activeForm") or "")[:200],
                      "status": t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"}
                     for t in inp["todos"] if isinstance(t, dict)]
            db.set_task_plan_steps(self.task_id, steps)
        elif block.get("name") == "Bash":
            cmd = str(inp.get("command") or "")
            if TEST_COMMAND_RE.search(cmd) and block.get("id"):
                self._pending_tests[block["id"]] = cmd.splitlines()[0][:200]

    def _tool_result(self, block: dict[str, Any]) -> None:
        cmd = self._pending_tests.pop(block.get("tool_use_id"), None)
        if not cmd:
            return
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        ok = not block.get("is_error")
        self.tests.append({"command": cmd, "ok": ok, "excerpt": str(content or "").strip()[-400:],
                           "at": datetime.now(timezone.utc).isoformat()})
        db.add_task_event(self.task_id, "note", ("Tests passed: " if ok else "Tests failed: ") + cmd[:120])


# ----------------------------------------------------------------------- git

def _git(path: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def git_snapshot(path: str) -> Optional[dict[str, Any]]:
    """Where the project's git stood when a task started (None: not a git repo)."""
    if not Path(path).is_dir() or (_git(path, "rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return None
    head = (_git(path, "rev-parse", "HEAD") or "").strip() or None
    branch = (_git(path, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
    dirty = [l[3:] for l in (_git(path, "status", "--porcelain") or "").splitlines() if l.strip()]
    return {"branch": branch, "head": head, "started_dirty": len(dirty)}


def _changes(path: str, base: Optional[str]) -> list[dict[str, Any]]:
    """Files changed since `base` (committed or not), with +/- line counts."""
    files: dict[str, dict[str, Any]] = {}
    target = base or "HEAD"
    for line in (_git(path, "diff", "--name-status", target) or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files[parts[-1]] = {"path": parts[-1], "status": parts[0][:1], "additions": 0, "deletions": 0}
    for line in (_git(path, "diff", "--numstat", target) or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] in files:
            files[parts[2]]["additions"] = int(parts[0]) if parts[0].isdigit() else 0
            files[parts[2]]["deletions"] = int(parts[1]) if parts[1].isdigit() else 0
    for line in (_git(path, "status", "--porcelain", "-uall") or "").splitlines():
        if line.startswith("??"):
            name = line[3:].strip('"')
            files.setdefault(name, {"path": name, "status": "A", "additions": _line_count(Path(path) / name), "deletions": 0, "untracked": True})
    return list(files.values())


def _line_count(file: Path, limit: int = 1_000_000) -> int:
    """git doesn't count lines in files it doesn't track yet, so count them."""
    try:
        if file.is_file() and file.stat().st_size <= limit:
            return file.read_bytes().count(b"\n") + (0 if file.stat().st_size == 0 or file.read_bytes().endswith(b"\n") else 1)
    except OSError:
        pass
    return 0


def validation(tests: list[dict[str, Any]], files: list[dict[str, Any]], category: Optional[str]) -> dict[str, str]:
    """A plain verdict on the run: were the changes checked?"""
    last: dict[str, bool] = {}
    for t in tests:
        last[t["command"]] = t["ok"]
    if last and not all(last.values()):
        return {"status": "failed", "note": "The last run of " + ", ".join(c for c, ok in last.items() if not ok)[:120] + " failed."}
    if last:
        return {"status": "passed", "note": f"{len(last)} test command{'s' if len(last) != 1 else ''} passed."}
    if files and category in ("coding", "salesforce"):
        return {"status": "unverified", "note": "Files were changed but no tests were run."}
    return {"status": "n/a", "note": "Nothing to check." if not files else "No test command was run."}


def collect_artifacts(task: dict[str, Any], project_path: str, observer: Optional[RunObserver]) -> dict[str, Any]:
    """What a finished run left behind, saved on the task (db.set_task_artifacts)."""
    tests = observer.tests if observer else []
    snapshot = task.get("git_base") if isinstance(task.get("git_base"), dict) else None
    git: Optional[dict[str, Any]] = None
    files: list[dict[str, Any]] = []
    if snapshot is not None:
        base = snapshot.get("head")
        files = _changes(project_path, base)
        now = git_snapshot(project_path) or {}
        commits = []
        if base and now.get("head") and now["head"] != base:
            commits = [l for l in (_git(project_path, "log", "--format=%h %s", f"{base}..HEAD") or "").splitlines()][:20]
        git = {"branch": now.get("branch"), "base_branch": snapshot.get("branch"), "base": base, "head": now.get("head"),
               "commits": commits, "started_dirty": snapshot.get("started_dirty", 0),
               "uncommitted": bool((_git(project_path, "status", "--porcelain") or "").strip())}
    return {"git": git, "files": files, "tests": tests[-10:], "validation": validation(tests, files, task.get("category"))}


def full_diff(task: dict[str, Any], project_path: str) -> dict[str, Any]:
    """The unified diff of what a task changed (since its starting commit)."""
    snapshot = task.get("git_base")
    if not isinstance(snapshot, dict) or not snapshot.get("head"):
        return {"available": False, "diff": "", "note": "This project isn't a git repository, or the task didn't record a starting point."}
    text = _git(project_path, "diff", snapshot["head"]) or ""
    truncated = len(text.encode()) > DIFF_MAX_BYTES
    if truncated:
        text = text.encode()[:DIFF_MAX_BYTES].decode(errors="ignore")
    return {"available": True, "diff": text, "truncated": truncated, "base": snapshot["head"]}
