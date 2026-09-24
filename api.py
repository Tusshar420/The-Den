"""
Minimal local HTTP API in front of the queue - this is what iOS Shortcuts
(and anything else on your Tailscale network) talks to.

Auth is a single shared API key via the `X-API-Key` header. Tailscale
already keeps this off the open internet, but the key stops any other
device on your tailnet from poking your queue without you noticing.
"""

import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field

import db
import providers
import risk
import router
import worker

DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"
LEGACY_DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard_legacy.html"
STATIC_DIR = Path(__file__).parent / "static"

API_KEY = os.environ.get("API_KEY")  # set this in your environment / launchd plist

app = FastAPI(title="claude-queue")


class NoCacheStaticFiles(StaticFiles):
    """The dashboard's modules change whenever this repo does, and iOS
    Safari will happily keep serving a stale app.js for days - make every
    load revalidate (cheap: an unchanged file is a 304)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not API_KEY:
        # No key configured - fine for local testing, but you should set
        # API_KEY before exposing this over Tailscale to your other devices.
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


class ProjectIn(BaseModel):
    name: str
    path: str


class ProjectActiveIn(BaseModel):
    active: bool


class TaskIn(BaseModel):
    prompt: str
    priority: int = 0
    # Set this to an existing task's id to reply within its chat instead of
    # starting a new one - that task's thread then resumes the same Claude
    # Code session instead of starting from a blank conversation.
    thread_id: Optional[int] = None


class OctopusIn(BaseModel):
    # "text" is what the redesigned dashboard sends; "request" is kept for
    # anything already calling this (iOS Shortcuts etc.).
    request: str = Field(validation_alias=AliasChoices("request", "text"))
    # Omit (or null) to use the built-in "General" project - fine for
    # questions that don't belong to any repo.
    project_id: Optional[int] = None
    # "auto" lets the router decide; a models.json id forces that model.
    model: str = "auto"
    priority: int = 0
    # Image attachments aren't supported yet - accepted and ignored so the
    # dashboard's request shape doesn't need to change once they are.
    images: Optional[list[str]] = None


class ApprovalDecisionIn(BaseModel):
    approved: bool
    # Optional reason when denying - passed back to Claude.
    note: Optional[str] = None
    # Approve and add this command to the project's always-allowed list.
    always: bool = False


class TodoIn(BaseModel):
    text: str


class TodoUpdateIn(BaseModel):
    text: Optional[str] = None
    checked: Optional[bool] = None


class TodoReorderIn(BaseModel):
    ids: list[int]


@app.get("/health")
def health():
    return {"ok": True}


def _with_position(task: dict) -> dict:
    return {**task, "position": db.queue_position(task["id"])}


@app.get("/projects", dependencies=[Depends(require_api_key)])
def list_projects():
    return [{**p, "is_general": db.is_general_project(p)} for p in db.list_projects()]


@app.post("/projects", dependencies=[Depends(require_api_key)])
def create_project(body: ProjectIn):
    if db.get_project_by_name(body.name):
        raise HTTPException(status_code=409, detail=f"Project '{body.name}' already exists")
    return db.add_project(body.name, body.path)


@app.patch("/projects/{project_id}", dependencies=[Depends(require_api_key)])
def update_project(project_id: int, body: ProjectActiveIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    db.set_project_active(project_id, body.active)
    return db.get_project(project_id)


@app.get("/projects/{project_id}/tasks", dependencies=[Depends(require_api_key)])
def list_project_tasks(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_tasks(project_id=project_id)


@app.post("/projects/{project_id}/tasks", dependencies=[Depends(require_api_key)])
def create_task(project_id: int, body: TaskIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    thread_id = body.thread_id
    if thread_id is not None:
        root = db.get_task(thread_id)
        if not root or root["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="thread_id does not refer to a task in this project")
        # Normalize to the actual root, in case the caller passed a reply's
        # id rather than the first message's (the dashboard always resolves
        # this itself, but other API clients might not).
        thread_id = root["thread_id"] or root["id"]
        # A reply stays on whatever model its conversation started with -
        # switching models mid-thread would lose the session/context.
        thread_root = db.get_task(thread_id)
        return _with_position(db.add_task(project_id, body.prompt, body.priority, thread_id,
                                          lane=thread_root.get("lane") or "agent", model_id=thread_root.get("model_id"),
                                          category=thread_root.get("category"), routed_by="thread",
                                          route_reason="reply - same model as the rest of this chat"))

    return _with_position(db.add_task(project_id, body.prompt, body.priority, thread_id, routed_by="direct",
                                      route_reason="sent straight to Claude Code"))


@app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_task(task_id: int):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    db.cancel_task(task_id)
    return db.get_task(task_id)


@app.get("/queue/status", dependencies=[Depends(require_api_key)])
def queue_status():
    return db.queue_status()


@app.get("/tasks", dependencies=[Depends(require_api_key)])
def list_all_tasks(limit: int = 200, status: Optional[str] = None, project_id: Optional[int] = None):
    statuses = status.split(",") if status else None
    return db.list_all_tasks(limit=limit, statuses=statuses, project_id=project_id)


@app.get("/approvals", dependencies=[Depends(require_api_key)])
def list_pending_approvals():
    return db.list_pending_approvals()


@app.post("/approvals/{approval_id}/decide", dependencies=[Depends(require_api_key)])
def decide_approval(approval_id: int, body: ApprovalDecisionIn):
    approval = db.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if body.approved and body.always:
        entry = risk.allow_entry_for(risk.command_of(approval["tool_name"], approval["tool_input"])) \
            if approval["tool_name"] == "Bash" else None
        if not entry:
            raise HTTPException(status_code=400, detail="This command can't be always-allowed (it chains or redirects other commands)")
        task = db.get_task(approval["task_id"])
        db.add_allowed_command(task["project_id"], entry)
    return db.decide_approval(approval_id, body.approved, note=None if body.approved else body.note)


@app.get("/approvals/{approval_id}/context", dependencies=[Depends(require_api_key)])
def approval_context(approval_id: int):
    """Risk read, the steps so far and the project's uncommitted changes -
    everything the approval card and detail pane show besides the command."""
    approval = db.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    task = db.get_task(approval["task_id"])
    project = db.get_project(task["project_id"]) if task else None
    events = db.list_task_events([approval["task_id"]]).get(approval["task_id"], [])
    allow = db.get_project_settings(project["id"])["allow_commands"] if project else []
    return risk.approval_context(approval, project, events, allow)


@app.get("/projects/{project_id}/todos", dependencies=[Depends(require_api_key)])
def list_todos(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_todos(project_id)


@app.post("/projects/{project_id}/todos", dependencies=[Depends(require_api_key)])
def create_todo(project_id: int, body: TodoIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.add_todo(project_id, body.text)


@app.patch("/todos/{todo_id}", dependencies=[Depends(require_api_key)])
def update_todo(todo_id: int, body: TodoUpdateIn):
    if not db.get_todo(todo_id):
        raise HTTPException(status_code=404, detail="Todo not found")
    return db.update_todo(todo_id, text=body.text, checked=body.checked)


@app.delete("/todos/{todo_id}", dependencies=[Depends(require_api_key)])
def delete_todo(todo_id: int):
    if not db.get_todo(todo_id):
        raise HTTPException(status_code=404, detail="Todo not found")
    db.delete_todo(todo_id)
    return {"ok": True}


@app.post("/projects/{project_id}/todos/reorder", dependencies=[Depends(require_api_key)])
def reorder_todos(project_id: int, body: TodoReorderIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.reorder_todos(project_id, body.ids)


@app.post("/projects/{project_id}/todos/run", dependencies=[Depends(require_api_key)])
def run_checked_todos(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.run_checked_todos(project_id)


# ------------------------------------------------------------------ Octopus

GENERAL_PROJECT_NAME = "General"
GENERAL_PROJECT_PATH = Path(__file__).parent / "workspace" / "general"


def _general_project() -> dict:
    """Home for requests that don't belong to a registered repo. Its folder
    is only used if such a request gets routed to Claude Code."""
    GENERAL_PROJECT_PATH.mkdir(parents=True, exist_ok=True)
    return db.get_project_by_name(GENERAL_PROJECT_NAME) or db.add_project(
        GENERAL_PROJECT_NAME, str(GENERAL_PROJECT_PATH))


@app.post("/octopus", dependencies=[Depends(require_api_key)])
def octopus(body: OctopusIn):
    """One entry point for any request: classify it, pick a model from
    models.json, and queue it on the lane that model runs in."""
    text = body.request.strip()
    if not text:
        raise HTTPException(status_code=400, detail="request is empty")
    if body.project_id is not None:
        project = db.get_project(body.project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
    else:
        project = _general_project()

    try:
        decision = router.route(
            text,
            project_name=None if project["name"] == GENERAL_PROJECT_NAME else project["name"],
            force_model=body.model,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    task = db.add_task(project["id"], text, body.priority, None, lane=decision["lane"],
                       model_id=decision["model_id"], category=decision["category"],
                       route_reason=decision["reason"], routed_by=decision["routed_by"])
    model = router.get_model(decision["model_id"]) or {}
    decision = {**decision, "model_label": model.get("label", decision["model_id"])}
    return {"task": _with_position(task), "decision": decision}


@app.get("/octopus/models", dependencies=[Depends(require_api_key)])
def octopus_models():
    return router.models_status()


@app.get("/octopus/usage", dependencies=[Depends(require_api_key)])
def octopus_usage(days: int = 30):
    return db.usage_summary(days)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    # No auth on the page shell itself (it's static HTML/JS) - the data
    # requests it makes still need the API key, entered once and kept in
    # the browser's localStorage. The app itself lives in static/js/.
    return HTMLResponse(DASHBOARD_HTML_PATH.read_text(), headers={"Cache-Control": "no-cache"})


@app.get("/dashboard/legacy", response_class=HTMLResponse)
def dashboard_legacy():
    # The pre-redesign single-file dashboard, kept reachable until the new
    # one has fully replaced it.
    return LEGACY_DASHBOARD_HTML_PATH.read_text()



# ------------------------------------------------------------ queue control

class RequeueIn(BaseModel):
    # True: Retry / "Retry now" - straight to the front. False: Undo on a
    # cancel - back to the spot it had.
    front: bool = True


class QueueReorderIn(BaseModel):
    ids: list[int]


@app.post("/tasks/{task_id}/requeue", dependencies=[Depends(require_api_key)])
def requeue_task(task_id: int, body: RequeueIn = RequeueIn()):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in db.REQUEUEABLE:
        raise HTTPException(status_code=409, detail=f"A {task['status']} task can't be requeued")
    return _with_position(db.requeue_task(task_id, front=body.front))


@app.post("/tasks/{task_id}/top", dependencies=[Depends(require_api_key)])
def move_task_to_top(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "queued":
        raise HTTPException(status_code=409, detail="Only a queued task can be moved")
    db.move_to_front(task_id)
    return _with_position(db.get_task(task_id))


@app.post("/queue/reorder", dependencies=[Depends(require_api_key)])
def reorder_queue(body: QueueReorderIn):
    db.reorder_queue(body.ids)
    return {"ok": True}


@app.post("/tasks/{task_id}/stop", dependencies=[Depends(require_api_key)])
def stop_task(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "running" or not worker.stop_task(task_id):
        raise HTTPException(status_code=409, detail="Only a running Claude Code task can be stopped")
    return {"ok": True}


@app.post("/tasks/{task_id}/reviewed", dependencies=[Depends(require_api_key)])
def mark_reviewed(task_id: int):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    db.mark_reviewed(task_id)
    return db.get_task(task_id)


@app.get("/tasks/{task_id}/thread", dependencies=[Depends(require_api_key)])
def task_thread(task_id: int):
    """Every message in this task's chat, oldest first, plus what each
    agent turn did step by step (events, keyed by task id)."""
    tasks = db.get_thread(task_id)
    if not tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"tasks": tasks, "events": db.list_task_events([t["id"] for t in tasks])}


# --------------------------------------------------------------- settings

class ProjectSettingsIn(BaseModel):
    default_priority: Optional[int] = None
    notify_approvals: Optional[bool] = None
    notify_failures: Optional[bool] = None
    notify_completions: Optional[bool] = None
    quiet_hours: Optional[bool] = None
    allow_commands: Optional[list[str]] = None
    budget_usd: Optional[float] = None


class SettingsIn(BaseModel):
    notify_on_resume: Optional[bool] = None
    hold_low_when_busy: Optional[bool] = None
    monthly_budget_usd: Optional[float] = None


@app.get("/projects/{project_id}/settings", dependencies=[Depends(require_api_key)])
def get_project_settings(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {**db.get_project_settings(project_id), "month_spend_usd": db.project_month_spend(project_id)}


@app.patch("/projects/{project_id}/settings", dependencies=[Depends(require_api_key)])
def update_project_settings(project_id: int, body: ProjectSettingsIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    db.update_project_settings(project_id, **body.model_dump(exclude_none=True))
    return get_project_settings(project_id)


def _settings_view() -> dict:
    return {**db.get_settings(), "ntfy_topic": worker.NTFY_TOPIC, "claude_usage": db.get_setting("claude_usage")}


@app.get("/settings", dependencies=[Depends(require_api_key)])
def get_settings():
    return _settings_view()


@app.patch("/settings", dependencies=[Depends(require_api_key)])
def update_settings(body: SettingsIn):
    for k, v in body.model_dump(exclude_none=True).items():
        db.set_setting(k, v)
    return _settings_view()


@app.post("/notify/test", dependencies=[Depends(require_api_key)])
def send_test_push():
    if not worker.NTFY_TOPIC:
        raise HTTPException(status_code=409, detail="NTFY_TOPIC isn't set in the launchd plist")
    return {"sent": worker.notify_phone("Test push from Octopus", "If you can read this, pushes work.")}


@app.get("/usage/summary", dependencies=[Depends(require_api_key)])
def usage_summary():
    """Real API spend (today / month / last 7 days / by project) plus the
    last 5-hour and weekly Claude usage Claude Code reported."""
    return {**db.spend_summary(), "claude_usage": db.get_setting("claude_usage")}


# ---------------------------------------------------------------- recipes

class RecipeIn(BaseModel):
    name: str
    prompt: str
    # 'any' | 'all' | a project id
    target: str = "any"


class RecipeRunIn(BaseModel):
    # Required for a recipe whose target is 'any'.
    project_id: Optional[int] = None
    # Defaults to each project's default priority.
    priority: Optional[int] = None


@app.get("/recipes", dependencies=[Depends(require_api_key)])
def list_recipes():
    return db.list_recipes()


@app.post("/recipes", dependencies=[Depends(require_api_key)])
def create_recipe(body: RecipeIn):
    if not body.name.strip() or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="name and prompt are required")
    return db.add_recipe(body.name.strip(), body.prompt.strip(), body.target)


@app.delete("/recipes/{recipe_id}", dependencies=[Depends(require_api_key)])
def delete_recipe(recipe_id: int):
    db.delete_recipe(recipe_id)
    return {"ok": True}


@app.post("/recipes/{recipe_id}/run", dependencies=[Depends(require_api_key)])
def run_recipe(recipe_id: int, body: RecipeRunIn = RecipeRunIn()):
    """Queue a recipe as a Claude Code task: in one project, or in every
    active project for an 'all' recipe."""
    recipe = db.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    if recipe["target"] == "all" and body.project_id is None:
        projects = [p for p in db.list_projects(active_only=True) if not db.is_general_project(p)]
    else:
        pid = body.project_id if body.project_id is not None else (
            int(recipe["target"]) if recipe["target"].isdigit() else None)
        if pid is None:
            raise HTTPException(status_code=400, detail="Pick a project to run this recipe in")
        project = db.get_project(pid)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        projects = [project]
    tasks = []
    for p in projects:
        prio = body.priority if body.priority is not None else db.get_project_settings(p["id"])["default_priority"]
        tasks.append(_with_position(db.add_task(p["id"], recipe["prompt"], prio, routed_by="recipe",
                                                route_reason=f"recipe: {recipe['name']}")))
    return {"tasks": tasks}


# -------------------------------------------------------------- schedules

class ScheduleIn(BaseModel):
    prompt: str
    label: Optional[str] = None
    # A project id, or null for every active project.
    project_id: Optional[int] = None
    cadence: str  # once | daily | weekdays | weekly
    time: str  # "HH:MM", local time
    weekday: Optional[int] = None  # weekly: 0 = Monday
    date: Optional[str] = None  # once: "YYYY-MM-DD" (default: next time it's that time)
    model: str = "auto"
    priority: int = 0


class ScheduleUpdateIn(BaseModel):
    enabled: bool


@app.get("/schedules", dependencies=[Depends(require_api_key)])
def list_schedules():
    return db.list_schedules()


@app.post("/schedules", dependencies=[Depends(require_api_key)])
def create_schedule(body: ScheduleIn):
    import re as _re
    if body.cadence not in db.CADENCES:
        raise HTTPException(status_code=400, detail=f"cadence must be one of {', '.join(db.CADENCES)}")
    if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.time):
        raise HTTPException(status_code=400, detail="time must be HH:MM")
    if body.cadence == "weekly" and body.weekday not in range(7):
        raise HTTPException(status_code=400, detail="weekly schedules need a weekday (0 = Monday)")
    if body.project_id is not None and not db.get_project(body.project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    label = (body.label or body.prompt).strip()
    return db.add_schedule(label[:80], body.prompt.strip(), body.project_id, body.cadence, body.time,
                           body.weekday, body.model, body.priority, on_date=body.date)


@app.patch("/schedules/{schedule_id}", dependencies=[Depends(require_api_key)])
def update_schedule(schedule_id: int, body: ScheduleUpdateIn):
    sc = db.set_schedule_enabled(schedule_id, body.enabled)
    if not sc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return sc


@app.delete("/schedules/{schedule_id}", dependencies=[Depends(require_api_key)])
def delete_schedule(schedule_id: int):
    db.delete_schedule(schedule_id)
    return {"ok": True}


# ----------------------------------------------------------------- polish

POLISH_SYSTEM_PROMPT = (
    "You polish requests a developer sends to an AI assistant (Claude Code or a chat model). Rewrite the text so it is clear, "
    "specific and well-formed: fix grammar, spelling and punctuation, and tighten vague wording. Keep the same intent, language "
    "and tone. Keep file names, commands, code, numbers and names exactly as written. Do not add new requirements or explanations. "
    "Return only the rewritten text, with no quotes or preamble."
)


class PolishIn(BaseModel):
    text: str


@app.post("/octopus/polish", dependencies=[Depends(require_api_key)])
def polish_text(body: PolishIn):
    """Rewrite a prompt with the cheapest available answer-only model. The
    dashboard falls back to a local cleanup if this fails."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")
    candidates = [m for m in router.registry().get("models", [])
                  if router.lane_for(m) == "api" and router.availability(m)[0]]
    if not candidates:
        raise HTTPException(status_code=503, detail="No answer-only model is available to polish with")
    model = sorted(candidates, key=lambda m: (m.get("cost", 5), -m.get("speed", 0)))[0]
    try:
        out = providers.complete(model, [{"role": "user", "content": text[:4000]}], POLISH_SYSTEM_PROMPT, 30)
    except providers.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"text": out.text.strip(), "model_id": model["id"]}
