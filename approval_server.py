#!/usr/bin/env python3
"""
MCP stdio server used as Claude Code's --permission-prompt-tool for
worker.py's unattended tasks.

When a queued task wants to run something that acceptEdits doesn't cover
(e.g. `sf project deploy start`), Claude Code would normally have no one
to ask - this server is what it calls instead. It writes a pending row to
the queue's `approvals` table, pushes a phone notification, then blocks
(polling the DB) until the dashboard's Approve/Deny button decides it, or
APPROVAL_TIMEOUT_SECONDS passes - an unanswered request auto-denies so it
doesn't stall the rest of the queue forever.

A Bash command covered by the project's "Always allowed commands" (see
risk.match_allowed) is allowed straight away, without a row or a push.

Runs under its own Python 3.12 + `mcp` package venv (.approval-venv) -
`mcp` requires >=3.10, but the rest of this project targets whatever
Python has Claude Code on this Mac, which is the 3.9 that ships with the
Xcode Command Line Tools.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import risk  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

TASK_ID = int(os.environ["CLAUDE_QUEUE_TASK_ID"])
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", str(60 * 60)))
POLL_SECONDS = 3

mcp = FastMCP("claude-queue-approval")


def notify_phone(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        return
    try:
        # Priority 5 arrives as time-sensitive on iOS, so it can break
        # through a Focus mode - an approval blocks the whole queue.
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": "warning", "Priority": "5"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # best-effort - a failed notification shouldn't block the approval flow


@mcp.tool()
def approval_prompt(tool_name: str, input: dict, tool_use_id: str = "") -> dict:
    """Ask a human, via the claude-queue dashboard, to approve or deny a tool call."""
    command = str(input.get("command", json.dumps(input)))
    task = db.get_task(TASK_ID)
    project = db.get_project(task["project_id"]) if task else None
    settings = db.get_project_settings(project["id"]) if project else None

    if tool_name == "Bash" and settings:
        entry = risk.match_allowed(command, settings["allow_commands"])
        if entry:
            db.add_task_event(TASK_ID, "approval", f"Allowed without asking ({entry} is always allowed)")
            return {"behavior": "allow", "updatedInput": input}

    approval = db.create_approval(TASK_ID, tool_name, json.dumps(input))
    db.add_task_event(TASK_ID, "approval", "Waiting for your OK: " + command[:160])
    if settings is None or settings["notify_approvals"]:
        notify_phone(
            "Approval needed",
            f"{project['name'] if project else 'task #' + str(TASK_ID)}: {command[:150]}",
        )

    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        current = db.get_approval(approval["id"])
        if current["status"] == "approved":
            db.add_task_event(TASK_ID, "approval", "You approved · resuming")
            return {"behavior": "allow", "updatedInput": input}
        if current["status"] == "denied":
            note = (current.get("note") or "").strip()
            db.add_task_event(TASK_ID, "approval", "You denied" + (f': "{note}"' if note else ""))
            message = "Denied from the claude-queue dashboard."
            if note:
                message += f" The user said: {note}"
            return {"behavior": "deny", "message": message}
        if current["status"] == "timed_out":
            break  # the task was stopped from the dashboard

    db.timeout_approval(approval["id"])
    return {"behavior": "deny", "message": "No response within the approval window; denied automatically."}


if __name__ == "__main__":
    mcp.run()
