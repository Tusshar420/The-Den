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
from pydantic import BaseModel

import db
import router

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
    request: str
    # Omit (or null) to use the built-in "General" project - fine for
    # questions that don't belong to any repo.
    project_id: Optional[int] = None
    # "auto" lets the router decide; a models.json id forces that model.
    model: str = "auto"
    priority: int = 0


class ApprovalDecisionIn(BaseModel):
    approved: bool


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


@app.get("/projects", dependencies=[Depends(require_api_key)])
def list_projects():
    return db.list_projects()


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
        return db.add_task(project_id, body.prompt, body.priority, thread_id,
                           lane=thread_root.get("lane") or "agent", model_id=thread_root.get("model_id"),
                           category=thread_root.get("category"), routed_by="thread",
                           route_reason="reply - same model as the rest of this chat")

    return db.add_task(project_id, body.prompt, body.priority, thread_id, routed_by="direct",
                       route_reason="sent straight to Claude Code")


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
    if not db.get_approval(approval_id):
        raise HTTPException(status_code=404, detail="Approval not found")
    return db.decide_approval(approval_id, body.approved)


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
    return {"task": task, "decision": decision}


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
