"""
Background worker loop: pulls the next queued task, runs Claude Code
non-interactively against that task's project directory, records the
result, and automatically retries after a usage-limit hit.

This is deliberately a single global queue across all active projects
(ordered by priority, then insertion order) rather than one loop per
project - you only have one Claude Code usage allowance, so running
more than one task "at once" wouldn't actually get you more throughput,
just interleaved/confusing output.
"""

import asyncio
import json
import logging
import os
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import db



log = logging.getLogger("claude-queue.worker")

import urllib.request

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

def notify_phone(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning("Failed to send phone notification: %s", e)
# ---------------------------------------------------------------- config

# Path/name of the Claude Code binary. Override with CLAUDE_BIN if it's
# not on PATH for the launchd/login-item environment (launchd agents get
# a minimal PATH - you may need the full path, e.g. /usr/local/bin/claude
# or wherever `which claude` points).
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Extra flags for the Claude Code invocation. `--permission-mode acceptEdits`
# is what lets this run fully unattended for file edits (Edit/Write) - it
# does not cover Bash. A Bash command that needs approval routes through
# approval_server.py instead (see MCP_CONFIG_PATH below), which is the
# actual per-command review mechanism - not --allowedTools. Testing found
# --allowedTools doesn't reliably scope Bash in this CLI's non-interactive
# mode (a pattern that matched nothing still let arbitrary commands,
# including destructive ones, run unattended) - don't reach for it here.
# Flag names have shifted across Claude Code versions - run
# `claude --help` on your Mac and adjust CLAUDE_EXTRA_ARGS below if these
# don't match what you have installed.
#
# Uses shlex so a value with internal spaces survives as one argument
# instead of being split apart.
CLAUDE_EXTRA_ARGS = shlex.split(os.environ.get(
    "CLAUDE_EXTRA_ARGS",
    '--output-format json --permission-mode acceptEdits',
))

# A Bash command acceptEdits/--allowedTools doesn't cover would normally
# just fail with no one to approve it. This routes those requests to
# approval_server.py instead - an MCP stdio server (own venv, see
# .approval-venv/ and the README) that creates a pending row in the
# `approvals` table, notifies your phone, and blocks until the dashboard
# decides it. Each task run gets its own CLAUDE_QUEUE_TASK_ID so the
# server knows which task a given request belongs to (set in run_claude_task).
MCP_CONFIG_PATH = Path(__file__).parent / "mcp-config.json"
PERMISSION_PROMPT_TOOL = "mcp__approval__approval_prompt"

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
TASK_TIMEOUT_SECONDS = int(os.environ.get("TASK_TIMEOUT_SECONDS", str(60 * 60)))  # 1 hour/task cap

# If a rate-limit message doesn't include a parseable reset time, back off
# by this much and try again (then keep doubling, capped) rather than
# hammering the CLI every poll interval.
DEFAULT_RATE_LIMIT_BACKOFF_MINUTES = int(os.environ.get("DEFAULT_RATE_LIMIT_BACKOFF_MINUTES", "60"))
MAX_RATE_LIMIT_BACKOFF_MINUTES = int(os.environ.get("MAX_RATE_LIMIT_BACKOFF_MINUTES", "360"))

RATE_LIMIT_PATTERNS = [
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"limit reached", re.IGNORECASE),
    re.compile(r"try again (later|after)", re.IGNORECASE),
]

# Loose timestamp guesses inside a rate-limit message - covers an ISO
# timestamp, or a "resets at 3:45 PM" style phrase. Best-effort only;
# unmatched cases fall back to the fixed backoff above.
ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
CLOCK_TS_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|AM|PM)?\b")

# escalating backoff state per task id, kept in memory (fine to lose on restart)
_backoff_minutes: dict[int, int] = {}


def _looks_rate_limited(text: str) -> bool:
    return any(p.search(text) for p in RATE_LIMIT_PATTERNS)


def _guess_reset_time(text: str) -> Optional[datetime]:
    m = ISO_TS_RE.search(text)
    if m:
        try:
            ts = m.group(0)
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts)
        except ValueError:
            pass

    m = CLOCK_TS_RE.search(text)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        now = datetime.now().astimezone()
        if ampm and ampm.lower() == "pm" and hour != 12:
            hour += 12
        candidate = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    return None


def _next_backoff(task_id: int) -> datetime:
    minutes = _backoff_minutes.get(task_id, DEFAULT_RATE_LIMIT_BACKOFF_MINUTES)
    resume_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    _backoff_minutes[task_id] = min(minutes * 2, MAX_RATE_LIMIT_BACKOFF_MINUTES)
    return resume_at


class TaskResult:
    def __init__(self, success: bool, rate_limited: bool = False,
                 summary: str = "", error: str = "", resume_at: Optional[datetime] = None,
                 session_id: Optional[str] = None):
        self.success = success
        self.rate_limited = rate_limited
        self.summary = summary
        self.error = error
        self.resume_at = resume_at
        self.session_id = session_id


async def run_claude_task(prompt: str, project_path: str, task_id: int,
                           session_id: Optional[str] = None) -> TaskResult:
    # Resuming an existing session_id (the caller's thread, from
    # db.get_thread_session_id) keeps this task in the same conversation -
    # so "remove the thing you just added" refers to what Claude actually
    # did, not a blank first message. Each task is otherwise its own fresh
    # `claude -p` process with no memory of previous invocations.
    cmd = [CLAUDE_BIN, "-p", prompt]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += CLAUDE_EXTRA_ARGS
    if MCP_CONFIG_PATH.exists():
        cmd += ["--mcp-config", str(MCP_CONFIG_PATH), "--permission-prompt-tool", PERMISSION_PROMPT_TOOL]
    log.info("Running: %s (cwd=%s)", " ".join(cmd[:3]) + " ...", project_path)

    env = {**os.environ, "CLAUDE_QUEUE_TASK_ID": str(task_id)}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return TaskResult(False, error=f"Could not find '{CLAUDE_BIN}' on PATH. Set CLAUDE_BIN to its full path.")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=TASK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return TaskResult(False, error=f"Task exceeded {TASK_TIMEOUT_SECONDS}s and was killed.")

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    combined = stdout + "\n" + stderr

    if _looks_rate_limited(combined):
        reset = _guess_reset_time(combined)
        return TaskResult(False, rate_limited=True, error=combined[-1000:], resume_at=reset)

    # Try structured JSON output first (--output-format json)
    summary = None
    new_session_id = None
    is_error = proc.returncode != 0
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, dict):
            if "result" in parsed:
                summary = str(parsed["result"])[:4000]
            if "is_error" in parsed:
                is_error = bool(parsed["is_error"]) or is_error
            new_session_id = parsed.get("session_id") or None
    except (json.JSONDecodeError, ValueError):
        pass

    if summary is None:
        summary = stdout[-4000:] if stdout.strip() else stderr[-4000:]

    if is_error:
        return TaskResult(False, error=summary or stderr[-2000:], session_id=new_session_id)

    return TaskResult(True, summary=summary, session_id=new_session_id)


async def worker_loop(stop_event: asyncio.Event) -> None:
    log.info("Worker loop started (polling every %ss)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        task = db.get_next_queued_task()
        if not task:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        project = db.get_project(task["project_id"])
        if not project or not project["active"]:
            db.mark_error(task["id"], "Project missing or inactive", needs_review=True)
            continue

        db.mark_running(task["id"])
        log.info("Starting task %s for project '%s'", task["id"], project["name"])

        # A reply (thread_id set) resumes whatever session its thread last
        # ran in; a root task's thread is itself, which has no session yet
        # on its own first run - i.e. it starts a fresh conversation.
        thread_root_id = task["thread_id"] or task["id"]
        resume_session_id = db.get_thread_session_id(thread_root_id)
        result = await run_claude_task(task["prompt"], project["path"], task["id"], resume_session_id)

        if result.session_id:
            db.set_task_session(task["id"], result.session_id)

        if result.rate_limited:
            resume_at = result.resume_at or _next_backoff(task["id"])
            if resume_at.tzinfo is None:
                resume_at = resume_at.replace(tzinfo=timezone.utc)
            db.mark_rate_limited(task["id"], resume_at.isoformat())
            log.warning("Task %s hit a usage limit. Resuming at %s", task["id"], resume_at.isoformat())
            # Sleep until resume time (capped poll so we still notice manual cancels)
            while datetime.now(timezone.utc) < resume_at and not stop_event.is_set():
                await asyncio.sleep(min(POLL_INTERVAL_SECONDS * 6, 300))
            continue

        if result.success:
            _backoff_minutes.pop(task["id"], None)
            db.mark_done(task["id"], result.summary)
            log.info("Task %s done", task["id"])
            notify_phone("Claude task done", f"{project['name']}: {task['prompt'][:150]}")
        else:
            db.mark_error(task["id"], result.error, needs_review=True)
            log.error("Task %s failed: %s", task["id"], result.error[:300])
            notify_phone("Claude task needs review", f"{project['name']}: {task['prompt'][:100]}")
