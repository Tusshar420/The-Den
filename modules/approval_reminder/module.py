"""
Approval reminder: when something has waited for your OK for `minutes` (config, default 10)
with no answer, one email about it - the project (also in the subject), the task, what it
wants to do, the queue, and Approve / Deny buttons. The buttons are the same signed one-tap
links as the approval push (approval_links.py), so they work on any device on Tailscale.

Checks about once a minute on its own thread. Only pending approvals are looked at, so
approved, denied, timed-out and cancelled ones are skipped; each gets `times` emails (default 1), `minutes` apart.
Follows the email switch for "approval_reminder" (Settings > Email, the setting
"email_events_off") and holds while "I'm going to sleep" is on, like the approval push.

A module, so a change here needs POST /modules/approval_reminder/reload, never a restart.
"""

import html
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import approval_links
import db
import email_templates
import mailer
import risk
from assistant.providers.google import GmailProvider

EVENT = "approval_reminder"
CHECK_SECONDS = 60
QUEUED = ["queued", "rate_limited"]

# Registered into email_templates.EVENTS while the module runs, so it has an on/off switch
# like every other email (GET/PATCH /email).
SPEC = {
    "kind": "warn",
    "subject": "Still waiting for your OK · {project}: {task}",
    "headline": "{project} is waiting for your OK",
    "lines": [
        "This has been waiting {waited} with no answer.",
        "- Project: {project}",
        "- Task: {task_line}",
        "- Wants to: {action}",
        "- Asked at: {asked_at}",
        "- Task status: {task_status}",
        "- Queue: {queue}",
        "{links_note}",
    ],
}

STATUS_WORDS = {"running": "Running - paused until you answer",
                "done": "Finished - the action waits for your OK"}


def waited(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes // 60} h {minutes % 60} min"


def action_of(approval: dict[str, Any]) -> str:
    """What it asks for, in words: an assistant or recipe action's own description, else the
    command (or tool and target) it wants to run."""
    try:
        payload = json.loads(approval["tool_input"])
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and payload.get("describe"):
        return str(payload["describe"])[:300]
    return risk.command_of(approval["tool_name"], approval["tool_input"])[:300]


def fields_for(a: dict[str, Any], task: dict[str, Any], now: datetime) -> dict[str, Any]:
    asked = datetime.fromisoformat(a["created_at"])
    project = db.get_project(task["project_id"]) or {}
    here = len(db.list_all_tasks(limit=10000, statuses=QUEUED, project_id=task["project_id"]))
    everywhere = len(db.list_all_tasks(limit=10000, statuses=QUEUED))
    name = (task.get("title") or task.get("prompt") or "")[:120]
    status = STATUS_WORDS.get(task["status"], task["status"])
    if task.get("current_step") and task["status"] == "running":
        status += f" (last step: {task['current_step'][:120]})"
    base, signed = approval_links.den_url(), bool(os.environ.get("API_KEY"))
    return {
        "project": project.get("name") or f"Project #{task['project_id']}",
        "task": name,
        "task_line": f"#{task['id']} {name}" + (f" · {task['model_id']}" if task.get("model_id") else ""),
        "action": action_of(a),
        "asked_at": asked.astimezone().strftime("%d %b, %H:%M"),
        "waited": waited((now - asked).total_seconds()),
        "task_status": status,
        "queue": (f"{everywhere} task(s) waiting to start, {here} of them in this project" if everywhere
                  else "Nothing else is waiting to start"),
        "links_note": "The buttons work on any device on your Tailscale network." if base
                      else "Open the dashboard to answer it.",
        "buttons": ([("Approve", approval_links.decide_url(a["id"], "approve"), "good"),
                     ("Deny", approval_links.decide_url(a["id"], "deny"), "bad")] if base and signed else [])
                   + ([("Open dashboard", f"{base}/dashboard", "info")] if base else []),
    }


def render(fields: dict[str, Any]) -> tuple[str, str, str]:
    """The shared email layout, with the buttons under it (plain text gets the links)."""
    subject, plain, page = email_templates.render(EVENT, fields)
    buttons = fields.get("buttons") or []
    if buttons:
        plain += "\n\n" + "\n".join(f"{label}: {url}" for label, url, _ in buttons)
        links = "".join(
            f'<a href="{html.escape(url)}" style="display:inline-block;margin:0 8px 8px 0;padding:10px 18px;'
            f'border-radius:8px;background:{email_templates.KINDS[kind]};color:#fff;font-weight:600;'
            f'text-decoration:none;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{html.escape(label)}</a>'
            for label, url, kind in buttons)
        page += f'<div style="max-width:520px;margin:14px auto 0">{links}</div>'
    return subject, plain, page


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        email_templates.EVENTS[EVENT] = SPEC
        ctx.add_cleanup(lambda: email_templates.EVENTS.pop(EVENT, None))

    def start(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="den-approval-reminder")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=30)

    def _loop(self):
        while not self._stop.wait(CHECK_SECONDS):
            try:
                self.check()
            except Exception:
                self.ctx.log.exception("Approval reminder check failed")

    def check(self, now: datetime = None) -> int:
        """Email each approval that has waited long enough and hasn't had its email. Marked
        before sending, so a failed send can't repeat every minute."""
        if not mailer.enabled(EVENT) or not mailer.configured() or db.sleep_mode_active():
            return 0
        now = now or datetime.now(timezone.utc)
        gap = timedelta(minutes=max(1, int(self.ctx.config["minutes"])))
        times = max(1, int(self.ctx.config.get("times", 1)))
        pending = db.list_pending_approvals()
        ids = {a["id"] for a in pending}
        old = self.ctx.get_state("reminded", [])
        if isinstance(old, list):   # first version kept a plain list: one email each
            old = {str(i): 1 for i in old}
        counts = {k: v for k, v in old.items() if int(k) in ids}   # forget decided ones
        sent = 0
        for a in pending:
            n = counts.get(str(a["id"]), 0)
            if n >= times or datetime.fromisoformat(a["created_at"]) + gap * (n + 1) > now:
                continue
            task = db.get_task(a["task_id"])
            if not task or task["status"] in ("cancelled", "deleted"):
                continue
            counts[str(a["id"])] = n + 1
            self.ctx.set_state("reminded", counts)
            if self.send(render(fields_for(a, task, now))):
                sent += 1
        self.ctx.set_state("reminded", counts)
        return sent

    def send(self, email: tuple[str, str, str]) -> bool:
        subject, plain, page = email
        try:
            GmailProvider(mailer.sender() or "google").send([mailer.recipient()], f"[The Den] {subject}", plain, html=page)
        except Exception as e:   # a mail problem must never stop the next check
            self.ctx.log.warning("Approval reminder not sent: %s", e)
            return False
        self.ctx.log.info("Approval reminder sent: %s", subject)
        return True
