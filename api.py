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
from pydantic import BaseModel

import db

DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"

API_KEY = os.environ.get("API_KEY")  # set this in your environment / launchd plist

app = FastAPI(title="claude-queue")


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

    return db.add_task(project_id, body.prompt, body.priority, thread_id)


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


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    # No auth on the page shell itself (it's static HTML/JS) - the data
    # requests it makes still need the API key, entered once and kept in
    # the browser's localStorage. See dashboard.html.
    return DASHBOARD_HTML_PATH.read_text()
