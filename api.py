"""
Minimal local HTTP API in front of the queue - this is what iOS Shortcuts
(and anything else on your Tailscale network) talks to.

Auth is a single shared API key via the `X-API-Key` header. Tailscale
already keeps this off the open internet, but the key stops any other
device on your tailnet from poking your queue without you noticing.
"""

import hmac
import html
import json
import os
import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field

import approval_links
import db
import providers
import risk
import router
import titles
import uploads
import worker
from assistant import actions as assistant_actions
from assistant import api as assistant_api
from assistant import orchestrator as assistant_orchestrator
from octopus import api as octopus_api
from octopus import service as octopus_service

DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"
LEGACY_DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard_legacy.html"
STATIC_DIR = Path(__file__).parent / "static"

API_KEY = os.environ.get("API_KEY")  # set this in your environment / launchd plist

app = FastAPI(title="The Den")


class NoCacheStaticFiles(StaticFiles):
    """The dashboard's modules change whenever this repo does, and iOS
    Safari will happily keep serving a stale app.js for days - make every
    load revalidate (cheap: an unchanged file is a 304)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def check_bind_is_safe(host: str, api_key: Optional[str] = None) -> None:
    """No key is fine for local testing, but only while nothing else can reach
    the port. Raises SystemExit rather than start an open API on the network."""
    if not (api_key if api_key is not None else API_KEY) and host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"Refusing to start: API_KEY is not set but HOST is {host!r}, which other devices can reach. "
            "Set API_KEY, or set HOST=127.0.0.1 to keep the API local to this Mac."
        )


# Failed-key attempts per client address, so the key can't be guessed by
# hammering the API. A locked-out client is refused even with the right key
# until its oldest failure ages out of the window.
FAIL_LIMIT = 10
FAIL_WINDOW_SECONDS = 60
_failures: dict[str, list[float]] = {}


def _recent_failures(client: str, now: float) -> list[float]:
    recent = [t for t in _failures.get(client, []) if now - t < FAIL_WINDOW_SECONDS]
    if recent:
        _failures[client] = recent
    else:
        _failures.pop(client, None)
    return recent


def require_api_key(request: Request, x_api_key: Optional[str] = Header(default=None)) -> None:
    if not API_KEY:
        # Only reachable on a loopback bind - main.py refuses to start otherwise.
        return
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = _recent_failures(client, now)
    if len(recent) >= FAIL_LIMIT:
        retry_after = max(1, int(FAIL_WINDOW_SECONDS - (now - recent[0])) + 1)
        raise HTTPException(status_code=429, detail="Too many failed attempts - try again shortly",
                            headers={"Retry-After": str(retry_after)})
    if not hmac.compare_digest((x_api_key or "").encode(), API_KEY.encode()):
        _failures[client] = recent + [now]
        if len(_failures) > 1024:                       # bound memory against many spoofed sources
            for c in list(_failures):
                _recent_failures(c, now)
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# Personal assistant: connections, permissions, activity, dashboard views.
app.include_router(assistant_api.build_router(require_api_key))
# Octopus: plans, routing history, agents, tools, providers, task detail, voice.
app.include_router(octopus_api.build_router(require_api_key))


class ProjectIn(BaseModel):
    name: str
    path: str
    # "work" or "personal" - lets the dashboard show one side at a time.
    category: str = "work"


class ProjectActiveIn(BaseModel):
    active: Optional[bool] = None
    category: Optional[str] = None
    # not_started | in_progress | need_info | on_hold | completed
    status: Optional[str] = None


class TaskIn(BaseModel):
    prompt: str
    priority: int = 0
    # Set this to an existing task's id to reply within its chat instead of
    # starting a new one - that task's thread then resumes the same Claude
    # Code session instead of starting from a blank conversation.
    thread_id: Optional[int] = None
    # Claude Code permission mode for this task (db.PERMISSION_MODES). Omit
    # for the default; a reply that omits it keeps its chat's last mode.
    permission_mode: Optional[str] = None
    # Upload ids from POST /uploads - pictures attached to this message.
    images: Optional[list[str]] = None
    # A reply only: send it to a different model than the chat's last one (a
    # models.json id). The new model is given the whole conversation so far.
    model: Optional[str] = None


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
    # Upload ids from POST /uploads. Octopus only picks a model that can read them.
    images: Optional[list[str]] = None
    # Continue an existing chat instead of starting one - what a voice or
    # Shortcuts client uses so "move it to 4" follows "what's on tomorrow".
    # thread_id names the chat; continue_recent picks the assistant chat that
    # was active in the last 15 minutes (if any).
    thread_id: Optional[int] = None
    continue_recent: bool = False
    permission_mode: Optional[str] = None  # see TaskIn
    # "Advanced" overrides: an agents.json id forces that agent.
    agent: Optional[str] = None
    # Where the request came from (dashboard | voice | shortcut | api) - shown on the Routing page.
    source: str = "api"
    # auto: Octopus decides whether to show its plan first. skip: the caller has
    # already seen it, run it. force: always show the plan and wait for Run.
    review: str = "auto"


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
    # How it will run once queued - same meaning as on a task. null = the default (Claude Code, acceptEdits, normal).
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    priority: Optional[int] = None


class TodoReorderIn(BaseModel):
    ids: list[int]


class NoteIn(BaseModel):
    text: str
    kind: Literal["guideline", "requirement", "note"] = "note"


class NoteUpdateIn(BaseModel):
    text: Optional[str] = None
    kind: Optional[Literal["guideline", "requirement", "note"]] = None


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
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the project a name")
    if db.get_project_by_name(name):
        raise HTTPException(status_code=409, detail=f"Project '{name}' already exists")
    # The dashboard suggests "~/code/..."; the worker runs Claude Code with this
    # as its working directory, so it has to be a real absolute folder.
    path = os.path.abspath(os.path.expanduser(body.path.strip()))
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"No folder at {path} on this Mac")
    if body.category not in db.PROJECT_CATEGORIES:
        raise HTTPException(status_code=400, detail="category must be 'work' or 'personal'")
    return db.add_project(name, path, body.category)


@app.patch("/projects/{project_id}", dependencies=[Depends(require_api_key)])
def update_project(project_id: int, body: ProjectActiveIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if body.category is not None:
        if body.category not in db.PROJECT_CATEGORIES:
            raise HTTPException(status_code=400, detail="category must be 'work' or 'personal'")
        db.set_project_category(project_id, body.category)
    if body.status is not None:
        if body.status not in db.PROJECT_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(db.PROJECT_STATUSES)}")
        db.set_project_status(project_id, body.status)
    if body.active is not None:
        db.set_project_active(project_id, body.active)
    return db.get_project(project_id)


@app.get("/projects/{project_id}/tasks", dependencies=[Depends(require_api_key)])
def list_project_tasks(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_tasks(project_id=project_id)


def _check_mode(mode: Optional[str]) -> Optional[str]:
    if mode is not None and mode not in db.PERMISSION_MODES:
        raise HTTPException(status_code=400, detail=f"permission_mode must be one of {', '.join(db.PERMISSION_MODES)}")
    return mode


def _thread_mode(root_id: int) -> Optional[str]:
    """The mode the chat last ran under - what a reply inherits."""
    return next((t["permission_mode"] for t in reversed(db.get_thread(root_id)) if t.get("permission_mode")), None)


def _reply_model(root: dict, requested: Optional[str], has_images: bool) -> tuple[Optional[str], str, str]:
    """(model_id, lane, why) for a reply. No request = the chat's latest model."""
    current = db.get_thread_model(root["id"]) or root.get("model_id")
    cfg = router.get_model(current)
    current_lane = router.lane_for(cfg) if cfg else root.get("lane") or "agent"
    if not requested or requested == "auto" or requested == current:
        return current, current_lane, "reply - same model as the previous message"

    chosen = router.get_model(requested)
    if not chosen:
        raise HTTPException(status_code=400, detail=f"Unknown model '{requested}'")
    lane = router.lane_for(chosen)
    if current_lane not in ("agent", "api") or lane not in ("agent", "api") or chosen.get("modality") == "image":
        raise HTTPException(status_code=400, detail="This chat can't switch to that model")
    latest = db.get_thread(root["id"])[-1]
    if latest["status"] in ("queued", "running", "rate_limited"):  # the new model needs the last answer to hand over
        raise HTTPException(status_code=409, detail="Wait for the last message to finish before switching models")
    ok, why, until = router.availability(chosen)
    if not ok and not until:  # a rate-limited model is fine: the reply waits for it
        raise HTTPException(status_code=409, detail=f"{chosen.get('label', chosen['id'])} isn't available: {why}")
    if has_images and not chosen.get("vision"):
        raise HTTPException(status_code=400, detail=f"{chosen.get('label', chosen['id'])} can't read pictures")
    return chosen["id"], lane, f"reply - you switched to {chosen.get('label', chosen['id'])}; it gets the chat so far"


@app.post("/projects/{project_id}/tasks", dependencies=[Depends(require_api_key)])
def create_task(project_id: int, body: TaskIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    mode = _check_mode(body.permission_mode)
    try:
        images = uploads.validate(body.images)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    thread_id = body.thread_id
    if thread_id is not None:
        root = db.get_task(thread_id)
        if not root or root["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="thread_id does not refer to a task in this project")
        # Normalize to the actual root, in case the caller passed a reply's
        # id rather than the first message's (the dashboard always resolves
        # this itself, but other API clients might not).
        thread_id = root["thread_id"] or root["id"]
        # A reply stays on the model the chat last used, unless it names another
        # one. Either way the model gets the conversation so far: api models are
        # sent the earlier messages, and Claude Code resumes its session plus a
        # catch-up on what the other models said (worker.py).
        thread_root = db.get_task(thread_id)
        model_id, lane, reason = _reply_model(thread_root, body.model, bool(images))
        return _with_position(db.add_task(project_id, body.prompt, body.priority, thread_id,
                                          lane=lane, model_id=model_id,
                                          category=thread_root.get("category"), routed_by="thread", route_reason=reason,
                                          permission_mode=(mode or _thread_mode(thread_id)) if lane == "agent" else None,
                                          images=images))

    return _with_position(db.add_task(project_id, body.prompt, body.priority, thread_id, routed_by="direct",
                                      route_reason="sent straight to Claude Code", permission_mode=mode, images=images))


SUGGEST_TITLE = "Suggest improvements"


def _suggest_prompt(project: dict) -> str:
    """What the project's suggestion run is told: the review brief plus what the
    Den itself knows (backlog, recent work, notes) that the folder can't show."""
    pid = project["id"]
    backlog = [t["text"] for t in db.list_todos(pid) if t["status"] == "pending"][:15]
    recent = sorted((t for t in db.list_tasks(project_id=pid) if not t["thread_id"]), key=lambda t: t["id"])[-10:]
    lines = [
        f"Review the project \"{project['name']}\" (this folder) and suggest concrete improvements. "
        "Read only - do not change any files.",
        "",
        "Look at the code and structure, README and docs, dependencies, tests, error handling, "
        "security, performance, and how easy it is to run and maintain. Check git history to see what "
        "has been changing lately.",
    ]
    if backlog:
        lines += ["", "Already on the backlog (don't repeat these, but say if one should be reprioritised):"]
        lines += [f"- {t}" for t in backlog]
    if recent:
        lines += ["", "Recent tasks run here:"]
        lines += [f"- [{t['status']}] {(t.get('title') or t['prompt'])[:100]}" for t in recent]
    lines += [
        "",
        "Reply with 5 to 10 suggestions, most valuable first. For each: a short title, why it matters "
        "for this project specifically, what to change and where (file names), and a rough size "
        "(small / medium / large). Finish with the one thing you'd do first.",
    ]
    return "\n".join(lines)


@app.post("/projects/{project_id}/suggest", dependencies=[Depends(require_api_key)])
def suggest_improvements(project_id: int):
    """The project page's Suggest button: queue a read-only review of the project."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if db.is_general_project(project):
        raise HTTPException(status_code=400, detail="General has no folder to review")
    # A second tap while one is still waiting or running shouldn't queue another.
    open_run = next((t for t in db.list_tasks(project_id=project_id, statuses=["queued", "running"])
                     if t["title"] == SUGGEST_TITLE), None)
    if open_run:
        return _with_position(open_run)
    return _with_position(db.add_task(project_id, _suggest_prompt(project), 0, title=SUGGEST_TITLE,
                                      routed_by="direct", route_reason="project suggestions - read only",
                                      permission_mode="plan"))


@app.post("/uploads", dependencies=[Depends(require_api_key)])
async def upload_image(request: Request):
    """Store one attached picture. The body is the raw image file (not JSON or a
    form); the reply is {id, mime, size} - send the id as `images` on a prompt."""
    too_large = HTTPException(status_code=413, detail="That picture is too large")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad Content-Length header")
    if declared > uploads.MAX_BYTES:                     # cheap early refusal; not the real limit
        raise too_large
    # A chunked body has no Content-Length and a client can lie about it, so
    # the cap is enforced on what actually arrives, stopping as soon as it's passed.
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > uploads.MAX_BYTES:
            raise too_large
        chunks.append(chunk)
    try:
        return uploads.save(b"".join(chunks))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/uploads/{image_id}", dependencies=[Depends(require_api_key)])
def get_upload(image_id: str):
    path = uploads.path_of(image_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type=uploads.mime_of(image_id), headers={"Cache-Control": "private, max-age=86400"})


@app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_task(task_id: int):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    db.cancel_task(task_id)
    return db.get_task(task_id)


@app.get("/queue/status", dependencies=[Depends(require_api_key)])
def queue_status():
    return db.queue_status()


# The dashboard's Restart button: reload this service's code without the terminal.
# pid changes once the new process is up, which is how the page knows to reload.
SERVICE_STARTED_AT = db._now()


def _service_state() -> dict:
    return {"pid": os.getpid(), "started_at": SERVICE_STARTED_AT, "restarting": worker.draining,
            "waiting_on": worker.busy_task_ids() if worker.draining else []}


@app.get("/service", dependencies=[Depends(require_api_key)])
def service_info():
    return _service_state()


@app.post("/service/restart", dependencies=[Depends(require_api_key)])
async def restart_service():
    """Takes no new work from now on and restarts as soon as nothing is running
    (see worker.request_restart) - right away when idle."""
    worker.request_restart()
    return _service_state()


@app.get("/tasks", dependencies=[Depends(require_api_key)])
def list_all_tasks(limit: int = 200, status: Optional[str] = None, project_id: Optional[int] = None):
    statuses = status.split(",") if status else None
    return db.list_all_tasks(limit=limit, statuses=statuses, project_id=project_id)


@app.get("/messages", dependencies=[Depends(require_api_key)])
def message_centre(q: str = "", status: str = "all", project_ids: Optional[str] = None,
                   lane: Optional[str] = None, limit: int = 40, offset: int = 0):
    """Chats, most recent message first (see db.list_chats). project_ids is a
    comma-separated list; leave it off for every project."""
    try:
        pids = [int(x) for x in project_ids.split(",") if x] if project_ids is not None else None
    except ValueError:
        raise HTTPException(status_code=400, detail="project_ids must be comma-separated numbers")
    return db.list_chats(q=q.strip(), status=status, project_ids=pids, lane=lane or None,
                         limit=max(1, min(limit, 200)), offset=max(0, offset))


def _slim(approval: dict) -> dict:
    """An assistant approval's stored arguments (the full email body...) are
    needed to run it, not to show it - the card is built from `preview`."""
    if assistant_actions.is_assistant_approval(approval):
        try:
            payload = json.loads(approval["tool_input"])
            payload.pop("args", None)
            return {**approval, "tool_input": json.dumps(payload)}
        except ValueError:
            pass
    return approval


@app.get("/approvals", dependencies=[Depends(require_api_key)])
def list_pending_approvals():
    return [_slim(a) for a in db.list_pending_approvals()]


@app.post("/approvals/{approval_id}/decide", dependencies=[Depends(require_api_key)])
def decide_approval(approval_id: int, body: ApprovalDecisionIn):
    approval = db.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if assistant_actions.is_assistant_approval(approval):
        # Personal-assistant action: approving runs exactly what the card showed, once.
        # (No "always allow" here - trusting a capability is a policy setting.)
        result = assistant_actions.decide(approval, body.approved, body.note)
        return {**_slim(db.get_approval(approval_id)), "assistant_result": result}
    if body.approved and body.always:
        entry = risk.allow_entry_for(risk.command_of(approval["tool_name"], approval["tool_input"])) \
            if approval["tool_name"] == "Bash" else None
        if not entry:
            raise HTTPException(status_code=400, detail="This command can't be always-allowed (it chains or redirects other commands)")
        task = db.get_task(approval["task_id"])
        db.add_allowed_command(task["project_id"], entry)
    return db.decide_approval(approval_id, body.approved, note=None if body.approved else body.note)


_QUICK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · The Den</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#14181f;--muted:#5b6573;--accent:{accent};--line:#e3e6eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f141a;--card:#18202a;--fg:#e8edf2;--muted:#93a1b0;--line:#26313d}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,system-ui,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px;max-width:420px;width:100%}}
h1{{margin:0 0 6px;font-size:22px;color:var(--accent)}}p{{margin:0 0 14px;color:var(--muted)}}
code{{display:block;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
font:13px/1.4 ui-monospace,Menlo,monospace;color:var(--fg);white-space:pre-wrap;word-break:break-word;margin-bottom:16px}}
a{{color:var(--fg);font-weight:600}}
</style></head><body><div class="card"><h1>{title}</h1><p>{text}</p>{command}
<a href="/dashboard">Open dashboard</a></div></body></html>"""


def _quick_page(title: str, text: str, command: str = "", accent: str = "#5b6573", status: int = 200) -> HTMLResponse:
    cmd = f"<code>{html.escape(command[:600])}</code>" if command else ""
    return HTMLResponse(_QUICK_PAGE.format(title=html.escape(title), text=html.escape(text), command=cmd, accent=accent),
                        status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/approvals/{approval_id}/quick/{decision}", response_class=HTMLResponse)
def quick_decide(approval_id: int, decision: str, t: str = ""):
    """Approve / Deny buttons on an approval push. Opened in Safari, which can't
    send X-API-Key - the signed token in the link is the auth (approval_links)."""
    if not approval_links.valid(approval_id, decision, t):
        return _quick_page("Link not valid", "This approval link is broken or was changed.", status=403)
    approval = db.get_approval(approval_id)
    if not approval:
        return _quick_page("Not found", "That approval doesn't exist any more.", status=404)
    command = risk.command_of(approval["tool_name"], approval["tool_input"]) if approval["tool_name"] == "Bash" else ""
    if approval["status"] != "pending":
        said = {"approved": "approved", "denied": "denied", "timed_out": "closed (timed out or the task stopped)"}
        return _quick_page("Already decided", f"This was already {said.get(approval['status'], approval['status'])}.", command)
    decide_approval(approval_id, ApprovalDecisionIn(approved=decision == "approve"))
    if db.get_approval(approval_id)["status"] == "approved":
        return _quick_page("Approved ✓", "Octopus is carrying on.", command, accent="#2e9e5b")
    return _quick_page("Denied", "Octopus won't run this and will work around it.", command, accent="#d0473a")


@app.get("/approvals/{approval_id}/context", dependencies=[Depends(require_api_key)])
def approval_context(approval_id: int):
    """Risk read, the steps so far and the project's uncommitted changes -
    everything the approval card and detail pane show besides the command."""
    approval = db.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if assistant_actions.is_assistant_approval(approval):
        payload = json.loads(approval["tool_input"])
        return {"kind": "assistant", "tool_name": approval["tool_name"], "describe": payload.get("describe"),
                "capabilities": payload.get("capabilities", []), "preview": payload.get("preview"),
                "redacted": bool(payload.get("redacted"))}
    task = db.get_task(approval["task_id"])
    project = db.get_project(task["project_id"]) if task else None
    events = db.list_task_events([approval["task_id"]]).get(approval["task_id"], [])
    allow = db.get_project_settings(project["id"])["allow_commands"] if project else []
    return risk.approval_context(approval, project, events, allow)


@app.get("/projects/{project_id}/notes", dependencies=[Depends(require_api_key)])
def list_notes(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_project_notes(project_id)


@app.post("/projects/{project_id}/notes", dependencies=[Depends(require_api_key)])
def create_note(project_id: int, body: NoteIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write something first")
    return db.add_project_note(project_id, text, body.kind)


@app.patch("/notes/{note_id}", dependencies=[Depends(require_api_key)])
def update_note(note_id: int, body: NoteUpdateIn):
    if not db.get_project_note(note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    text = body.text.strip() if body.text is not None else None
    if text == "":
        raise HTTPException(status_code=400, detail="A note can't be empty - delete it instead")
    return db.update_project_note(note_id, text, body.kind)


@app.delete("/notes/{note_id}", dependencies=[Depends(require_api_key)])
def delete_note(note_id: int):
    if not db.get_project_note(note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete_project_note(note_id)
    return {"ok": True}


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
    sent, settings = body.model_fields_set, {}
    if "model" in sent:
        if body.model not in (None, "auto") and not router.get_model(body.model):
            raise HTTPException(status_code=400, detail=f"Unknown model '{body.model}'")
        settings["model_id"] = body.model
    if "permission_mode" in sent:
        settings["permission_mode"] = _check_mode(body.permission_mode)
    if "priority" in sent:
        if body.priority not in (-1, 0, 1):
            raise HTTPException(status_code=400, detail="priority must be -1, 0 or 1")
        settings["priority"] = body.priority
    return db.update_todo(todo_id, text=body.text, checked=body.checked, settings=settings)


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
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    name = None if db.is_general_project(project) else project["name"]
    decisions = {}
    for t in db.list_todos(project_id):
        if t["checked"] and t["status"] == "pending" and t["model_id"]:
            try:
                decisions[t["id"]] = router.route(t["text"], project_name=name, force_model=t["model_id"])
            except ValueError:
                pass  # can't take it right now - it runs on the default rather than being lost
    return db.run_checked_todos(project_id, decisions=decisions)


# ------------------------------------------------------------------ Octopus

@app.post("/octopus", dependencies=[Depends(require_api_key)])
def octopus(body: OctopusIn):
    """One entry point for any request, typed or spoken: Octopus classifies it,
    picks the project, agent, model and tools, checks permissions, and either
    queues it (status "queued"), waits for you to look at the plan first
    ("proposed" / "needs_input" - answer with /octopus/plans/{routing_id}/run),
    or, for an instant command like "what's happening?", just does it
    ("done", with the answer in `result`). See octopus/service.py."""
    try:
        return octopus_service.submit(
            body.request, project_id=body.project_id, model=body.model, agent=body.agent, priority=body.priority,
            permission_mode=body.permission_mode, thread_id=body.thread_id, continue_recent=body.continue_recent,
            source=body.source, review=body.review, images=body.images)
    except octopus_service.ServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@app.get("/octopus/result/{task_id}", dependencies=[Depends(require_api_key)])
def octopus_result(task_id: int, wait: int = 0):
    """The answer to a request, for clients that just want text back - voice
    (speech-to-text -> POST /octopus -> this -> text-to-speech), Shortcuts.
    `wait` holds the request open up to that many seconds (max 60) until the
    task has finished. `awaiting_approval` lists anything the assistant
    needs a yes/no on before it acts; decide it with /approvals/{id}/decide."""
    deadline = time.time() + max(0, min(wait, 60))
    while True:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task["status"] not in ("queued", "running", "rate_limited") or time.time() >= deadline:
            break
        time.sleep(0.4)
    pending = [a for a in db.list_pending_approvals() if a["task_id"] == task_id]
    approvals = []
    for a in pending:
        try:
            p = json.loads(a["tool_input"])
        except ValueError:
            p = {}
        approvals.append({"id": a["id"], "describe": p.get("describe"), "preview": p.get("preview")})
    text = (task.get("result_summary") if task["status"] == "done" else task.get("error_message")) or ""
    finished = task["status"] not in ("queued", "running", "rate_limited")
    return {"id": task["id"], "status": task["status"], "finished": finished, "lane": task.get("lane"),
            "text": text, "spoken": assistant_orchestrator.spoken_summary(text, [a["describe"] or "" for a in approvals]) if finished else "",
            "awaiting_approval": approvals, "thread_id": task["thread_id"] or task["id"]}


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


def _queued_task_or_error(task_id: int) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "queued":
        raise HTTPException(status_code=409, detail="It has already started - only a queued task can be changed")
    return task


class MoveIn(BaseModel):
    # Queued task to sit right behind; null = first in line.
    after_id: Optional[int] = None


@app.post("/tasks/{task_id}/move", dependencies=[Depends(require_api_key)])
def move_task(task_id: int, body: MoveIn):
    _queued_task_or_error(task_id)
    db.move_after(task_id, body.after_id)
    return _with_position(db.get_task(task_id))


@app.post("/tasks/{task_id}/run-now", dependencies=[Depends(require_api_key)])
def run_task_now(task_id: int):
    """Proceed now: start a queued Claude Code task right away, alongside
    whatever is already running (the worker's run_now_loop picks it up within
    a couple of seconds). A chat still runs one message at a time, so a reply
    whose chat is busy starts the moment that finishes. Answer-only tasks
    have no second slot - they just go to the front, and are quick anyway."""
    task = _queued_task_or_error(task_id)
    project = db.get_project(task["project_id"])
    if not project or not project["active"]:
        raise HTTPException(status_code=409, detail="Its project is paused - resume the project first")
    if task["lane"] != "agent":
        db.move_to_front(task_id)
        return {"task": _with_position(db.get_task(task_id)), "mode": "front"}
    db.set_run_now(task_id)
    root = task["thread_id"] or task["id"]
    chat_busy = any(m["id"] != task_id and (m["status"] == "running" or
                                            (m["id"] < task_id and m["status"] in ("queued", "rate_limited")))
                    for m in db.get_thread(task_id))
    return {"task": _with_position(db.get_task(task_id)), "mode": "after_chat" if chat_busy else "now",
            "thread_root": root}


class TaskEditIn(BaseModel):
    prompt: Optional[str] = None
    title: Optional[str] = None
    priority: Optional[int] = None
    # New chats only - a reply has to stay in its chat's project and model to
    # continue the same session.
    project_id: Optional[int] = None
    model: Optional[str] = None  # "auto" re-routes, a models.json id forces it
    permission_mode: Optional[str] = None


@app.patch("/tasks/{task_id}", dependencies=[Depends(require_api_key)])
def edit_task(task_id: int, body: TaskEditIn):
    """Change a task that hasn't started yet."""
    task = _queued_task_or_error(task_id)
    is_reply = task["thread_id"] is not None
    fields: dict = {}

    if body.prompt is not None:
        prompt = body.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="The prompt can't be empty")
        if prompt != task["prompt"]:
            fields["prompt"] = prompt
            # A name that came from the old prompt follows the new one (the
            # namer loop picks it up again); a name you gave it stays.
            if not is_reply and body.title is None and task.get("title_by") in ("text", "ai", "kept"):
                fields["title"], fields["title_by"] = db.quick_title(prompt), "text"
    if body.title is not None and not is_reply:
        title = body.title.strip()
        if title and title != task.get("title"):
            fields["title"], fields["title_by"] = title[:120], "user"
    if body.priority is not None:
        if body.priority not in (-1, 0, 1):
            raise HTTPException(status_code=400, detail="priority must be -1, 0 or 1")
        fields["priority"] = body.priority

    project = db.get_project(task["project_id"])
    if body.project_id is not None and body.project_id != task["project_id"]:
        if is_reply:
            raise HTTPException(status_code=400, detail="A reply stays in its chat's project")
        if len(db.get_thread(task_id)) > 1:
            raise HTTPException(status_code=400, detail="This chat already has replies - it has to stay in its project")
        project = db.get_project(body.project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        fields["project_id"] = project["id"]
    if body.permission_mode is not None:
        fields["permission_mode"] = _check_mode(body.permission_mode)
    if body.model is not None:
        if is_reply:
            raise HTTPException(status_code=400, detail="A reply stays on its chat's model")
        try:
            decision = router.route(
                fields.get("prompt", task["prompt"]),
                project_name=None if db.is_general_project(project) else project["name"],
                force_model=body.model,
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        fields.update(lane=decision["lane"], model_id=decision["model_id"], category=decision["category"],
                      route_reason=decision["reason"], routed_by=decision["routed_by"])

    if fields and not db.update_queued_task(task_id, fields):
        raise HTTPException(status_code=409, detail="It started while you were editing - nothing was changed")
    return _with_position(db.get_task(task_id))


@app.post("/tasks/{task_id}/stop", dependencies=[Depends(require_api_key)])
def stop_task(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "running" or not worker.stop_task(task_id):
        raise HTTPException(status_code=409, detail="Only a running Claude Code task can be stopped")
    return {"ok": True}


@app.post("/tasks/{task_id}/pause", dependencies=[Depends(require_api_key)])
def pause_task(task_id: int):
    """Hold a task: a queued one is skipped by the worker (keeping its place in
    line) until played; a running Claude Code one is frozen mid-run."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in db.PAUSABLE:
        raise HTTPException(status_code=409, detail=f"A {task['status']} task can't be paused")
    if task["status"] == "running" and not worker.pause_task(task_id):
        raise HTTPException(status_code=409, detail="Only a running Claude Code task can be paused")
    if not db.set_paused(task_id, True):
        worker.resume_task(task_id)  # it finished in the meantime
        raise HTTPException(status_code=409, detail="It finished before it could be paused")
    return _with_position(db.get_task(task_id))


@app.post("/tasks/{task_id}/play", dependencies=[Depends(require_api_key)])
def play_task(task_id: int):
    """Undo pause: a held task goes back to waiting its turn, a frozen one
    carries on where it left off."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in db.PAUSABLE:
        raise HTTPException(status_code=409, detail=f"A {task['status']} task isn't paused")
    if task["status"] == "running":
        worker.resume_task(task_id)
    db.set_paused(task_id, False)
    return _with_position(db.get_task(task_id))


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
    auto_in_progress: Optional[bool] = None


class QuickButtonIn(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    icon: str = Field("✨", max_length=8)
    label: str = Field(min_length=1, max_length=40)
    prompt: str = Field(min_length=1, max_length=2000)
    pic: bool = False


class SettingsIn(BaseModel):
    # The Home "Quick buttons", saved as a whole list (an empty list clears them).
    quick_buttons: Optional[list[QuickButtonIn]] = Field(None, max_length=24)
    notify_on_resume: Optional[bool] = None
    hold_low_when_busy: Optional[bool] = None
    notify_weekly_pace: Optional[bool] = None
    monthly_budget_usd: Optional[float] = None
    # Octopus: how much it may do without asking, and when it shows its plan first.
    autonomy: Optional[Literal["assist", "execute", "autonomous"]] = None
    plan_review: Optional[Literal["risky", "always", "never"]] = None
    # Voice (see octopus/voice): provider ids and whether replies are spoken.
    voice_stt: Optional[str] = None
    voice_tts: Optional[str] = None
    voice_tts_voice: Optional[str] = None
    voice_speak_replies: Optional[bool] = None


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
    pace = worker.weekly_pace(db.get_setting("claude_usage"))
    if pace and pace["level"]:
        pace["title"], pace["message"] = worker.weekly_alert_text(pace)
    return {**db.get_settings(), "push_configured": bool(worker.ACTIVITYSMITH_API_KEY), "claude_usage": db.get_setting("claude_usage"),
            "weekly_pace": pace}


@app.get("/settings", dependencies=[Depends(require_api_key)])
def get_settings():
    return _settings_view()


class SleepIn(BaseModel):
    hours: float = Field(db.SLEEP_DEFAULT_HOURS, gt=0, le=24)


@app.post("/sleep", dependencies=[Depends(require_api_key)])
def start_sleep(body: SleepIn = SleepIn()):
    """"I'm going to sleep": keep working through the queue and make full use of the limits until `hours` pass."""
    db.start_sleep_mode(body.hours)
    return _settings_view()


@app.delete("/sleep", dependencies=[Depends(require_api_key)])
def end_sleep():
    db.end_sleep_mode()
    return _settings_view()


@app.patch("/settings", dependencies=[Depends(require_api_key)])
def update_settings(body: SettingsIn):
    for k, v in body.model_dump(exclude_none=True).items():
        db.set_setting(k, v)
    return _settings_view()


@app.post("/notify/test", dependencies=[Depends(require_api_key)])
def send_test_push():
    if not worker.ACTIVITYSMITH_API_KEY:
        raise HTTPException(status_code=409, detail="ACTIVITYSMITH_API_KEY isn't set in the launchd plist")
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
                                                route_reason=f"recipe: {recipe['name']}", title=recipe["name"])))
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
    model = router.cheapest_api_model()
    if not model:
        raise HTTPException(status_code=503, detail="No answer-only model is available to polish with")
    try:
        out = providers.complete(model, [{"role": "user", "content": text[:4000]}], POLISH_SYSTEM_PROMPT, 30)
    except providers.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"text": out.text.strip(), "model_id": model["id"]}


# -------------------------------------------------------------- summarise

class SummaryIn(BaseModel):
    refresh: bool = False  # ignore the cached summary even if nothing changed


@app.post("/tasks/{task_id}/summary", dependencies=[Depends(require_api_key)])
def summarize_task(task_id: int, body: Optional[SummaryIn] = None):
    """A short catch-up on a whole chat (any message's id works). Cached
    until the chat changes."""
    try:
        return titles.summarize_thread(task_id, refresh=bool(body and body.refresh))
    except LookupError:
        raise HTTPException(status_code=404, detail="Task not found")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except providers.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
