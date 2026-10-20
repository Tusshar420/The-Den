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
import approval_links  # noqa: E402
import db  # noqa: E402
import risk  # noqa: E402
import webpush  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

# CLAUDE_QUEUE_TASK_ID: pre-rename name, still set by a worker that predates the rename
TASK_ID = int(os.environ.get("THE_DEN_TASK_ID") or os.environ["CLAUDE_QUEUE_TASK_ID"])
ACTIVITYSMITH_API_KEY = os.environ.get("ACTIVITYSMITH_API_KEY")
TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", str(60 * 60)))
POLL_SECONDS = 3
# While "I'm going to sleep" is on, nobody is there to answer: don't wake the phone,
# and give up quickly so the queue moves on to work that doesn't need a yes.
SLEEP_TIMEOUT_SECONDS = int(os.environ.get("SLEEP_APPROVAL_TIMEOUT_SECONDS", str(5 * 60)))

mcp = FastMCP("the-den-approval")


def notify_phone(title: str, message: str, approval_id: int) -> None:
    # The Dock / Home Screen web app too (webpush), with its own Approve / Deny.
    webpush.send_all(title, message, "approval", "/dashboard", approval_id)
    if not ACTIVITYSMITH_API_KEY:
        return
    try:
        # time-sensitive can break through a Focus mode - an approval
        # blocks the whole queue.
        body = {"title": title, "message": message, "interruption_level": "time-sensitive"}
        buttons = approval_links.actions(approval_id)
        if buttons:
            body["actions"] = buttons
            body["redirection"] = approval_links.den_url() + "/dashboard"
        req = urllib.request.Request(
            "https://activitysmith.com/api/push-notification",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {ACTIVITYSMITH_API_KEY}", "Content-Type": "application/json",
                     # Cloudflare rejects urllib's default User-Agent (error 1010)
                     "User-Agent": "the-den/1.0"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # best-effort - a failed notification shouldn't block the approval flow


@mcp.tool()
def approval_prompt(tool_name: str, input: dict, tool_use_id: str = "") -> dict:
    """Ask a human, via The Den dashboard, to approve or deny a tool call."""
    command = str(input.get("command", json.dumps(input)))
    task = db.get_task(TASK_ID)
    project = db.get_project(task["project_id"]) if task else None
    settings = db.get_project_settings(project["id"]) if project else None

    if tool_name == "Bash" and settings:
        entry = risk.match_allowed(command, settings["allow_commands"])
        if entry:
            db.add_task_event(TASK_ID, "approval", f"Allowed without asking ({entry} is always allowed)")
            return {"behavior": "allow", "updatedInput": input}

    asleep = db.sleep_mode_active()
    approval = db.create_approval(TASK_ID, tool_name, json.dumps(input))
    db.add_task_event(TASK_ID, "approval", "Waiting for your OK: " + command[:160])
    if not asleep and (settings is None or settings["notify_approvals"]):
        notify_phone(
            "Approval needed",
            f"{project['name'] if project else 'task #' + str(TASK_ID)}: {command[:150]}",
            approval["id"],
        )

    quick = asleep and db.get_setting("sleep_auto_deny", True)   # Settings › Safety
    deadline = time.time() + (min(TIMEOUT_SECONDS, SLEEP_TIMEOUT_SECONDS) if quick else TIMEOUT_SECONDS)
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        current = db.get_approval(approval["id"])
        if current["status"] == "approved":
            db.add_task_event(TASK_ID, "approval", "You approved · resuming")
            return {"behavior": "allow", "updatedInput": input}
        if current["status"] == "denied":
            note = (current.get("note") or "").strip()
            db.add_task_event(TASK_ID, "approval", "You denied" + (f': "{note}"' if note else ""))
            message = "Denied from The Den dashboard."
            if note:
                message += f" The user said: {note}"
            return {"behavior": "deny", "message": message}
        if current["status"] == "timed_out":
            break  # the task was stopped from the dashboard

    db.timeout_approval(approval["id"])
    if asleep:
        db.add_task_event(TASK_ID, "approval", "You were asleep · denied automatically")
        return {"behavior": "deny", "message": "The user is asleep, so this was denied automatically. Do not retry it: "
                "carry on with the parts of the task that don't need it, and list what you skipped in your final summary."}
    return {"behavior": "deny", "message": "No response within the approval window; denied automatically."}


if __name__ == "__main__":
    mcp.run()
