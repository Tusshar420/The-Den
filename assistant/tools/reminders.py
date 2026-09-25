"""
Reminders: local, stored in queue.db, fired by the scheduler loop as a push
notification to the phone. No external account involved, so setting one is
allowed by default.
"""

import os
from datetime import timezone

import db
from assistant import timeutil
from assistant.tools import ActionSpec, Prepared, Tool, ToolError, register


def _view(r) -> dict:
    return {"id": r["id"], "text": r["text"], "due": timeutil.parse_iso(r["due_at"]).astimezone(timeutil.local_tz()).isoformat()}


class ReminderTool(Tool):
    id = "reminders"
    label = "Reminders"
    actions = {
        "create": ActionSpec("create", "Set a reminder that pings the owner's phone at a time.",
                             {"text": "what to remind them of", "when": "when, ISO 8601 with offset"}),
        "list": ActionSpec("list", "List pending reminders."),
        "cancel": ActionSpec("cancel", "Cancel a pending reminder.", {"reminder_id": "id from list"}),
    }

    def prepare_create(self, args, ctx):
        try:
            due = timeutil.parse_iso(str(args["when"]))
        except ValueError:
            raise ToolError("'when' must be a date-time like 2026-09-25T09:00:00+05:30")
        if due <= ctx.now():
            raise ToolError("That time has already passed - when should I remind you?")
        return Prepared({"text": args["text"], "due": due.isoformat()}, ["REMINDER_WRITE"], f"reminder {timeutil.fmt_when(due)}",
                        f"Setting a reminder for {timeutil.fmt_when(due)}")

    def run_create(self, args, ctx):
        due = timeutil.parse_iso(args["due"])
        r = db.add_reminder(args["text"], due.astimezone(timezone.utc).isoformat(), (ctx.task or {}).get("id"))
        note = "" if os.environ.get("ACTIVITYSMITH_API_KEY") else (
            "\n(Phone notifications aren't set up - ACTIVITYSMITH_API_KEY isn't set - so this will only appear in the dashboard's Reminders tab.)")
        return {"text": f"I'll remind you on {timeutil.fmt_when(due)}: {args['text']}{note}",
                "data": {"reminder": _view(r)}, "ref": str(r["id"])}

    def prepare_list(self, args, ctx):
        return Prepared({}, ["REMINDER_READ"], "reminders", "Checking your reminders")

    def run_list(self, args, ctx):
        rows = db.list_reminders(["pending"])
        if not rows:
            return {"text": "You have no pending reminders.", "data": {"reminders": []}}
        lines = [f"You have {len(rows)} pending reminder{'s' if len(rows) != 1 else ''}:"]
        lines += [f"• {timeutil.fmt_when(timeutil.parse_iso(r['due_at']).astimezone(timeutil.local_tz()))} — {r['text']}" for r in rows]
        return {"text": "\n".join(lines), "data": {"reminders": [_view(r) for r in rows]}}

    def prepare_cancel(self, args, ctx):
        try:
            rid = int(args["reminder_id"])
        except (TypeError, ValueError):
            raise ToolError("reminder_id must be a number")
        r = db.get_reminder(rid)
        if not r or r["status"] != "pending":
            raise ToolError("There's no pending reminder with that id")
        return Prepared({"reminder_id": rid, "text": r["text"]}, ["REMINDER_WRITE"], f"cancel reminder {rid}",
                        f"Cancelling the reminder “{r['text']}”")

    def run_cancel(self, args, ctx):
        ok = db.cancel_reminder(args["reminder_id"])
        return {"text": f"Cancelled the reminder: {args.get('text', '')}".strip(": ") if ok else "That reminder was already gone.",
                "data": {"cancelled": args["reminder_id"]}}

    def remember(self, action, result, memory):
        data = result.get("data") or {}
        if action == "list":
            memory["reminders"] = data.get("reminders", [])
        elif action == "create" and data.get("reminder"):
            memory["reminders"] = ([data["reminder"]] + memory.get("reminders", []))[:10]


register(ReminderTool())
