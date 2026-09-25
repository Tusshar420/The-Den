"""
Background worker loops:

- the agent lane: pulls the next queued task, runs Claude Code
  non-interactively in that task's project directory, records what it does
  step by step, and automatically retries after a usage-limit hit;
- the api lane: plain model calls (OpenAI, Gemini...) that touch no files;
- the scheduler: queues scheduled prompts when they come due.

The agent lane is deliberately a single global queue across all active
projects (in queue_pos order - see db.get_next_queued_task) rather than one
loop per project - you only have one Claude Code usage allowance, so running
more than one task "at once" wouldn't actually get you more throughput,
just interleaved/confusing output.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import db
import providers
import router
import uploads
from assistant import actions as assistant_actions
from assistant import orchestrator, planner
from octopus import execution, registry as octopus_registry

log = logging.getLogger("the-den.worker")

# ------------------------------------------------------------ notifications

ACTIVITYSMITH_API_KEY = os.environ.get("ACTIVITYSMITH_API_KEY")
ACTIVITYSMITH_URL = "https://activitysmith.com/api/push-notification"

# ActivitySmith interruption levels: time-sensitive can break through a
# Focus mode, active is a normal notification, passive arrives quietly.
PUSH_LEVEL = {"approval": "time-sensitive", "reminder": "time-sensitive", "failure": "time-sensitive",
              "completion": "passive", "info": "active"}


def notify_phone(title: str, message: str, kind: str = "info") -> bool:
    if not ACTIVITYSMITH_API_KEY:
        return False
    try:
        body = {"title": title, "message": message, "interruption_level": PUSH_LEVEL.get(kind, "active")}
        req = urllib.request.Request(
            ACTIVITYSMITH_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {ACTIVITYSMITH_API_KEY}", "Content-Type": "application/json",
                     # Cloudflare rejects urllib's default User-Agent (error 1010)
                     "User-Agent": "the-den/1.0"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning("Failed to send phone notification: %s", e)
        return False


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """23:00 to 08:00 local time - see a project's "Quiet hours" setting."""
    h = (now or datetime.now()).hour
    return h >= 23 or h < 8


def _task_name(task: dict) -> str:
    """The chat's current name for a notification - re-read, since the
    namer may have renamed it after the task was picked up."""
    fresh = db.get_task(task["id"]) or task
    return fresh.get("title") or fresh["prompt"][:150]


def notify(kind: str, project_id: Optional[int], title: str, message: str) -> None:
    """A task notification, filtered by that project's Settings toggles."""
    if project_id is not None:
        ps = db.get_project_settings(project_id)
        if kind == "failure" and not ps["notify_failures"]:
            return
        if kind == "completion" and not ps["notify_completions"]:
            return
        if ps["quiet_hours"] and in_quiet_hours():
            return
    notify_phone(title, message, kind)


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
#
# stream-json (which needs --verbose in print mode) reports every tool call
# as it happens - that's the dashboard's live "Running npm test" line - plus
# rate_limit_event messages carrying the real 5-hour / weekly usage. A
# plain `--output-format json` override still works, just without either.
# Flag names have shifted across Claude Code versions - run
# `claude --help` on your Mac and adjust CLAUDE_EXTRA_ARGS if these
# don't match what you have installed.
#
# Uses shlex so a value with internal spaces survives as one argument
# instead of being split apart.
CLAUDE_EXTRA_ARGS = shlex.split(os.environ.get(
    "CLAUDE_EXTRA_ARGS",
    '--output-format stream-json --verbose --permission-mode acceptEdits',
))

# A Bash command acceptEdits/--allowedTools doesn't cover would normally
# just fail with no one to approve it. This routes those requests to
# approval_server.py instead - an MCP stdio server (own venv, see
# .approval-venv/ and the README) that creates a pending row in the
# `approvals` table, notifies your phone, and blocks until the dashboard
# decides it. Each task run gets its own THE_DEN_TASK_ID so the
# server knows which task a given request belongs to (set in run_claude_task).
MCP_CONFIG_PATH = Path(__file__).parent / "mcp-config.json"
PERMISSION_PROMPT_TOOL = "mcp__approval__approval_prompt"

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
TASK_TIMEOUT_SECONDS = int(os.environ.get("TASK_TIMEOUT_SECONDS", str(60 * 60)))  # 1 hour/task cap
SCHEDULER_INTERVAL_SECONDS = 30

# If a rate-limit message doesn't include a parseable reset time, back off
# by this much and try again (then keep doubling, capped) rather than
# hammering the CLI every poll interval.
DEFAULT_RATE_LIMIT_BACKOFF_MINUTES = int(os.environ.get("DEFAULT_RATE_LIMIT_BACKOFF_MINUTES", "60"))
MAX_RATE_LIMIT_BACKOFF_MINUTES = int(os.environ.get("MAX_RATE_LIMIT_BACKOFF_MINUTES", "360"))
SLEEP_RATE_LIMIT_BACKOFF_MINUTES = int(os.environ.get("SLEEP_RATE_LIMIT_BACKOFF_MINUTES", "15"))

# "Save the session for urgent work": Low tasks wait while the 5-hour
# window is above this (see the hold_low_when_busy setting).
HOLD_LOW_ABOVE_UTILIZATION = 0.85

RATE_LIMIT_PATTERNS = [
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"limit reached", re.IGNORECASE),
    re.compile(r"hit your limit", re.IGNORECASE),
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
    if db.sleep_mode_active():
        # No reset time to go on: check again soon and often, so the quota isn't left idle overnight.
        return datetime.now(timezone.utc) + timedelta(minutes=SLEEP_RATE_LIMIT_BACKOFF_MINUTES)
    minutes = _backoff_minutes.get(task_id, DEFAULT_RATE_LIMIT_BACKOFF_MINUTES)
    resume_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    _backoff_minutes[task_id] = min(minutes * 2, MAX_RATE_LIMIT_BACKOFF_MINUTES)
    return resume_at


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ------------------------------------------------------------ usage windows

def _epoch_iso(v: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(v), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def record_claude_usage(info: dict[str, Any]) -> None:
    """Keep the latest 5-hour / weekly utilization Claude Code reported
    (rate_limit_event), for the dashboard's usage bars."""
    windows = {}
    for name, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict):
            windows[name] = {"utilization": w.get("utilization"), "resets_at": _epoch_iso(w.get("resetsAt"))}
    db.set_setting("claude_usage", {
        "status": info.get("status"),
        "limited_window": info.get("rateLimitType"),
        "resets_at": _epoch_iso(info.get("resetsAt")),
        "windows": windows,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def _hold_low_priority() -> bool:
    if not db.get_setting("hold_low_when_busy") or db.sleep_mode_active():
        return False  # asleep there's no urgent work to save the session for
    w = ((db.get_setting("claude_usage") or {}).get("windows") or {}).get("five_hour") or {}
    util, resets = w.get("utilization"), w.get("resets_at")
    if util is None or not resets:
        return False
    return util > HOLD_LOW_ABOVE_UTILIZATION and _parse_iso(resets) > datetime.now(timezone.utc)


# --------------------------------------------------- weekly limit early warning

WEEK_SECONDS = 7 * 86400
# Below this much of the week gone, "pace" is too noisy to project from.
PACE_MIN_ELAPSED = 0.10
# Ranked lowest to highest; a level only alerts once per week and only escalates.
WEEKLY_LEVELS = ("pace", "high", "critical")


def weekly_pace(usage: Optional[dict[str, Any]], now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """How the weekly (seven_day) window is going, and whether it deserves a
    warning. Judged on pace, not just the raw number: 80% on day 6 is fine,
    45% on day 2 is not. projected = usage / share of the week gone, i.e.
    where the week ends if the rest of it looks like so far.

      pace      at least 25% used and on course to end 10%+ over the limit
      high      80%+ used, or 50%+ used and on course to end 30%+ over
      critical  95%+ used

    None when there's no reading or the window has already reset."""
    now = now or datetime.now(timezone.utc)
    w = ((usage or {}).get("windows") or {}).get("seven_day") or {}
    util, resets = w.get("utilization"), w.get("resets_at")
    if util is None or not resets:
        return None
    resets_at = _parse_iso(resets)
    left = (resets_at - now).total_seconds()
    if left <= 0:
        return None
    elapsed = min(1.0, max(0.0, 1 - left / WEEK_SECONDS))
    projected = util / elapsed if elapsed >= PACE_MIN_ELAPSED else None
    level = None
    if util >= 0.95:
        level = "critical"
    elif util >= 0.80 or (projected is not None and util >= 0.50 and projected >= 1.30):
        level = "high"
    elif projected is not None and util >= 0.25 and projected >= 1.10:
        level = "pace"
    runs_out_at = None
    if level and 0 < util < 1 and elapsed > 0:
        eta = now + timedelta(seconds=(1 - util) / util * elapsed * WEEK_SECONDS)
        if eta < resets_at:
            runs_out_at = eta
    return {
        "level": level, "pct": round(util * 100), "elapsed_pct": round(elapsed * 100),
        "projected_pct": round(projected * 100) if projected is not None else None,
        "runs_out_at": runs_out_at.isoformat() if runs_out_at else None,
        "resets_at": resets_at.isoformat(),
    }


def _span(seconds: float) -> str:
    h = int(seconds // 3600)
    return f"{h // 24}d {h % 24}h" if h >= 24 else f"{max(h, 1)}h"


def weekly_alert_text(p: dict[str, Any]) -> tuple[str, str]:
    """(title, message) for a weekly_pace result that has a level."""
    used = f"{p['pct']}% of the weekly limit used, {p['elapsed_pct']}% of the week gone."
    if p["level"] == "critical":
        title = "Weekly limit almost gone"
    elif p["level"] == "high":
        title = "Weekly limit running high"
    else:
        title = "Weekly limit: on pace to run out"
    resets_at = _parse_iso(p["resets_at"])
    if p["runs_out_at"]:
        out = _parse_iso(p["runs_out_at"])
        early = _span((resets_at - out).total_seconds())
        return title, f"{used} At this pace it runs out {out.astimezone():%a %H:%M}, {early} before it resets. Ease off or save it for what matters."
    return title, f"{used} It resets {resets_at.astimezone():%a %H:%M}."


def check_weekly_pace(now: Optional[datetime] = None) -> bool:
    """Push a phone alert when the weekly window crosses into a worse level.
    Once per level per week; held back through quiet hours (it isn't marked
    sent, so it goes out on the next reading after they end)."""
    if not db.get_setting("notify_weekly_pace"):
        return False
    p = weekly_pace(db.get_setting("claude_usage"), now)
    if not p or not p["level"]:
        return False
    sent = db.get_setting("weekly_alert_sent") or {}
    same_week = bool(sent.get("resets_at")) and abs(
        (_parse_iso(sent["resets_at"]) - _parse_iso(p["resets_at"])).total_seconds()) < 3600
    if same_week and WEEKLY_LEVELS.index(sent["level"]) >= WEEKLY_LEVELS.index(p["level"]):
        return False
    if in_quiet_hours(now.astimezone() if now else None):
        return False
    title, message = weekly_alert_text(p)
    if notify_phone(title, message, "reminder"):
        db.set_setting("weekly_alert_sent", {"resets_at": p["resets_at"], "level": p["level"]})
        return True
    return False


# ---------------------------------------------------------- progress lines

def _rel(path: str, root: str) -> str:
    try:
        if path and root and os.path.isabs(path) and path.startswith(root.rstrip("/") + "/"):
            return os.path.relpath(path, root)
    except ValueError:
        pass
    return path


def describe_tool(name: str, inp: dict[str, Any], root: str) -> Optional[str]:
    """One human line per tool call ("Running npm test"), or None to skip."""
    def s(key: str) -> str:
        v = inp.get(key)
        return str(v).strip() if v is not None else ""

    if name == "Bash":
        cmd = s("command").splitlines()[0] if s("command") else ""
        return "Running " + cmd[:160] if cmd else "Running a command"
    if name == "Read":
        return "Reading " + _rel(s("file_path"), root)
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "Editing " + _rel(s("file_path") or s("notebook_path"), root)
    if name == "Write":
        return "Writing " + _rel(s("file_path"), root)
    if name == "Glob":
        return "Looking for files matching " + s("pattern")
    if name == "Grep":
        return "Searching for " + s("pattern")[:120]
    if name == "LS":
        return "Listing " + (_rel(s("path"), root) or "the project")
    if name == "WebFetch":
        return "Fetching " + s("url")[:160]
    if name == "WebSearch":
        return "Searching the web for " + s("query")[:120]
    if name in ("Task", "Agent"):
        return "Delegating: " + (s("description") or "a subtask")[:120]
    if name == "TodoWrite":
        return "Updating its plan"
    if name.startswith("mcp__approval"):
        return None  # the approval server logs its own, more useful line
    if name.startswith("mcp__"):
        return "Using " + name.split("__")[-1]
    return "Using " + name


# ------------------------------------------------------------- agent lane

class TaskResult:
    def __init__(self, success: bool, rate_limited: bool = False,
                 summary: str = "", error: str = "", resume_at: Optional[datetime] = None,
                 session_id: Optional[str] = None, usage: Optional[dict] = None, stopped: bool = False):
        self.success = success
        self.rate_limited = rate_limited
        self.summary = summary
        self.error = error
        self.resume_at = resume_at
        self.session_id = session_id
        # duration_ms / input_tokens / output_tokens / cost_usd, whichever
        # the run reported - see db.set_task_usage.
        self.usage = usage or {}
        self.stopped = stopped


# The agent lane's live process, so the dashboard's Stop can reach it (the
# API runs in this same process - see main.py).
_running: dict[int, asyncio.subprocess.Process] = {}
_stopped: set[int] = set()


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Claude Code runs in its own process group (start_new_session), so this
    also takes down whatever it launched - a test run, the approval server."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


_frozen: set[int] = set()  # running tasks whose process is SIGSTOPped


def _signal_tree(proc: asyncio.subprocess.Process, sig: int) -> bool:
    try:
        os.killpg(proc.pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def pause_task(task_id: int) -> bool:
    """Freeze a running Claude Code task (and anything it launched) where it
    is; resume_task picks it up again exactly there."""
    proc = _running.get(task_id)
    if not proc or not _signal_tree(proc, signal.SIGSTOP):
        return False
    _frozen.add(task_id)
    return True


def resume_task(task_id: int) -> bool:
    proc = _running.get(task_id)
    _frozen.discard(task_id)
    return bool(proc) and _signal_tree(proc, signal.SIGCONT)


def stop_task(task_id: int) -> bool:
    proc = _running.get(task_id)
    if not proc:
        return False
    _stopped.add(task_id)
    _kill_tree(proc)
    return True


# ------------------------------------------------------------ restart (the dashboard's Restart button)
# Exiting mid-run would orphan Claude Code: it runs in its own process group, so it
# outlives this process, and the restarted worker would start the same task again
# beside it. So a restart drains first - no lane picks up new work - and exits once
# nothing is running. launchd (KeepAlive) starts the service again with fresh code.
draining = False
_restart_task: Optional[asyncio.Task] = None


def busy_task_ids() -> list[int]:
    """What a restart is waiting on: agent processes this worker owns, plus
    api / assistant calls in flight."""
    return sorted(set(_running) | set(db.running_task_ids(("api", "assistant"))))


async def _restart_when_idle() -> None:
    while busy_task_ids():
        await asyncio.sleep(2)
    log.warning("Restarting (asked from the dashboard) - launchd starts the service again")
    await asyncio.sleep(0.5)  # let the API answer whoever is polling
    os._exit(0)


def request_restart() -> list[int]:
    """Stop taking new work and restart as soon as nothing is running. Must be
    called on the event loop (the API's async route). Returns what it waits on."""
    global draining, _restart_task
    draining = True
    if _restart_task is None:
        _restart_task = asyncio.get_running_loop().create_task(_restart_when_idle())
    return busy_task_ids()


def _usage_from_result(parsed: dict[str, Any]) -> dict[str, Any]:
    # total_cost_usd is Claude Code's API-price estimate - on a
    # subscription it's what the run *would* have cost, still a
    # useful yardstick for comparing against the api lane.
    u = parsed.get("usage") or {}
    return {
        "duration_ms": parsed.get("duration_ms"),
        "input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                        + (u.get("cache_creation_input_tokens") or 0) if u else None,
        "output_tokens": u.get("output_tokens"),
        "cost_usd": parsed.get("total_cost_usd"),
    }


def _with_permission_mode(args: list[str], mode: Optional[str]) -> list[str]:
    """CLAUDE_EXTRA_ARGS with --permission-mode set to this task's mode
    (a task with none keeps whatever the args already say)."""
    if mode not in db.PERMISSION_MODES:
        return args
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == "--permission-mode":
            skip = True
        elif not a.startswith("--permission-mode="):
            out.append(a)
    return out + ["--permission-mode", mode]


async def run_claude_task(prompt: str, project_path: str, task_id: int,
                          session_id: Optional[str] = None, model: Optional[str] = None,
                          permission_mode: Optional[str] = None, extra_args: Optional[list[str]] = None,
                          observer: Optional[execution.RunObserver] = None) -> TaskResult:
    # Resuming an existing session_id (the caller's thread, from
    # db.get_thread_session_id) keeps this task in the same conversation -
    # so "remove the thing you just added" refers to what Claude actually
    # did, not a blank first message. Each task is otherwise its own fresh
    # `claude -p` process with no memory of previous invocations.
    cmd = [CLAUDE_BIN, "-p", prompt]
    if session_id:
        cmd += ["--resume", session_id]
    if model:
        cmd += ["--model", model]
    cmd += _with_permission_mode(CLAUDE_EXTRA_ARGS, permission_mode)
    if MCP_CONFIG_PATH.exists():
        cmd += ["--mcp-config", str(MCP_CONFIG_PATH), "--permission-prompt-tool", PERMISSION_PROMPT_TOOL]
    cmd += extra_args or []   # the agent's instructions and the tools it was not given (octopus/execution.py)
    log.info("Running: %s (cwd=%s)", " ".join(cmd[:3]) + " ...", project_path)

    env = {**os.environ, "THE_DEN_TASK_ID": str(task_id)}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project_path,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # A tool result line can carry a whole file; the default 64 KiB
            # line limit would abort the read.
            limit=32 * 1024 * 1024,
            start_new_session=True,
        )
    except FileNotFoundError:
        return TaskResult(False, error=f"Could not find '{CLAUDE_BIN}' on PATH. Set CLAUDE_BIN to its full path.")

    _running[task_id] = proc
    result_msg: Optional[dict[str, Any]] = None
    rejected_reset: Optional[datetime] = None
    loose_lines: list[str] = []  # non-JSON stdout (e.g. an old CLI's plain text)

    async def read_stdout() -> None:
        nonlocal result_msg, rejected_reset
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                loose_lines.append(line[-2000:])
                del loose_lines[:-40]
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if observer:
                observer.observe(msg)
            if kind == "assistant":
                for block in (msg.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        text = describe_tool(block.get("name", ""), block.get("input") or {}, project_path)
                        if text:
                            db.add_task_event(task_id, "tool", text)
            elif kind == "rate_limit_event":
                info = msg.get("rate_limit_info") or {}
                record_claude_usage(info)
                check_weekly_pace()
                if info.get("status") == "rejected" and info.get("resetsAt"):
                    iso = _epoch_iso(info["resetsAt"])
                    rejected_reset = _parse_iso(iso) if iso else None
            elif kind == "result":
                result_msg = msg

    try:
        stdout_task = asyncio.ensure_future(read_stdout())
        stderr_task = asyncio.ensure_future(proc.stderr.read())  # type: ignore[union-attr]
        # The time cap counts only time spent running, not time paused.
        finished = asyncio.ensure_future(asyncio.gather(stdout_task, stderr_task, proc.wait()))
        active = 0.0
        while not finished.done():
            await asyncio.wait({finished}, timeout=1)
            if task_id not in _frozen:
                active += 1
            if active > TASK_TIMEOUT_SECONDS and not finished.done():
                finished.cancel()
                raise asyncio.TimeoutError
        await finished
        stderr = stderr_task.result().decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        _kill_tree(proc)
        await proc.wait()
        db.expire_pending_approvals(task_id)
        return TaskResult(False, error=f"Task exceeded {TASK_TIMEOUT_SECONDS}s and was killed.")
    finally:
        _running.pop(task_id, None)
        _frozen.discard(task_id)

    if task_id in _stopped:
        _stopped.discard(task_id)
        db.expire_pending_approvals(task_id)
        return TaskResult(False, stopped=True, error="Stopped from the dashboard")

    if result_msg is None and loose_lines:
        # --output-format json prints one object; plain text prints lines.
        try:
            parsed = json.loads("\n".join(loose_lines))
            if isinstance(parsed, dict):
                result_msg = parsed
        except ValueError:
            pass

    summary = None
    new_session_id = None
    usage: dict = {}
    is_error = proc.returncode != 0 or result_msg is None
    if result_msg:
        if "result" in result_msg:
            summary = str(result_msg["result"])[:4000]
        is_error = bool(result_msg.get("is_error")) or is_error
        new_session_id = result_msg.get("session_id") or None
        usage = _usage_from_result(result_msg)

    tail = "\n".join(loose_lines)[-4000:]
    if summary is None:
        summary = tail if tail.strip() else stderr[-4000:]

    # Only a *failed* run can be a rate limit - a successful summary that
    # happens to say "added rate limiting" is just a summary.
    if is_error:
        if rejected_reset or _looks_rate_limited(f"{summary}\n{stderr}\n{tail}"):
            reset = rejected_reset or _guess_reset_time(f"{summary}\n{stderr}")
            return TaskResult(False, rate_limited=True, error=(summary or stderr)[-1000:], resume_at=reset,
                              session_id=new_session_id)
        return TaskResult(False, error=summary or stderr[-2000:] or f"Claude Code exited with {proc.returncode}",
                          session_id=new_session_id, usage=usage)

    return TaskResult(True, summary=summary, session_id=new_session_id, usage=usage)


def over_budget(project: dict[str, Any]) -> bool:
    """Pause a project whose real (api lane) spend this month reached its
    budget, and say so - "At 100% I'll pause this project and ask"."""
    budget = db.get_project_settings(project["id"])["budget_usd"]
    if not budget or budget <= 0:
        return False
    spent = db.project_month_spend(project["id"])
    if spent < budget:
        return False
    db.set_project_active(project["id"], False)
    log.warning("Project '%s' reached its $%.2f budget ($%.2f spent) - paused", project["name"], budget, spent)
    notify_phone("Budget reached · paused " + project["name"],
                 f"${spent:.2f} of ${budget:.2f} spent this month. Resume it from the dashboard to keep going.",
                 "failure")
    return True


CATCH_UP_MAX_CHARS = 24000


def _transcript(messages: list[dict[str, Any]], max_chars: int, default_label: str = "Assistant") -> str:
    """Prompts and answers as plain 'User: ... / <model>: ...' text, newest kept if it has to be cut."""
    lines: list[str] = []
    for prev in messages:
        lines.append("User: " + prev["prompt"])
        if prev["status"] == "done" and prev["result_summary"]:
            who = (router.get_model(prev.get("model_id")) or {}).get("label") or default_label
            lines.append(f"{who}: {prev['result_summary']}")
    out = "\n\n".join(lines)
    return out if len(out) <= max_chars else "(earlier messages left out)\n\n..." + out[-max_chars:]


def catch_up_prompt(task: dict[str, Any], root_id: int, session_id: Optional[str]) -> str:
    """What to put before a Claude Code reply so it knows the whole chat. Its own
    session already holds what it did itself; anything answered by another
    model since (or all of it, if the chat began on another model and there's no
    session yet) is spelled out here. Empty when the session is up to date."""
    if session_id and task.get("session_id") == session_id:
        return ""                          # a run resumed after a usage limit - it has seen all of this
    history = db.get_thread_history(root_id, task["id"])
    if session_id:
        last_seen = max((i for i, h in enumerate(history) if h.get("session_id") == session_id), default=-1)
        history = history[last_seen + 1:]
    if not history:
        return ""
    return ("This conversation so far, from before you were brought in (other models answered these):\n\n"
            + _transcript(history, CATCH_UP_MAX_CHARS) + "\n\n---\nThe user's new message:\n")


async def run_agent_task(task: dict[str, Any]) -> bool:
    """Run one agent-lane task start to finish. Used by the queue (one at a
    time) and by "Proceed now" (alongside it). True if it hit the usage
    limit - it's then rate_limited, and the queue resumes it after the reset."""
    project = db.get_project(task["project_id"])
    if not project or not project["active"]:
        db.mark_error(task["id"], "Project missing or inactive", needs_review=False)
        return False
    if over_budget(project):
        return False

    resuming = task["status"] == "rate_limited"
    if not db.claim_task(task["id"]):
        return False  # already started elsewhere, or cancelled meanwhile
    log.info("Starting task %s for project '%s'", task["id"], project["name"])
    if resuming and db.get_setting("notify_on_resume"):
        db.set_setting("notify_on_resume", False)
        notify_phone("Octopus is back", f"Usage limit reset · resuming {_task_name(task)}")

    # A reply (thread_id set) resumes whatever session its thread last
    # ran in; a root task's thread is itself, which has no session yet
    # on its own first run - i.e. it starts a fresh conversation.
    thread_root_id = task["thread_id"] or task["id"]
    resume_session_id = db.get_thread_session_id(thread_root_id)
    model_cfg = router.get_model(task.get("model_id") or "claude-code") or {}
    started = datetime.now(timezone.utc)
    # Where git stood before this run, so what it changed can be shown afterwards.
    # A resumed (rate-limited) task keeps its original starting point.
    if task.get("git_base") is None:
        snapshot = await asyncio.to_thread(execution.git_snapshot, project["path"])
        if snapshot is not None:
            db.set_task_git_base(task["id"], snapshot)
            task = {**task, "git_base": snapshot}
    observer = execution.RunObserver(task["id"])
    prompt = catch_up_prompt(task, thread_root_id, resume_session_id) + task["prompt"]
    result = await run_claude_task(uploads.prompt_for_agent(prompt, task.get("images")), project["path"], task["id"], resume_session_id,
                                   model=model_cfg.get("model"), permission_mode=task.get("permission_mode"),
                                   extra_args=execution.claude_args(task), observer=observer)
    try:
        artifacts = await asyncio.to_thread(execution.collect_artifacts, task, project["path"], observer)
        db.set_task_artifacts(task["id"], artifacts)
        if artifacts["validation"]["status"] in ("passed", "failed", "unverified"):
            db.add_task_event(task["id"], "note", "Checked: " + artifacts["validation"]["note"])
    except Exception:  # what a run left behind is a bonus - never fail the task over it
        log.exception("Could not collect artifacts for task %s", task["id"])

    if result.session_id:
        db.set_task_session(task["id"], result.session_id)
    if not result.rate_limited:
        usage = dict(result.usage)
        if not usage.get("duration_ms"):
            usage["duration_ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        db.set_task_usage(task["id"], **usage)

    if result.stopped:
        db.mark_stopped(task["id"])
        log.info("Task %s stopped from the dashboard", task["id"])
        return False

    if result.rate_limited:
        resume_at = result.resume_at or _next_backoff(task["id"])
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=timezone.utc)
        db.mark_rate_limited(task["id"], resume_at.isoformat())
        log.warning("Task %s hit a usage limit. Resuming at %s", task["id"], resume_at.isoformat())
        return True

    if result.success:
        _backoff_minutes.pop(task["id"], None)
        db.mark_done(task["id"], result.summary)
        log.info("Task %s done", task["id"])
        notify("completion", project["id"], "Done · " + project["name"], _task_name(task))
    else:
        db.mark_error(task["id"], result.error, needs_review=False)
        log.error("Task %s failed: %s", task["id"], result.error[:300])
        notify("failure", project["id"], "Failed · " + project["name"], _task_name(task))
    return False


async def worker_loop(stop_event: asyncio.Event) -> None:
    """The agent lane - Claude Code running inside the project, one task at
    a time in queue order."""
    log.info("Worker loop started (polling every %ss)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        task = None if draining else db.get_next_queued_task("agent", hold_low_priority=_hold_low_priority())
        if not task:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        if not await run_agent_task(task):
            continue
        # Usage limit: it's account-wide, so the whole agent lane waits. Re-read
        # the task each time: "Retry now" or Cancel on the dashboard ends the
        # wait early.
        while not stop_event.is_set():
            current = db.get_task(task["id"])
            if not current or current["status"] != "rate_limited" or not current["resume_at"]:
                break
            remaining = (_parse_iso(current["resume_at"]) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


RUN_NOW_POLL_SECONDS = 2
_run_now_active: set = set()  # asyncio tasks started by run_now_loop, kept referenced until done


async def run_now_loop(stop_event: asyncio.Event) -> None:
    """"Proceed now": start those tasks straight away, next to whatever the
    queue is running. A chat still never runs two messages at once."""
    log.info("Proceed-now loop started")
    while not stop_event.is_set():
        try:
            for task in ([] if draining else db.run_now_tasks()):
                if task["lane"] != "agent":
                    continue
                t = asyncio.create_task(run_agent_task(task))
                _run_now_active.add(t)
                t.add_done_callback(_run_now_active.discard)
        except Exception:
            log.exception("Proceed-now tick failed")
        await asyncio.sleep(RUN_NOW_POLL_SECONDS)


# ------------------------------------------------------------------ api lane
# Plain model calls (OpenAI etc.) routed here by Octopus. They touch no
# project files, so they run alongside the agent lane instead of waiting behind a
# long coding task - "explain this function" shouldn't queue for an hour.

API_TASK_TIMEOUT_SECONDS = int(os.environ.get("API_TASK_TIMEOUT_SECONDS", "300"))
API_RESULT_MAX_CHARS = 20000

API_INSTRUCTIONS = """You are Octopus, Tushar's personal AI assistant. Answer directly and clearly.
You are answering through a chat on a dashboard, not inside a project: you cannot read the user's files,
run commands or change anything. If the request truly needs that, say so briefly so the user can resend
it as a coding task."""


def _api_messages(task: dict, model_cfg: Optional[dict] = None) -> list[dict]:
    """The chat so far plus this message. Earlier answers may have come from other
    models (the chat can switch), so they're all replayed as this model's own turns.
    When model_cfg has a known context size, the oldest turns are dropped to fit."""
    root_id = task["thread_id"] or task["id"]
    def user_message(t: dict) -> dict:
        # "images" (upload ids) is only present when there are some; providers turn it into image parts.
        return {"role": "user", "content": t["prompt"], **({"images": t["images"]} if t.get("images") else {})}

    turns: list[list[dict]] = []
    for prev in db.get_thread_history(root_id, task["id"]):
        turn = [user_message(prev)]
        if prev["status"] == "done" and prev["result_summary"]:
            turn.append({"role": "assistant", "content": prev["result_summary"]})
        turns.append(turn)
    # Leave room for the answer: ~4 characters a token, and use at most 60% of the window for the past.
    budget = int((model_cfg or {}).get("context_tokens", 0) * 4 * 0.6)
    if budget:
        used = len(task["prompt"])
        kept: list[list[dict]] = []
        for turn in reversed(turns):
            used += sum(len(m["content"]) for m in turn)
            if used > budget:
                break
            kept.append(turn)
        turns = kept[::-1]
    return [m for turn in turns for m in turn] + [user_message(task)]


async def api_worker_loop(stop_event: asyncio.Event) -> None:
    log.info("API lane started (polling every %ss)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            limited = tuple(m["id"] for m in router.registry().get("models", [])
                            if router.lane_for(m) == "api" and db.model_rate_limited_until(m["id"]))
        except Exception as e:  # a half-saved models.json shouldn't kill the lane
            log.error("Could not read models.json: %s", e)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        task = None if draining else db.get_next_queued_task("api", exclude_models=limited)
        if not task:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        project = db.get_project(task["project_id"])
        model_cfg = router.get_model(task.get("model_id"))
        if not model_cfg:
            db.mark_error(task["id"], f"Model '{task.get('model_id')}' is no longer in models.json", needs_review=False)
            continue
        if project and over_budget(project):
            continue

        db.mark_running(task["id"])
        log.info("API lane: task %s -> %s", task["id"], model_cfg["id"])
        started = datetime.now(timezone.utc)
        try:
            # The agent Octopus planned this for supplies its own instructions (agents.json).
            agent = octopus_registry.get_agent(task.get("agent_id"))
            instructions = (agent or {}).get("instructions") or API_INSTRUCTIONS
            if project and not db.is_general_project(project):
                instructions += f"\nThe user currently has the project '{project['name']}' selected."
            notes = db.project_notes_prompt(task["project_id"])
            if notes:
                instructions += "\n\n" + notes
            if model_cfg.get("modality") == "image":
                completion = await asyncio.to_thread(providers.generate_image, model_cfg, task["prompt"], API_TASK_TIMEOUT_SECONDS)
            else:
                extra = {"web": True} if "web" in (task.get("tools") or []) else {}
                completion = await asyncio.to_thread(
                    providers.complete, model_cfg, _api_messages(task, model_cfg), instructions, API_TASK_TIMEOUT_SECONDS, **extra)
        except providers.ProviderError as e:
            if e.rate_limited:
                resume_at = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after or 60)
                db.set_model_rate_limited(model_cfg["id"], resume_at.isoformat(), str(e))
                db.mark_rate_limited(task["id"], resume_at.isoformat())
                log.warning("API lane: %s rate-limited until %s", model_cfg["id"], resume_at.isoformat())
            else:
                db.mark_error(task["id"], str(e), needs_review=False)
                log.error("API lane: task %s failed: %s", task["id"], e)
                notify("failure", task["project_id"], "Octopus couldn't answer",
                       f"{model_cfg.get('label')}: {_task_name(task)}")
            continue
        except Exception as e:
            db.mark_error(task["id"], f"Unexpected error: {e}", needs_review=False)
            log.exception("API lane: task %s crashed", task["id"])
            continue

        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        db.clear_model_rate_limit(model_cfg["id"])
        db.set_task_usage(task["id"], duration_ms, completion.input_tokens, completion.output_tokens,
                          providers.estimate_cost(model_cfg, completion.input_tokens, completion.output_tokens))
        db.mark_done(task["id"], completion.text[:API_RESULT_MAX_CHARS] or "(empty response)")
        log.info("API lane: task %s done in %sms", task["id"], duration_ms)
        notify("completion", task["project_id"], "Octopus answered", f"{model_cfg.get('label')}: {_task_name(task)}")


# ------------------------------------------------------------ assistant lane
# Personal-assistant requests (email, calendar, reminders) routed here by
# Octopus. They talk to connected accounts, not project files, so they run
# alongside the other lanes and answer in seconds. What each request may do
# is decided in assistant/actions.py - this loop only runs them.

ASSISTANT_POLL_SECONDS = float(os.environ.get("ASSISTANT_POLL_SECONDS", "2"))
ASSISTANT_TASK_TIMEOUT_SECONDS = int(os.environ.get("ASSISTANT_TASK_TIMEOUT_SECONDS", "300"))


def _assistant_notifier(task: dict[str, Any]):
    """Push an approval request to the phone, honouring the project's
    'approval' notification setting like approval_server.py does."""
    def notify_approval(title: str, message: str) -> None:
        if db.get_project_settings(task["project_id"])["notify_approvals"]:
            notify_phone(title, message[:200], "approval")
    return notify_approval


async def assistant_worker_loop(stop_event: asyncio.Event) -> None:
    log.info("Assistant lane started (polling every %ss)", ASSISTANT_POLL_SECONDS)
    while not stop_event.is_set():
        task = None if draining else db.get_next_queued_task("assistant")
        if not task:
            await asyncio.sleep(ASSISTANT_POLL_SECONDS)
            continue

        db.mark_running(task["id"])
        log.info("Assistant lane: task %s", task["id"])
        try:
            out = await asyncio.wait_for(
                asyncio.to_thread(orchestrator.run_task, task, _assistant_notifier(task)),
                timeout=ASSISTANT_TASK_TIMEOUT_SECONDS)
        except planner.PlannerUnavailable as e:
            if e.rate_limited:
                resume_at = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after or 300)
                if e.model_id and e.model_id != "claude-code":  # claude-code's limit is derived from its tasks
                    db.set_model_rate_limited(e.model_id, resume_at.isoformat(), str(e))
                db.mark_rate_limited(task["id"], resume_at.isoformat())
                log.warning("Assistant lane: task %s waiting until %s (%s)", task["id"], resume_at.isoformat(), e)
            else:
                db.mark_error(task["id"], str(e), needs_review=False)
            continue
        except asyncio.TimeoutError:
            db.mark_error(task["id"], f"Timed out after {ASSISTANT_TASK_TIMEOUT_SECONDS}s", needs_review=False)
            continue
        except Exception as e:
            db.mark_error(task["id"], f"Unexpected error: {e}", needs_review=False)
            log.exception("Assistant lane: task %s crashed", task["id"])
            notify("failure", task["project_id"], "Octopus couldn't finish a request", task["prompt"][:100])
            continue

        db.set_task_usage(task["id"], out.duration_ms, out.input_tokens, out.output_tokens, out.cost_usd)
        db.mark_done(task["id"], out.text[:API_RESULT_MAX_CHARS] or "(done)")
        log.info("Assistant lane: task %s done in %sms%s", task["id"], out.duration_ms,
                 f" ({len(out.approvals)} approval(s) requested)" if out.approvals else "")


def fire_due_reminders() -> int:
    """Push each reminder that has come due. A reminder is marked fired first,
    so a crash below can't make it ring every tick."""
    fired = 0
    for r in db.due_reminders():
        db.mark_reminder_fired(r["id"])
        notify_phone("Reminder", r["text"], "reminder")
        fired += 1
    return fired


# ----------------------------------------------------------------- scheduler

def queue_prompt(project: dict[str, Any], prompt: str, priority: int = 0, model: str = "auto",
                 routed_by: str = "schedule") -> Optional[dict[str, Any]]:
    """Route a prompt like the Ask box does and queue it. None if no model can take it."""
    try:
        decision = router.route(prompt, project_name=None if db.is_general_project(project) else project["name"],
                                force_model=model)
    except ValueError as e:
        log.warning("Could not queue '%s' for %s: %s", prompt[:60], project["name"], e)
        return None
    return db.add_task(project["id"], prompt, priority, None, lane=decision["lane"],
                       model_id=decision["model_id"], category=decision["category"],
                       route_reason=decision["reason"], routed_by=routed_by)


def fire_schedule(sc: dict[str, Any]) -> int:
    # Advance first, so a crash below can't make it fire again every tick.
    db.mark_schedule_fired(sc["id"])
    if sc["project_id"]:
        project = db.get_project(sc["project_id"])
        targets = [project] if project else []
    else:
        targets = [p for p in db.list_projects(active_only=True) if not db.is_general_project(p)]
    queued = sum(1 for p in targets if queue_prompt(p, sc["prompt"], sc["priority"], sc["model"]))
    log.info("Schedule %s ('%s') fired: queued %s task(s)", sc["id"], sc["label"], queued)
    return queued


async def scheduler_loop(stop_event: asyncio.Event) -> None:
    log.info("Scheduler started (checking every %ss)", SCHEDULER_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            for sc in db.due_schedules():
                await asyncio.to_thread(fire_schedule, sc)  # routing may call the classifier model
            await asyncio.to_thread(fire_due_reminders)
            await asyncio.to_thread(assistant_actions.expire_stale)
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
