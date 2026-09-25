"""
Octopus's own workspace as a tool: the queue, tasks, projects and backlogs the
dashboard shows, made available to the personal assistant so "what's stuck in my
queue?" or "add that to the KodeAura backlog" can be answered and done from the
same chat as email and calendar.

It reads and writes through the same db layer the dashboard and the instant
commands use (octopus/control.py) and adds nothing they can't already do. What
it holds back:

- Only active projects. An archived or paused project's tasks and backlog are not
  visible here, the same as in the queue the worker runs.
- Personal-assistant chats. Their prompts and answers are your email and calendar
  content, so a task from that lane is listed by status only - never its text.
- Approvals. They can be counted and described, never decided: the approval is the
  human's gate on what the assistant does and no tool may answer it.

Everything a task, a chat title or a backlog item says was written by someone or
something else (a model, an email that was handed off to the coding agent), so
those reads are marked untrusted like an email body: after one, cancelling a task
or adding to a backlog can no longer run without asking (capabilities.effective).
"""

from typing import Any, Optional

import db
from assistant import timeutil
from assistant.tools import ActionSpec, Prepared, Tool, ToolError, preview, register

STATUSES = ("queued", "running", "rate_limited", "needs_review", "error", "done", "cancelled")
MAX_LIST = 15
TEXT_FOR_MODEL = 2500
TODO_CHARS = 500


def real_projects() -> list[dict[str, Any]]:
    """The projects this tool may see: active, and not the General scratch space."""
    return [p for p in db.list_projects(active_only=True) if not db.is_general_project(p)]


def find_project(name: Optional[str]) -> dict[str, Any]:
    """The one registered project this name means, or a ToolError listing the choices."""
    projects = real_projects()
    wanted = (name or "").strip().lower()
    if not wanted and len(projects) == 1:
        return projects[0]
    exact = [p for p in projects if p["name"].lower() == wanted]
    loose = exact or [p for p in projects if wanted and (wanted in p["name"].lower() or p["name"].lower() in wanted)]
    if len(loose) == 1:
        return loose[0]
    names = ", ".join(p["name"] for p in projects) or "none registered"
    raise ToolError(f"I couldn't tell which project you mean by '{name or ''}'. Registered projects: {names}.")


def _visible_task(task_id: Any) -> dict[str, Any]:
    """A task the owner could see in the queue, or a ToolError that doesn't say whether it exists elsewhere."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise ToolError("task_id must be a number")
    task = db.get_task(tid)
    project = db.get_project(task["project_id"]) if task else None
    if not task or not project or not project["active"]:
        raise ToolError(f"I can't find task #{tid} in your active projects.")
    return {**task, "project_name": project["name"], "project_is_general": db.is_general_project(project)}


def _private(task: dict[str, Any]) -> bool:
    return task.get("lane") == "assistant"


def _name(task: dict[str, Any]) -> str:
    if _private(task):
        return "Personal Assistant chat"
    return " ".join((task.get("title") or task.get("prompt") or "").split())[:80] or f"task {task['id']}"


def _where(task: dict[str, Any]) -> str:
    general = task.get("project_is_general", task.get("project_name") == db.GENERAL_PROJECT_NAME)
    return "" if general or not task.get("project_name") else f" in {task['project_name']}"


def _when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return timeutil.fmt_when(timeutil.parse_iso(iso).astimezone(timeutil.local_tz()))
    except ValueError:
        return ""


def _brief(task: dict[str, Any]) -> dict[str, Any]:
    out = {"id": task["id"], "title": _name(task), "status": task["status"], "project": task.get("project_name"),
           "lane": task.get("lane"), "model": task.get("model_id"), "created": task.get("created_at"),
           "finished": task.get("finished_at")}
    if task.get("paused"):
        out["paused"] = True
    if task.get("current_step") and not _private(task):
        out["step"] = task["current_step"]
    return out


def _line(task: dict[str, Any]) -> str:
    bits = [task["status"].replace("_", " ")]
    if task.get("model_id") and not _private(task):
        bits.append(task["model_id"])
    when = _when(task.get("finished_at") or task.get("created_at"))
    return f"#{task['id']} '{_name(task)}'{_where(task)} - {', '.join(bits)}" + (f" ({when})" if when else "")


def _visible(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = {p["id"]: p for p in db.list_projects(active_only=True)}
    return [{**t, "project_name": active[t["project_id"]]["name"], "project_is_general": db.is_general_project(active[t["project_id"]])}
            for t in rows if t["project_id"] in active]


class WorkspaceTool(Tool):
    id = "workspace"
    label = "Octopus workspace"
    actions = {
        "queue": ActionSpec("queue", "What Octopus is doing right now: what is running, what is waiting in the queue, "
                            "and how many approvals are waiting for the owner.", reads_untrusted=True),
        "tasks": ActionSpec("tasks", "List recent tasks (newest first), optionally only one status or one project.",
                            {"status?": "queued | running | rate_limited | needs_review | error | done | cancelled",
                             "project?": "project name", "limit?": f"how many, up to {MAX_LIST}"}, reads_untrusted=True),
        "task": ActionSpec("task", "Details of one task, including its answer or error.", {"task_id": "id from queue or tasks"},
                           reads_untrusted=True),
        "projects": ActionSpec("projects", "The owner's registered projects, with how many backlog items each has."),
        "backlog": ActionSpec("backlog", "The open to-do items in one project's backlog.", {"project": "project name"},
                              reads_untrusted=True),
        "add_todo": ActionSpec("add_todo", "Add an item to a project's backlog. Nothing runs until the owner queues it.",
                               {"text": "the to-do item", "project?": "project name (may be left out when there is only one)"}),
        "cancel_task": ActionSpec("cancel_task", "Cancel a task that is waiting, or stop one that is running.",
                                  {"task_id": "id from queue or tasks"}),
        "usage": ActionSpec("usage", "What the AI models have been used for and cost over the last 30 days."),
    }

    # ---------------------------------------------------------------- reads

    def prepare_queue(self, args, ctx):
        return Prepared({}, ["PROJECT_READ"], "queue", "Checking the queue")

    def run_queue(self, args, ctx):
        status = db.queue_status()
        running = _visible(status.get("running_all") or [])
        queued = _visible(db.list_tasks(statuses=["queued"]))
        limited = _visible([status["earliest_rate_limit_resume"]] if status.get("earliest_rate_limit_resume") else [])
        active = {p["id"] for p in db.list_projects(active_only=True)}
        approvals = [a for a in db.list_pending_approvals() if a["project_id"] in active]
        lines = []
        for t in running:
            step = "" if _private(t) or not t.get("current_step") else f" - {t['current_step']}"
            lines.append(f"Running: #{t['id']} '{_name(t)}'{_where(t)}{step}")
        if not running:
            lines.append("Nothing is running right now.")
        if limited:
            lines.append(f"Waiting for a usage limit to reset: #{limited[0]['id']} '{_name(limited[0])}'{_where(limited[0])}")
        if queued:
            lines.append(f"{len(queued)} waiting in the queue:")
            lines += [f"  {i}. #{t['id']} '{_name(t)}'{_where(t)}" for i, t in enumerate(queued[:MAX_LIST], 1)]
            if len(queued) > MAX_LIST:
                lines.append(f"  … and {len(queued) - MAX_LIST} more")
        if approvals:
            lines.append(f"{len(approvals)} approval{'s' if len(approvals) != 1 else ''} waiting for you on the dashboard.")
        if not (running or queued or approvals or limited):
            lines.append("The queue is empty.")
        data = {"running": [_brief(t) for t in running], "queued": [_brief(t) for t in queued[:MAX_LIST]],
                "queued_total": len(queued), "rate_limited": [_brief(t) for t in limited],
                "pending_approvals": [{"id": a["id"], "task_id": a["task_id"], "kind": "assistant" if str(a["tool_name"]).startswith("assistant:") else "coding"}
                                      for a in approvals]}
        return {"text": "\n".join(lines), "data": data}

    def prepare_tasks(self, args, ctx):
        if args.get("status") and args["status"] not in STATUSES:
            raise ToolError(f"status must be one of: {', '.join(STATUSES)}")
        project = find_project(args["project"]) if args.get("project") else None
        try:
            limit = max(1, min(int(args.get("limit") or 10), MAX_LIST))
        except (TypeError, ValueError):
            raise ToolError("limit must be a number")
        return Prepared({"status": args.get("status"), "project_id": project["id"] if project else None,
                         "project": project["name"] if project else None, "limit": limit}, ["PROJECT_READ"],
                        "tasks", "Looking through recent tasks")

    def run_tasks(self, args, ctx):
        rows = db.list_all_tasks(limit=200, statuses=[args["status"]] if args.get("status") else None,
                                 project_id=args.get("project_id"))
        tasks = _visible(rows)[:args["limit"]]
        if not tasks:
            return {"text": "No matching tasks.", "data": {"tasks": []}}
        head = f"{len(tasks)} recent task{'s' if len(tasks) != 1 else ''}" + (f" ({args['status']})" if args.get("status") else "") + \
               (f" in {args['project']}" if args.get("project") else "") + ":"
        return {"text": "\n".join([head] + [f"• {_line(t)}" for t in tasks]), "data": {"tasks": [_brief(t) for t in tasks]}}

    def prepare_task(self, args, ctx):
        t = _visible_task(args["task_id"])
        return Prepared({"task_id": t["id"]}, ["PROJECT_READ"], f"task {t['id']}", f"Opening task #{t['id']}")

    def run_task(self, args, ctx):
        t = _visible_task(args["task_id"])
        if _private(t):
            text = f"#{t['id']} is a Personal Assistant chat ({t['status'].replace('_', ' ')}). I don't reopen those from here."
            return {"text": text, "data": _brief(t)}
        answer = (t.get("result_summary") or "")[:TEXT_FOR_MODEL]
        error = (t.get("error_message") or "")[:TEXT_FOR_MODEL]
        lines = [_line(t), f"Request: {' '.join((t.get('prompt') or '').split())[:600]}"]
        if answer:
            lines.append(f"Result: {answer[:1200]}")
        if error:
            lines.append(f"Error: {error[:600]}")
        if t.get("cost_usd") is not None:
            lines.append(f"Cost: about ${t['cost_usd']:.4f}")
        return {"text": "\n".join(lines), "data": {**_brief(t), "prompt": (t.get("prompt") or "")[:TEXT_FOR_MODEL],
                                                    "result": answer, "error": error, "cost_usd": t.get("cost_usd")}}

    def prepare_projects(self, args, ctx):
        return Prepared({}, ["PROJECT_READ"], "projects", "Listing your projects")

    def run_projects(self, args, ctx):
        projects = real_projects()
        if not projects:
            return {"text": "No projects are registered yet.", "data": {"projects": []}}
        out = [{"id": p["id"], "name": p["name"], "category": p.get("category"),
                "open_todos": len([t for t in db.list_todos(p["id"]) if not t.get("checked")])} for p in projects]
        lines = [f"You have {len(out)} project{'s' if len(out) != 1 else ''}:"] + [
            f"• {p['name']}" + (f" - {p['open_todos']} to-do{'s' if p['open_todos'] != 1 else ''} in the backlog" if p["open_todos"] else "")
            for p in out]
        return {"text": "\n".join(lines), "data": {"projects": out}}

    def prepare_backlog(self, args, ctx):
        p = find_project(args["project"])
        return Prepared({"project_id": p["id"], "project": p["name"]}, ["PROJECT_READ"], f"backlog: {p['name']}",
                        f"Reading {p['name']}'s backlog")

    def run_backlog(self, args, ctx):
        todos = [t for t in db.list_todos(args["project_id"]) if not t.get("checked")]
        if not todos:
            return {"text": f"{args['project']}'s backlog is empty.", "data": {"todos": []}}
        lines = [f"{args['project']} has {len(todos)} item{'s' if len(todos) != 1 else ''} in its backlog:"]
        lines += [f"• {t['text']}" for t in todos[:MAX_LIST]]
        return {"text": "\n".join(lines), "data": {"todos": [{"id": t["id"], "text": t["text"]} for t in todos[:MAX_LIST]]}}

    def prepare_usage(self, args, ctx):
        return Prepared({}, ["PROJECT_READ"], "usage", "Checking model usage")

    def run_usage(self, args, ctx):
        u = db.usage_summary(30)
        rows = [r for r in u["by_model"] if r["key"] != "unknown"]
        total = sum(r["cost_usd"] or 0 for r in rows)
        if not rows:
            return {"text": "No model usage in the last 30 days.", "data": {"by_model": [], "total_cost_usd": 0}}
        lines = [f"Last 30 days: about ${total:.2f} across {sum(r['tasks'] for r in rows)} tasks."]
        lines += [f"• {r['key']}: {r['tasks']} task{'s' if r['tasks'] != 1 else ''}" + (f", ${r['cost_usd']:.2f}" if r["cost_usd"] else "")
                  for r in rows[:8]]
        return {"text": "\n".join(lines), "data": {"by_model": [{"model": r["key"], "tasks": r["tasks"], "done": r["done"],
                                                                    "failed": r["failed"], "cost_usd": r["cost_usd"]} for r in rows],
                                                    "total_cost_usd": round(total, 4)}}

    # --------------------------------------------------------------- writes

    def prepare_add_todo(self, args, ctx):
        p = find_project(args.get("project"))
        text = " ".join(str(args["text"]).split())[:TODO_CHARS]
        return Prepared({"project_id": p["id"], "project": p["name"], "text": text}, ["TODO_WRITE"], f"{p['name']}: {text[:60]}",
                        f"Add “{text[:60]}” to {p['name']}'s backlog",
                        preview("workspace", "BACKLOG APPROVAL", "Add to backlog", [("Project", p["name"]), ("Item", text)],
                                "Add", blocks=("Item",)))

    def run_add_todo(self, args, ctx):
        project = db.get_project(args["project_id"])
        if not project or not project["active"]:
            raise ToolError(f"{args['project']} isn't an active project any more.")
        todo = db.add_todo(args["project_id"], args["text"])
        return {"text": f"Added “{args['text']}” to {args['project']}'s backlog. Nothing runs until you queue it.",
                "data": {"todo_id": todo["id"]}, "ref": str(todo["id"])}

    def _cancellable(self, task_id: Any, ctx) -> dict[str, Any]:
        t = _visible_task(task_id)
        if ctx.task and ctx.task["id"] == t["id"]:
            raise ToolError("That's this chat's own task - I can't cancel it while answering you.")
        if t["status"] in ("queued", "rate_limited") or (t["status"] == "running" and t.get("lane") == "agent"):
            return t
        if t["status"] == "running":
            raise ToolError(f"#{t['id']} is running in a lane that can't be interrupted; it will finish in a moment.")
        raise ToolError(f"#{t['id']} is already {t['status'].replace('_', ' ')} - there's nothing to cancel.")

    def prepare_cancel_task(self, args, ctx):
        t = self._cancellable(args["task_id"], ctx)
        verb = "Stop" if t["status"] == "running" else "Cancel"
        return Prepared({"task_id": t["id"], "title": _name(t)}, ["TASK_CONTROL"], f"{verb.lower()} task {t['id']}",
                        f"{verb} task #{t['id']} '{_name(t)}'{_where(t)}",
                        preview("workspace", "TASK APPROVAL", f"{verb} task", [("Task", f"#{t['id']} {_name(t)}"),
                                                                              ("Project", t["project_name"]),
                                                                              ("State", t["status"].replace("_", " "))],
                                verb))

    def run_cancel_task(self, args, ctx):
        t = self._cancellable(args["task_id"], ctx)  # it may have moved on while the approval waited
        if t["status"] == "running":
            import worker  # lazy: worker imports the assistant package
            if not worker.stop_task(t["id"]):
                raise ToolError(f"#{t['id']} isn't running under Octopus any more.")
            return {"text": f"Stopped #{t['id']} '{_name(t)}'.", "data": {"task_id": t["id"], "status": "stopped"}, "ref": str(t["id"])}
        db.cancel_task(t["id"])
        if db.get_task(t["id"])["status"] != "cancelled":
            raise ToolError(f"#{t['id']} started before I could cancel it.")
        return {"text": f"Cancelled #{t['id']} '{_name(t)}' - it hadn't started.", "data": {"task_id": t["id"], "status": "cancelled"},
                "ref": str(t["id"])}

    # --------------------------------------------------------------- memory

    def remember(self, action, result, memory):
        data = result.get("data") or {}
        listed = data.get("tasks") if action == "tasks" else (data.get("running", []) + data.get("queued", [])) if action == "queue" else None
        if listed:
            memory["tasks"] = [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in listed[:10]]


register(WorkspaceTool())
