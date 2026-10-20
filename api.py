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
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field, field_validator

import accounts
import activities
import approval_links
import backup
import clients
import crm
import crm_reports
import db
import den_bin
import den_projects
import den_recipes
import den_inbox
import den_connections
import den_secrets
import den_crmx
import den_people
import den_share
import den_system
import den_work
import den_project_links
import den_areas
import den_views
import documents
import invites
import login
import mailer
from urllib.parse import unquote, urlencode
import project_images
import pad
import checklist
import providers
import restart_control
import risk
import team
import router
import phases
import timeline
import titles
import uploads
import webpush
import worker
from assistant import actions as assistant_actions
from assistant import api as assistant_api
from assistant import orchestrator as assistant_orchestrator
from octopus import api as octopus_api
from alexa import api as alexa_api
from den_modules import api as modules_api
from octopus import service as octopus_service
from octopus.voice import languages as voice_languages

DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"
LEGACY_DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard_legacy.html"
DEN2_HTML_PATH = Path(__file__).parent / "static" / "den" / "DenHome.dc.html"
STATIC_DIR = Path(__file__).parent / "static"

API_KEY = os.environ.get("API_KEY")  # set this in your environment / launchd plist

app = FastAPI(title="The Den")


@app.middleware("http")
async def funnel_only_reaches_alexa(request: Request, call_next):
    """Tailscale Funnel (the internet, for Alexa) marks every request it forwards. Those may only reach
    POST /alexa: everything else 404s before the key check, so a stranger guessing keys can't lock out
    this Mac (Funnel traffic arrives from 127.0.0.1) and an empty API_KEY never opens the API to them."""
    if request.headers.get("Tailscale-Funnel-Request") and not (request.method == "POST" and request.url.path == "/alexa"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


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


def _client_and_lockout(request: Request) -> tuple[str, float, list[float]]:
    """Who's asking, and their recent failures - or 429 if there have been too many."""
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = _recent_failures(client, now)
    if len(recent) >= FAIL_LIMIT:
        retry_after = max(1, int(FAIL_WINDOW_SECONDS - (now - recent[0])) + 1)
        raise HTTPException(status_code=429, detail="Too many failed attempts - try again shortly",
                            headers={"Retry-After": str(retry_after)})
    return client, now, recent


def _record_failure(client: str, recent: list[float], now: float) -> None:
    _failures[client] = recent + [now]
    if len(_failures) > 1024:                       # bound memory against many spoofed sources
        for c in list(_failures):
            _recent_failures(c, now)


def _key_ok(key: Optional[str]) -> bool:
    return bool(API_KEY) and hmac.compare_digest((key or "").encode(), API_KEY.encode())


def require_api_key(request: Request, x_api_key: Optional[str] = Header(default=None)) -> None:
    """The X-API-Key header, or the session cookie a dashboard sign-in gave this browser (login.py).
    Only a wrong key or password counts towards the lockout - sending none isn't a guess."""
    if not API_KEY:
        # Only reachable on a loopback bind - main.py refuses to start otherwise.
        return
    client, now, recent = _client_and_lockout(request)
    if x_api_key:
        if _key_ok(x_api_key):
            return
        _record_failure(client, recent, now)
    elif login.valid_session(request.cookies.get(login.COOKIE)):
        return
    raise HTTPException(status_code=401, detail="Not signed in: invalid or missing X-API-Key header")


# Personal assistant: connections, permissions, activity, dashboard views.
app.include_router(assistant_api.build_router(require_api_key))
# Octopus: plans, routing history, agents, tools, providers, task detail, voice.
app.include_router(octopus_api.build_router(require_api_key))
# The Den 2.0 dashboard's page views (static/den/): see den_views.py.
app.include_router(den_views.build_router(require_api_key))
app.include_router(den_projects.build_router(require_api_key))
app.include_router(den_bin.build_router(require_api_key))
app.include_router(den_recipes.build_router(require_api_key))
app.include_router(den_inbox.build_router(require_api_key))
app.include_router(den_connections.build_router(require_api_key))
app.include_router(den_secrets.build_router(require_api_key))
app.include_router(den_crmx.build_router(require_api_key))
app.include_router(den_people.build_router(require_api_key))
app.include_router(den_share.build_router(require_api_key))
app.include_router(den_system.build_router(require_api_key))
app.include_router(den_work.build_router(require_api_key))
app.include_router(den_project_links.build_router(require_api_key))
app.include_router(den_areas.build_router(require_api_key))
# Modules: status and enable/disable/reload (den_modules/, docs/MODULES.md).
app.include_router(modules_api.build_router(require_api_key))
# Alexa: POST /alexa is checked against Amazon's signature instead of X-API-Key; the rest needs the key.
_alexa_public, _alexa_private = alexa_api.build_routers(require_api_key)
app.include_router(_alexa_public)
app.include_router(_alexa_private)


class ProjectLinkIn(BaseModel):
    label: Optional[str] = None
    url: str


class ProjectRecordIn(BaseModel):
    # The CRM record (crm.py checks these). Send only what changes; null clears an optional one.
    description: Optional[str] = None                 # markdown
    stage: Optional[str] = None                       # pipeline: lead | proposal | active | on_hold | done | lost
    priority: Optional[str] = None                    # low | medium | high | critical
    health: Optional[str] = None                      # on_track | at_risk | off_track
    start_date: Optional[str] = None                  # YYYY-MM-DD
    due_date: Optional[str] = None
    completed_date: Optional[str] = None
    contract_value: Optional[float] = None
    currency: Optional[str] = None                    # USD, INR, EUR...
    billing_type: Optional[str] = None                # fixed | hourly | retainer
    owner: Optional[str] = None
    tags: Optional[list[str]] = None
    links: Optional[list[ProjectLinkIn]] = None       # repo, live site, staging, docs, design...
    color: Optional[str] = None                       # #rrggbb; null goes back to the automatic colour
    logo_bg: Optional[str] = None                     # #rrggbb behind the logo/letter tile; null: the project colour
    client_id: Optional[int] = None                   # the client (GET /clients) it's for; null: none


class ProjectIn(ProjectRecordIn):
    name: str
    # The folder Claude Code works in. Leave it out (or empty) for a CRM-only project: a full
    # record that nothing runs in until a folder is linked (POST /projects/{id}/folder).
    path: Optional[str] = None
    # Which workspace it belongs to (a den_areas id: "work", "personal", or one you added).
    category: str = "work"
    status: Optional[str] = None                      # the stage - db.PROJECT_STATUSES


class EnvIn(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None


class MergeIn(BaseModel):
    # The project to fold in, and what its folder is called as an environment here.
    source_id: int
    env_name: str = Field(min_length=1, max_length=40)


class ProjectActiveIn(ProjectRecordIn):
    name: Optional[str] = None
    active: Optional[bool] = None
    # What the project's own folder is called next to its environments ("" = Main).
    env_label: Optional[str] = Field(None, max_length=40)
    category: Optional[str] = None
    # not_started | in_progress | need_info | on_hold | completed
    status: Optional[str] = None
    hidden: Optional[bool] = None


class ProjectFolderIn(BaseModel):
    path: str


class ProjectDeleteIn(BaseModel):
    # The project's exact name, typed by hand - a hard delete can't be undone.
    confirm: str


class HiddenIn(BaseModel):
    ids: list[int]
    hidden: bool = True


class TaskIn(BaseModel):
    prompt: str
    priority: int = 0
    # Set this to an existing task's id to reply within its chat instead of
    # starting a new one - that task's thread then resumes the same Claude
    # Code session instead of starting from a blank conversation.
    thread_id: Optional[int] = None
    # A reply to a chat that is mid-run normally goes straight into that run; True queues it to start after the run instead.
    after_run: bool = False
    # Claude Code permission mode for this task (db.PERMISSION_MODES). Omit
    # for the default; a reply that omits it keeps its chat's last mode.
    permission_mode: Optional[str] = None
    # Upload ids from POST /uploads - pictures attached to this message.
    images: Optional[list[str]] = None
    # A reply: send it to a different model than the chat's last one (a models.json
    # id). The new model is given the whole conversation so far. A new message
    # (kind "message") uses it to choose who answers.
    model: Optional[str] = None
    # Run in this project environment (GET /projects/{id}/envs); omit for the
    # project's own folder. A reply keeps its chat's environment.
    env_id: Optional[int] = None
    # "task" (default: work that runs to completion, in the project) or "message" (a
    # question to an AI model whose answer the owner reads). A message is routed to an
    # answering model - pass `model` to pick one - and never changes files. A reply
    # always keeps its chat's kind, so this is ignored on one.
    kind: Optional[str] = None
    # Run on this Claude account (GET /accounts ids) instead of the project's own / the default.
    # Claude Code work only - ignored when an api model answers.
    account: Optional[str] = Field(None, max_length=40)


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
    # Run on this Claude account (GET /accounts ids) instead of the project's own / the default.
    account: Optional[str] = Field(None, max_length=40)
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
    # Upload ids from POST /uploads - pictures that go with the item when it's queued.
    images: Optional[list[str]] = None
    # Optional run settings, same meaning as on TodoUpdateIn. Left out = the defaults.
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    priority: Optional[int] = None
    # Only ever run on this Claude account (GET /accounts ids); on any other it just waits.
    only_account: Optional[str] = Field(None, max_length=40)
    # Starred in the backlog. Just a marker - run order is `priority`.
    important: Optional[bool] = None
    # The environment (GET /projects/{id}/envs) it runs in; left out = the project's own folder.
    env_id: Optional[int] = None


class TodoUpdateIn(BaseModel):
    text: Optional[str] = None
    checked: Optional[bool] = None
    # How it will run once queued - same meaning as on a task. null = the default (Claude Code, acceptEdits, normal).
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    priority: Optional[int] = None
    # Only run on this Claude account; on any other it waits. null or "" = any account.
    only_account: Optional[str] = Field(None, max_length=40)
    # The item's full set of pictures (replaces what it had); [] or null removes them all.
    images: Optional[list[str]] = None
    # Star / unstar it in the backlog.
    important: Optional[bool] = None


class TodoReorderIn(BaseModel):
    ids: list[int]


class NoteIn(BaseModel):
    text: str = ""
    kind: Literal["guideline", "requirement", "note"] = "note"
    images: Optional[list[str]] = None                # upload ids (POST /uploads)


class NoteUpdateIn(BaseModel):
    text: Optional[str] = None
    kind: Optional[Literal["guideline", "requirement", "note"]] = None
    images: Optional[list[str]] = None                # the full new list; [] removes them all
    active: Optional[bool] = None                      # off = kept, but not given to runs


_BOOT_ID = f"{os.getpid()}-{int(time.time())}"   # new on every restart; open pages reload when it changes


@app.get("/health")
def health():
    return {"ok": True, "boot": _BOOT_ID}


def _with_position(task: dict) -> dict:
    return {**task, "position": db.queue_position(task["id"])}


@app.get("/projects", dependencies=[Depends(require_api_key)])
def list_projects():
    # claude_account: the Claude account the project's Claude Code tasks run on (its own, or the default).
    # schedule: where it stands against its due date (crm.schedule) - the list's overdue / due-soon badges.
    default = accounts.active_id()
    # stage + progress: the Projects table and pipeline board (crm.stage_of / crm.progress).
    # client_name: the name of its client (clients.py), None without one.
    names = {c["id"]: c["name"] for c in db.list_clients()}
    # last_activity_at: when anything last happened on it (activities.last_activity), None if nothing has.
    last = activities.last_activity()
    return [{**p, "is_general": db.is_general_project(p), "claude_account": accounts.for_project(p["id"], default=default),
             "schedule": crm.schedule(p), "stage": crm.stage_of(p), "progress": crm.progress(p["id"]),
             "client_name": names.get(p.get("client_id")), "last_activity_at": last.get(p["id"])}
            for p in db.list_projects()]


@app.get("/projects/{project_id}", dependencies=[Depends(require_api_key)])
def get_project(project_id: int):
    """One project's full record: the CRM fields, plus progress (from its phases and to-dos) and schedule."""
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return {**crm.record(p), "is_general": db.is_general_project(p)}


DESCRIBE_SYSTEM_PROMPT = (
    "You write the Description for a project in a project tracker. From the facts given, write a short, concrete summary "
    "in Markdown: one or two sentences saying what the project is, then (only if the facts support it) a few '- ' bullets "
    "for scope, tech and current focus. Use only what the facts say; never invent features, clients or dates. If the facts "
    "are thin, keep it brief. No title heading, no quotes, no preamble. Return only the Markdown."
)
_DESCRIBE_FILES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
_DESCRIBE_MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "Gemfile", "composer.json")


def _describe_facts(p: dict) -> str:
    """Facts about a project for the describer: its record, its phases/to-dos, and (read-only) the
    folder's README, manifest and top-level names."""
    r = crm.record(p)
    lines = [f"Name: {r.get('name')}"]
    for k, label in (("category", "Category"), ("stage", "Stage"), ("owner", "Owner"), ("client_name", "Client")):
        if r.get(k):
            lines.append(f"{label}: {r[k]}")
    if r.get("tags"):
        lines.append("Tags: " + ", ".join(map(str, r["tags"])))
    if r.get("description"):
        lines.append("Current description (improve on it, keep what is true):\n" + r["description"][:1500])
    try:
        todos = [t["text"] for t in db.list_todos(p["id"])][:15]
    except Exception:
        todos = []
    if todos:
        lines.append("To-dos:\n" + "\n".join("- " + t[:120] for t in todos))
    if db.has_folder(p):
        folder = Path(p["path"])
        try:
            for name in _DESCRIBE_FILES:
                f = folder / name
                if f.is_file():
                    lines.append(f"{name}:\n" + f.read_text(errors="replace")[:3000])
                    break
            for name in _DESCRIBE_MANIFESTS:
                f = folder / name
                if f.is_file():
                    lines.append(f"{name}:\n" + f.read_text(errors="replace")[:1200])
            names = sorted(x.name + ("/" if x.is_dir() else "") for x in folder.iterdir() if not x.name.startswith("."))
            lines.append("Top-level files: " + ", ".join(names[:50]))
        except OSError:
            pass
    return "\n\n".join(lines)


@app.post("/projects/{project_id}/describe", dependencies=[Depends(require_api_key)])
def describe_project(project_id: int):
    """Draft a Description from what's known about the project. Returns text only - nothing is saved;
    the dashboard puts it in the editor for the owner to review."""
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    model = router.cheapest_api_model()
    if not model:
        raise HTTPException(status_code=503, detail="No answer-only model is available to write with")
    try:
        out = providers.complete(model, [{"role": "user", "content": _describe_facts(p)[:9000]}], DESCRIBE_SYSTEM_PROMPT, 60)
    except providers.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    db.log_usage("describe", model["id"], out.input_tokens, out.output_tokens,
                 providers.estimate_cost(model, out.input_tokens, out.output_tokens))
    return {"text": out.text.strip(), "model_id": model["id"]}


def _project_folder(raw: str) -> str:
    """The folder a project's tasks run in, checked. The dashboard suggests "~/code/..."; the
    worker runs Claude Code with this as its working directory, so it has to be a real absolute
    folder. Only whether it exists is looked at - nothing inside it is read."""
    path = os.path.abspath(os.path.expanduser(raw.strip()))
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"No folder at {path} on this Mac")
    return path


def _project_name(raw: str, project_id: Optional[int] = None) -> str:
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the project a name")
    if len(name) > 120:
        raise HTTPException(status_code=400, detail="The name is too long (at most 120 characters)")
    if name == db.GENERAL_PROJECT_NAME:
        raise HTTPException(status_code=409, detail=f"'{name}' is the Den's own project - pick another name")
    other = db.get_project_by_name(name)
    if other and other["id"] != project_id:
        raise HTTPException(status_code=409, detail=f"Project '{name}' already exists")
    return name


def _record_fields(body: ProjectRecordIn) -> dict:
    sent = {k: getattr(body, k) for k in crm.EDITABLE if k in body.model_fields_set}
    if "links" in sent and sent["links"] is not None:
        sent["links"] = [x.model_dump() for x in sent["links"]]
    return sent


def _require_folder(project: dict) -> None:
    """Nothing is ever queued for a CRM-only project (the worker refuses it too)."""
    if not db.has_folder(project):
        raise HTTPException(status_code=400, detail=db.NO_FOLDER_HINT)


@app.post("/projects", dependencies=[Depends(require_api_key)])
def create_project(body: ProjectIn):
    """Register a code project (with its folder) or create a CRM-only one (no path) - optionally
    with its whole record filled in. Everything is checked before anything is saved."""
    name = _project_name(body.name)
    if db.get_project_by_name(name):
        raise HTTPException(status_code=409, detail=f"Project '{name}' already exists")
    path = _project_folder(body.path) if (body.path or "").strip() else ""
    if body.category not in db.project_categories():
        raise HTTPException(status_code=400, detail="category must be one of your workspaces: " + ", ".join(db.project_categories()))
    if body.status is not None and body.status not in db.PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(db.PROJECT_STATUSES)}")
    try:
        record = crm.clean(_record_fields(body), {})
    except crm.RecordError as e:
        raise HTTPException(status_code=400, detail=str(e))
    p = db.add_project(name, path, body.category)
    status = body.status if body.status is not None else crm.STAGE_STATUS.get(record.get("stage"))
    if status is not None:
        db.set_project_status(p["id"], status)
    db.update_project_record(p["id"], record)
    p = db.get_project(p["id"])
    mailer.project_event("project_created", p)
    return {**p, "stage": crm.stage_of(p)}


@app.post("/projects/{project_id}/folder", dependencies=[Depends(require_api_key)])
def link_project_folder(project_id: int, body: ProjectFolderIn):
    """"Link a folder" (or change it): the project works in this folder from now on. Refused while
    it has queued or running tasks, which are already pointed at the old one."""
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    if db.is_general_project(p):
        raise HTTPException(status_code=400, detail="General has no folder")
    path = _project_folder(body.path)
    if db.has_folder(p) and p["path"] != path and db.project_has_active_tasks(project_id):
        raise HTTPException(status_code=409, detail="This project has queued or running tasks - wait for them to finish before changing its folder")
    db.link_project_folder(project_id, path)
    p = db.get_project(project_id)
    return {**crm.record(p), "is_general": db.is_general_project(p)}


@app.delete("/projects/{project_id}", dependencies=[Depends(require_api_key)])
def delete_project(project_id: int, body: ProjectDeleteIn):
    """Hard delete: the project and everything recorded only under it (record, to-dos, phases,
    notes, documents, calendar). Needs the name typed exactly. A project Octopus has ever worked
    on can't be deleted - archive or hide it. The project's folder on disk is never touched."""
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    if db.is_general_project(p):
        raise HTTPException(status_code=400, detail="General can't be deleted")
    if body.confirm != p["name"]:
        raise HTTPException(status_code=400, detail=f"Type the project's name exactly ({p['name']}) to delete it")
    blocker = db.project_delete_blocker(project_id)
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)
    for doc in db.list_project_documents(project_id, with_stored=True):
        documents.remove(doc)
    project_images.remove(p.get("image"))
    db.delete_project(project_id)
    mailer.project_event("project_deleted", p)
    return {"ok": True, "id": project_id}


def _env_path(raw: str) -> str:
    path = os.path.abspath(os.path.expanduser(raw.strip()))
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"No folder at {path} on this Mac")
    return path


@app.get("/projects/{project_id}/envs", dependencies=[Depends(require_api_key)])
def list_envs(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_envs(project_id)


@app.post("/projects/{project_id}/envs", dependencies=[Depends(require_api_key)])
def create_env(project_id: int, body: EnvIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    name = (body.name or "").strip()
    if not name or not body.path:
        raise HTTPException(status_code=400, detail="An environment needs a name and a folder")
    if name.lower() in _env_names(project_id):
        raise HTTPException(status_code=409, detail=f"Environment '{name}' already exists in this project")
    return db.add_env(project_id, name, _env_path(body.path))


def _env_names(project_id: int) -> set[str]:
    """Every environment name in use here, lower-cased - the project's own folder's included."""
    p = db.get_project(project_id) or {}
    return {e["name"].lower() for e in db.list_envs(project_id)} | {(p.get("env_label") or "Main").lower()}


def _check_env(project_id: int, env_id: Optional[int]) -> Optional[int]:
    if env_id is None:
        return None
    env = db.get_env(env_id)
    if not env or env["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="env_id does not refer to an environment in this project")
    return env_id


@app.post("/projects/{project_id}/merge", dependencies=[Depends(require_api_key)])
def merge_project(project_id: int, body: MergeIn):
    """Fold another project into this one as an environment (see db.merge_project_into)."""
    source = db.get_project(body.source_id)
    if not db.get_project(project_id) or not source:
        raise HTTPException(status_code=404, detail="Project not found")
    if db.is_general_project(source) or db.is_general_project(db.get_project(project_id)):
        raise HTTPException(status_code=400, detail="General can't be merged")
    try:
        env = db.merge_project_into(project_id, body.source_id, body.env_name.strip())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # Uploaded documents are kept in a folder per project - move the source's across.
    old_docs = documents.DOCUMENT_DIR / str(int(body.source_id))
    if old_docs.is_dir():
        new_docs = documents.DOCUMENT_DIR / str(int(project_id))
        new_docs.mkdir(parents=True, exist_ok=True)
        for f in old_docs.iterdir():
            f.rename(new_docs / f.name)
        old_docs.rmdir()
    return {"env": env, "project": db.get_project(project_id), "merged": source["name"]}


@app.patch("/projects/{project_id}/envs/{env_id}", dependencies=[Depends(require_api_key)])
def update_env(project_id: int, env_id: int, body: EnvIn):
    env = db.get_env(env_id)
    if not env or env["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Environment not found")
    name = body.name.strip() if body.name is not None else None
    if name is not None and (not name or name.lower() in _env_names(project_id) - {env["name"].lower()}):
        raise HTTPException(status_code=409, detail="Give the environment a unique, non-empty name")
    db.update_env(env_id, name, _env_path(body.path) if body.path is not None else None)
    return db.get_env(env_id)


@app.delete("/projects/{project_id}/envs/{env_id}", dependencies=[Depends(require_api_key)])
def delete_env(project_id: int, env_id: int):
    env = db.get_env(env_id)
    if not env or env["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Environment not found")
    db.delete_env(env_id)
    return {"ok": True}


@app.patch("/projects/{project_id}", dependencies=[Depends(require_api_key)])
def update_project(project_id: int, body: ProjectActiveIn):
    current = db.get_project(project_id)
    if not current:
        raise HTTPException(status_code=404, detail="Project not found")
    # Record fields are checked before anything is saved, so a bad value changes nothing.
    sent = _record_fields(body)
    if (sent or body.name is not None) and db.is_general_project(current):
        raise HTTPException(status_code=400, detail="General has no project record")
    try:
        record = crm.clean(sent, current)
    except crm.RecordError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = _project_name(body.name, project_id) if body.name is not None else None
    if body.category is not None:
        if body.category not in db.project_categories():
            raise HTTPException(status_code=400, detail="category must be one of your workspaces: " + ", ".join(db.project_categories()))
        db.set_project_category(project_id, body.category)
    if body.status is not None and body.status not in db.PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(db.PROJECT_STATUSES)}")
    # Stage and status move together (crm.sync_stage_status): a stage picked on the board sets the
    # status, a status picked on the project page sets the stage if the old one no longer fits.
    status, stage = crm.sync_stage_status(current, body.status, record.get("stage"))
    if body.status is not None:
        status = body.status
    if status is not None:
        db.set_project_status(project_id, status)
    if stage is not None:
        record["stage"] = stage
    if body.active is not None:
        db.set_project_active(project_id, body.active)
    if body.hidden is not None:
        if db.is_general_project(db.get_project(project_id)):
            raise HTTPException(status_code=400, detail="General can't be hidden")
        db.set_project_hidden(project_id, body.hidden)
    if name is not None and name != current["name"]:
        db.set_project_name(project_id, name)
    if body.env_label is not None:
        label = body.env_label.strip()
        if label and any(e["name"].lower() == label.lower() for e in db.list_envs(project_id)):
            raise HTTPException(status_code=409, detail=f"An environment is already called {label}")
        db.set_env_label(project_id, label)
    db.update_project_record(project_id, record)
    p = db.get_project(project_id)
    # A move along the pipeline goes on the project's activity timeline.
    activities.record_stage_change(project_id, crm.stage_of(current), crm.stage_of(p))
    if p["status"] != current["status"] and p["status"] in ("archived", "completed"):
        mailer.project_event(f"project_{p['status']}", p)
    return {**crm.record(p), "is_general": db.is_general_project(p)}


# ------------------------------------------------------- the team (team.py)

class EmployeeIn(BaseModel):
    # Send only what changes; null clears an optional field.
    name: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[str] = None                      # active | on_leave | inactive
    employment_type: Optional[str] = None             # full_time | part_time | contractor | intern
    manager_id: Optional[int] = None                  # another employee; null = reports to nobody
    start_date: Optional[str] = None                  # YYYY-MM-DD
    location: Optional[str] = None
    skills: Optional[str] = None                      # comma separated
    notes: Optional[str] = None
    color: Optional[str] = None                       # #rrggbb


class EmployeeTaskIn(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[str] = None                    # YYYY-MM-DD
    priority: Optional[str] = None                    # low | normal | high
    status: Optional[str] = None                      # open | in_progress | done
    project_id: Optional[int] = None                  # the project it's for, if any
    employee_id: Optional[int] = None                 # reassign it to someone else


class EmployeeLeaveIn(BaseModel):
    kind: Optional[str] = None                        # vacation | sick | other
    start_date: str
    end_date: Optional[str] = None                    # default: the same day
    note: Optional[str] = None


def _employee_or_404(employee_id: int) -> dict:
    e = db.get_employee(employee_id)
    if not e:
        raise HTTPException(status_code=404, detail="Employee not found")
    return e


def _team_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except team.TeamError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/employees", dependencies=[Depends(require_api_key)])
def list_employees():
    """Every employee with open_tasks, overdue_tasks and away_today."""
    return team.list_all()


@app.get("/team/overview", dependencies=[Depends(require_api_key)])
def team_overview():
    return {**team.overview(), "statuses": team.STATUSES, "employment_types": team.EMPLOYMENT_TYPES,
            "priorities": team.PRIORITIES, "leave_kinds": team.LEAVE_KINDS}


@app.post("/employees", dependencies=[Depends(require_api_key)])
def create_employee(body: EmployeeIn):
    fields = _team_call(team.clean_employee, _sent(body))
    emp = db.add_employee(fields)
    return team.page(emp["id"])


@app.get("/employees/{employee_id}", dependencies=[Depends(require_api_key)])
def get_employee(employee_id: int):
    """The employee's page: details, tasks, time off, direct reports and meetings."""
    page = team.page(employee_id)
    if not page:
        raise HTTPException(status_code=404, detail="Employee not found")
    return page


@app.patch("/employees/{employee_id}", dependencies=[Depends(require_api_key)])
def update_employee(employee_id: int, body: EmployeeIn):
    _employee_or_404(employee_id)
    fields = _team_call(team.clean_employee, _sent(body), employee_id)
    db.update_employee(employee_id, fields)
    return team.page(employee_id)


@app.delete("/employees/{employee_id}", dependencies=[Depends(require_api_key)])
def delete_employee(employee_id: int):
    """Remove an employee with their tasks, time off and meeting attendance."""
    _employee_or_404(employee_id)
    db.delete_employee(employee_id)
    return {"ok": True, "id": employee_id}


@app.get("/employee-tasks", dependencies=[Depends(require_api_key)])
def list_employee_tasks(employee_id: Optional[int] = None, project_id: Optional[int] = None):
    """Tasks assigned to employees (all, or one person's / one project's), open work first."""
    return db.list_employee_tasks(employee_id, project_id)


@app.post("/employees/{employee_id}/tasks", dependencies=[Depends(require_api_key)])
def create_employee_task(employee_id: int, body: EmployeeTaskIn):
    _employee_or_404(employee_id)
    fields = _team_call(team.clean_task, {**_sent(body, "employee_id"), "employee_id": employee_id}, True)
    return db.add_employee_task(fields)


@app.patch("/employee-tasks/{task_id}", dependencies=[Depends(require_api_key)])
def update_employee_task(task_id: int, body: EmployeeTaskIn):
    if not db.get_employee_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    fields = _team_call(team.clean_task, _sent(body))
    db.update_employee_task(task_id, fields)
    return db.get_employee_task(task_id)


@app.delete("/employee-tasks/{task_id}", dependencies=[Depends(require_api_key)])
def delete_employee_task(task_id: int):
    if not db.get_employee_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete_employee_task(task_id)
    return {"ok": True, "id": task_id}


@app.get("/employee-leave", dependencies=[Depends(require_api_key)])
def list_employee_leave():
    """Everyone's time off, in date order."""
    return db.list_leave()


@app.post("/employees/{employee_id}/leave", dependencies=[Depends(require_api_key)])
def add_employee_leave(employee_id: int, body: EmployeeLeaveIn):
    _employee_or_404(employee_id)
    fields = _team_call(team.clean_leave, {**_sent(body), "employee_id": employee_id})
    return db.add_leave(fields)


@app.delete("/employee-leave/{leave_id}", dependencies=[Depends(require_api_key)])
def delete_employee_leave(leave_id: int):
    if not db.get_leave(leave_id):
        raise HTTPException(status_code=404, detail="Time off not found")
    db.delete_leave(leave_id)
    return {"ok": True, "id": leave_id}


# ------------------------------------------------------- CRM clients and contacts (clients.py)

class ClientIn(BaseModel):
    # Send only what changes; null clears an optional field.
    name: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None                       # markdown
    status: Optional[str] = None                      # prospect | active | past
    color: Optional[str] = None                       # #rrggbb; null: the automatic colour
    health: Optional[str] = None                      # healthy | watch | at_risk (Den 2.1 client form)


class ContactIn(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None                        # their title, e.g. "Head of marketing"
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    preferred_method: Optional[str] = None            # email | phone | text | whatsapp
    client_id: Optional[int] = None
    # Quick add from a project page: also link the new contact to this project (key | stakeholder).
    # Its client defaults to the project's.
    project_id: Optional[int] = None
    project_role: Optional[str] = None


class ProjectContactIn(BaseModel):
    contact_id: int
    role: str = "stakeholder"                         # key | stakeholder - a project has one key contact


def _sent(body: BaseModel, *skip: str) -> dict:
    return {k: getattr(body, k) for k in body.model_fields_set if k not in skip}


def _client_or_404(client_id: int) -> dict:
    c = db.get_client(client_id)
    if not c:
        raise HTTPException(status_code=404, detail="Client not found")
    return c


def _record_project(project_id: int) -> dict:
    """A project the CRM can attach clients and contacts to (not General)."""
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    if db.is_general_project(p):
        raise HTTPException(status_code=400, detail="General has no project record")
    return p


def _project_out(project_id: int) -> dict:
    p = db.get_project(project_id)
    return {**crm.record(p), "is_general": db.is_general_project(p)}


@app.get("/clients", dependencies=[Depends(require_api_key)])
def list_clients():
    """Every client with project_count, active_projects, contact_count and value (contract value per
    currency: total = every project but lost ones, active = work under way)."""
    return clients.list_all()


@app.post("/clients", dependencies=[Depends(require_api_key)])
def create_client(body: ClientIn):
    try:
        fields = clients.clean_client(_sent(body))
    except clients.ClientError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return clients.page(db.add_client(fields)["id"])


@app.get("/clients/{client_id}", dependencies=[Depends(require_api_key)])
def get_client(client_id: int):
    """The client page: details, its projects (stage, due date, value), contacts and value."""
    _client_or_404(client_id)
    return clients.page(client_id)


@app.patch("/clients/{client_id}", dependencies=[Depends(require_api_key)])
def update_client(client_id: int, body: ClientIn):
    _client_or_404(client_id)
    try:
        fields = clients.clean_client(_sent(body), client_id)
    except clients.ClientError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.update_client(client_id, fields)
    return clients.page(client_id)


@app.delete("/clients/{client_id}", dependencies=[Depends(require_api_key)])
def delete_client(client_id: int):
    """Only a client no project belongs to can be deleted. Its contacts are kept, without a client."""
    c = _client_or_404(client_id)
    n = len(db.client_projects(client_id))
    if n:
        raise HTTPException(status_code=409, detail=f"{n} project{'s' if n != 1 else ''} belong{'s' if n == 1 else ''} to "
                            f"{c['name']} - give {'them' if n != 1 else 'it'} another client first, or mark the client as past")
    project_images.remove(c.get("logo"))
    db.delete_client(client_id)
    return {"ok": True, "id": client_id}


@app.post("/clients/{client_id}/logo", dependencies=[Depends(require_api_key)])
async def upload_client_logo(client_id: int, request: Request):
    current = _client_or_404(client_id)
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > project_images.MAX_BYTES:
            raise HTTPException(status_code=413, detail="That file is too large")
        chunks.append(chunk)
    try:
        name = project_images.save(client_id, b"".join(chunks), prefix="c")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.update_client(client_id, {"logo": name})
    project_images.remove(current.get("logo"))
    return clients.page(client_id)


@app.delete("/clients/{client_id}/logo", dependencies=[Depends(require_api_key)])
def delete_client_logo(client_id: int):
    current = _client_or_404(client_id)
    db.update_client(client_id, {"logo": None})
    project_images.remove(current.get("logo"))
    return clients.page(client_id)


@app.get("/contacts", dependencies=[Depends(require_api_key)])
def list_contacts(client_id: Optional[int] = None):
    """Every contact (or one client's), each with project_ids: the projects it's linked to."""
    return db.list_contacts(client_id)


@app.post("/contacts", dependencies=[Depends(require_api_key)])
def create_contact(body: ContactIn):
    """Add a contact. With project_id it's also linked to that project (project_role, stakeholder
    by default) and, unless client_id is sent, belongs to the project's client."""
    fields = _sent(body, "project_id", "project_role")
    project = _record_project(body.project_id) if body.project_id is not None else None
    role = body.project_role or "stakeholder"
    if role not in clients.ROLES:
        raise HTTPException(status_code=400, detail=f"project_role must be one of: {', '.join(clients.ROLES)}")
    if project and "client_id" not in fields:
        fields["client_id"] = project.get("client_id")
    try:
        fields = clients.clean_contact(fields, new=True)
    except clients.ClientError as e:
        raise HTTPException(status_code=400, detail=str(e))
    contact = db.add_contact(fields)
    if project:
        db.link_project_contact(project["id"], contact["id"], role)
    return next(c for c in db.list_contacts() if c["id"] == contact["id"])


@app.patch("/contacts/{contact_id}", dependencies=[Depends(require_api_key)])
def update_contact(contact_id: int, body: ContactIn):
    if not db.get_contact(contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    if body.project_id is not None or body.project_role is not None:
        raise HTTPException(status_code=400, detail="Link a contact to a project with POST /projects/{id}/contacts")
    try:
        fields = clients.clean_contact(_sent(body))
    except clients.ClientError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.update_contact(contact_id, fields)
    return next(c for c in db.list_contacts() if c["id"] == contact_id)


@app.delete("/contacts/{contact_id}", dependencies=[Depends(require_api_key)])
def delete_contact(contact_id: int):
    """Remove a contact and its links to projects (the projects are untouched)."""
    if not db.get_contact(contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    db.delete_contact(contact_id)
    return {"ok": True, "id": contact_id}


@app.post("/projects/{project_id}/contacts", dependencies=[Depends(require_api_key)])
def link_project_contact(project_id: int, body: ProjectContactIn):
    """Link a contact to a project, or change its role there. Making someone the key contact makes
    the previous key contact a stakeholder. Sends the project's whole record back."""
    _record_project(project_id)
    if not db.get_contact(body.contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    if body.role not in clients.ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of: {', '.join(clients.ROLES)}")
    db.link_project_contact(project_id, body.contact_id, body.role)
    return _project_out(project_id)


@app.delete("/projects/{project_id}/contacts/{contact_id}", dependencies=[Depends(require_api_key)])
def unlink_project_contact(project_id: int, contact_id: int):
    """Take a contact off a project (the contact itself is kept)."""
    _record_project(project_id)
    if not db.unlink_project_contact(project_id, contact_id):
        raise HTTPException(status_code=404, detail="That contact isn't on this project")
    return _project_out(project_id)


# ------------------------------------------------------- CRM activities and follow-ups (activities.py)

class ActivityIn(BaseModel):
    # Send only what changes. Log it on one of project_id / client_id / contact_id.
    kind: Optional[str] = None                        # call | meeting | email | note | decision
    occurred_at: Optional[str] = None                 # ISO; no offset = local time
    summary: Optional[str] = None
    outcome: Optional[str] = None
    contact_ids: Optional[list[int]] = None           # who was involved
    project_id: Optional[int] = None
    client_id: Optional[int] = None
    contact_id: Optional[int] = None


class FollowupIn(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[str] = None                    # YYYY-MM-DD, local
    due_time: Optional[str] = None                    # HH:MM, local; none = any time that day
    status: Optional[str] = None                      # open | done
    project_id: Optional[int] = None
    client_id: Optional[int] = None
    contact_id: Optional[int] = None


def _activity_error(e: activities.ActivityError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(e, activities.NotFound) else 400, detail=str(e))


@app.get("/crm/feed", dependencies=[Depends(require_api_key)])
def crm_feed(project_id: Optional[int] = None, client_id: Optional[int] = None, contact_id: Optional[int] = None):
    """One project's, client's or contact's activity timeline: open follow-ups ("open") and, newest
    first, activities, stage changes, follow-ups done and (for a project) finished tasks ("items")."""
    try:
        return activities.feed(project_id, client_id, contact_id)
    except activities.ActivityError as e:
        raise _activity_error(e)


@app.post("/activities", dependencies=[Depends(require_api_key)])
def create_activity(body: ActivityIn):
    try:
        return activities.create_activity(_sent(body))
    except activities.ActivityError as e:
        raise _activity_error(e)


@app.patch("/activities/{activity_id}", dependencies=[Depends(require_api_key)])
def update_activity(activity_id: int, body: ActivityIn):
    try:
        return activities.update_activity(activity_id, _sent(body))
    except activities.ActivityError as e:
        raise _activity_error(e)


@app.delete("/activities/{activity_id}", dependencies=[Depends(require_api_key)])
def delete_activity(activity_id: int):
    if not db.get_crm_activity(activity_id):
        raise HTTPException(status_code=404, detail="That activity is gone")
    db.delete_crm_activity(activity_id)
    return {"ok": True, "id": activity_id}


@app.get("/followups", dependencies=[Depends(require_api_key)])
def list_followups(status: str = "open,done"):
    """Follow-ups for Home and the Calendar, soonest due first: the open ones and those done in the
    last 30 days. Each has due_at, days_left, overdue and what it's on (subject, subject_name)."""
    statuses = [x for x in status.split(",") if x in activities.FOLLOWUP_STATUSES] or None
    return activities.list_followups(statuses)


@app.post("/followups", dependencies=[Depends(require_api_key)])
def create_followup(body: FollowupIn):
    try:
        return activities.create_followup(_sent(body))
    except activities.ActivityError as e:
        raise _activity_error(e)


@app.patch("/followups/{followup_id}", dependencies=[Depends(require_api_key)])
def update_followup(followup_id: int, body: FollowupIn):
    """Change a follow-up, or mark it done / open again ({"status": "done"})."""
    try:
        return activities.update_followup(followup_id, _sent(body))
    except activities.ActivityError as e:
        raise _activity_error(e)


@app.delete("/followups/{followup_id}", dependencies=[Depends(require_api_key)])
def delete_followup(followup_id: int):
    if not db.get_followup(followup_id):
        raise HTTPException(status_code=404, detail="That follow-up is gone")
    db.delete_followup(followup_id)
    return {"ok": True, "id": followup_id}


# ------------------------------------------------------- CRM dashboard and reports (crm_reports.py)

@app.get("/crm/dashboard", dependencies=[Depends(require_api_key)])
def crm_dashboard(scope: str = "all", client_id: Optional[int] = None):
    """The CRM page: what's overdue / due soon / at risk / gone quiet, follow-ups due, the pipeline by stage,
    active value by client and workload by due week - for one side (all | work | personal) and maybe one client.
    "counts" is what Home's CRM card shows."""
    try:
        return crm_reports.dashboard(scope, client_id)
    except crm_reports.ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/crm/reports/{kind}/{item_id}", dependencies=[Depends(require_api_key)])
def crm_report(kind: Literal["client", "project"], item_id: int, format: str = "json"):
    """A client's or a project's summary (details, value, progress, stage history, activities, follow-ups):
    JSON, or a file to download (format=csv | md)."""
    if format not in crm_reports.FORMATS:
        raise HTTPException(status_code=400, detail=f"format must be one of: {', '.join(crm_reports.FORMATS)}")
    try:
        report = crm_reports.client_report(item_id) if kind == "client" else crm_reports.project_report(item_id)
    except crm_reports.ReportError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if format == "json":
        return report
    body = crm_reports.to_csv(report) if format == "csv" else crm_reports.to_markdown(report)
    media = "text/csv" if format == "csv" else "text/markdown"
    return Response(content=body, media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{crm_reports.filename(report, format)}"'})


@app.post("/tasks/hidden", dependencies=[Depends(require_api_key)])
def set_tasks_hidden(body: HiddenIn):
    """Hide or unhide queue items (any status). Replies follow their chat."""
    ids = [i for i in body.ids if db.get_task(i)]
    db.set_task_hidden(ids, body.hidden)
    return {"ids": ids, "hidden": body.hidden}


@app.get("/projects/{project_id}/tasks", dependencies=[Depends(require_api_key)])
def list_project_tasks(project_id: int, kind: Optional[str] = None):
    """kind=task|message narrows to one kind; omit for both."""
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_tasks(project_id=project_id, kind=_check_kind(kind))


def _check_kind(kind: Optional[str]) -> Optional[str]:
    if kind and kind not in db.ITEM_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(db.ITEM_KINDS)}")
    return kind or None


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
    thread = db.get_thread(root["id"])
    latest = next((t for t in reversed(thread) if t["lane"] != "control"), thread[-1])   # not an @octopus hand-off note
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
    _require_folder(db.get_project(project_id))
    if body.env_id is not None:
        env = db.get_env(body.env_id)
        if not env or env["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="env_id does not refer to an environment in this project")

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
        # "@octopus ..." goes to Octopus itself; the chat's model never sees it.
        if octopus_service.mentions_octopus(body.prompt):
            try:
                out = octopus_service.handoff(body.prompt, thread_root, priority=body.priority, permission_mode=mode,
                                              source="dashboard", images=images)
            except octopus_service.ServiceError as e:
                raise HTTPException(status_code=e.status, detail=e.detail)
            note = out.pop("note")
            return {**note, "octopus": out}
        if body.env_id is None:
            body.env_id = thread_root.get("env_id")
        model_id, lane, reason = _reply_model(thread_root, body.model, bool(images))
        account = _check_account(body.account) if body.account and lane == "agent" else None
        reply = db.add_task(project_id, body.prompt, body.priority, thread_id,
                            lane=lane, model_id=model_id,
                            category=thread_root.get("category"), routed_by="thread", route_reason=reason,
                            permission_mode=(mode or _thread_mode(thread_id)) if lane == "agent" else None,
                            images=images, env_id=body.env_id, claude_account=account)
        # Claude Code is mid-run on this chat: pass the message straight in, as in
        # VS Code, instead of making it wait for the run to finish.
        # (Not when the reply names a different account from the one that run is on.)
        if lane == "agent" and not body.model and mode is None and not body.after_run:
            live = next((m for m in db.get_thread(thread_id)
                         if m["id"] != reply["id"] and m["status"] == "running" and m["lane"] == "agent"), None)
            if live and account and account != live.get("claude_account"):
                live = None
            if live and worker.inject_message(live["id"], uploads.prompt_for_agent(body.prompt, images)):
                db.add_task_event(live["id"], "note", "You added: " + body.prompt.strip()[:200])
                db.mark_done(reply["id"], "Passed to Claude while it was working - its answer continues in the reply above.")
                reply = db.get_task(reply["id"])
        return _with_position(reply)

    if body.kind is not None and body.kind not in db.ITEM_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(db.ITEM_KINDS)}")
    if body.kind == "message":
        project = db.get_project(project_id)
        try:
            decision = router.route(body.prompt, project["name"], body.model)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if decision["lane"] not in ("agent", "api") or (router.get_model(decision["model_id"]) or {}).get("modality") == "image":
            raise HTTPException(status_code=400, detail="That model can't answer a message - pick a text model")
        return _with_position(db.add_task(project_id, body.prompt, body.priority, kind="message",
                                          lane=decision["lane"], model_id=decision["model_id"],
                                          category=decision["category"], routed_by=decision["routed_by"],
                                          route_reason=decision["reason"], images=images, env_id=body.env_id))

    mode = mode or db.get_project_settings(project_id).get("default_mode")   # the project's default, if it has one
    return _with_position(db.add_task(project_id, body.prompt, body.priority, thread_id, routed_by="direct",
                                      route_reason="sent straight to Claude Code", permission_mode=mode, images=images,
                                      env_id=body.env_id,
                                      claude_account=_check_account(body.account) if body.account else None))


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
    _require_folder(project)
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
    too_large = HTTPException(status_code=413, detail="That file is too large")
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
        return uploads.save(b"".join(chunks), unquote(request.headers.get("x-filename") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/uploads/{image_id}", dependencies=[Depends(require_api_key)])
def get_upload(image_id: str):
    path = uploads.path_of(image_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")
    headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if not uploads.is_image(image_id):
        headers["Content-Disposition"] = "attachment"
    return FileResponse(path, media_type=uploads.mime_of(image_id), headers=headers)


@app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_task(task_id: int):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    db.cancel_task(task_id)
    return db.get_task(task_id)


# ------------------------------------------------------------ recycle bin
# Delete moves a queue item to the bin; Undo / Restore brings it back where it was.

@app.delete("/tasks/{task_id}", dependencies=[Depends(require_api_key)])
def delete_task(task_id: int):
    try:
        ids = db.delete_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404 if str(e) == "Task not found" else 409, detail=str(e))
    return {"deleted": ids}


@app.post("/tasks/{task_id}/restore", dependencies=[Depends(require_api_key)])
def restore_task(task_id: int):
    ids = db.restore_task(task_id)
    if not ids:
        raise HTTPException(status_code=404, detail="Not in the recycle bin")
    return {"restored": ids, "task": _with_position(db.get_task(task_id))}


@app.get("/bin", dependencies=[Depends(require_api_key)])
def list_bin():
    return {"items": db.list_bin(), "keep_days": db.BIN_DAYS}


@app.delete("/bin/{task_id}", dependencies=[Depends(require_api_key)])
def purge_bin_item(task_id: int):
    n = db.purge_task(task_id)
    if not n:
        raise HTTPException(status_code=404, detail="Not in the recycle bin")
    return {"erased": n}


@app.delete("/bin", dependencies=[Depends(require_api_key)])
def empty_bin():
    return {"erased": db.purge_bin()}


@app.get("/queue/status", dependencies=[Depends(require_api_key)])
def queue_status():
    # restart_needed: tasks that edited the Den's own code since it started - tagged "Restart needed".
    # code_changed: source files edited since this process started (by anyone) - the "Restart needed" banner.
    return {**db.queue_status(), "restart_needed": restart_control.needs_restart(worker._STARTED_TS),
            "code_changed": [name for _, name in mailer._changed_sources(worker._STARTED_TS)]}


# The dashboard's Restart buttons: reload this service's code without the terminal.
# pid changes once the new process is up, which is how the page knows to reload.
SERVICE_STARTED_AT = db._now()


class RestartIn(BaseModel):
    # False: finish what's running first. True: kill it and restart right away.
    hard: bool = False


def _service_state() -> dict:
    return {"pid": os.getpid(), "started_at": SERVICE_STARTED_AT, "restarting": worker.draining,
            "hard": worker.restart_hard,
            "waiting_on": [] if worker.restart_hard or not worker.draining else worker.busy_task_ids(),
            "held": restart_control.held_ids()}


@app.get("/service", dependencies=[Depends(require_api_key)])
def service_info():
    return _service_state()


@app.post("/service/restart", dependencies=[Depends(require_api_key)])
async def restart_service(body: Optional[RestartIn] = None):
    """Holds the waiting tasks (they're let go again once it's back) and takes no new work.
    Normal: restarts as soon as nothing is running - right away when idle. Hard: stops what's
    running now and restarts within seconds (see worker.request_restart)."""
    hard = bool(body and body.hard)
    stopped = worker.request_restart(hard=hard)
    return {**_service_state(), "stopped": stopped if hard else []}


@app.get("/email", dependencies=[Depends(require_api_key)])
def email_status():
    """Whether email is set up, where it goes, and which events are on."""
    import email_templates
    return {"configured": mailer.configured(), "to": mailer.recipient(),
            "events": {k: mailer.enabled(k) for k in email_templates.EVENTS}}


class EmailIn(BaseModel):
    off: list[str] = []   # event keys to switch off; everything else is on


@app.patch("/email", dependencies=[Depends(require_api_key)])
def email_settings(body: EmailIn):
    import email_templates
    unknown = [k for k in body.off if k not in email_templates.EVENTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown email event: {', '.join(unknown)}")
    db.set_setting("email_events_off", sorted(set(body.off)))
    return email_status()


@app.post("/email/test", dependencies=[Depends(require_api_key)])
def email_test():
    if not mailer.configured():
        raise HTTPException(status_code=409, detail="Connect Google (Personal > Connections) with email access first")
    if not mailer.send("test", wait=True, force=True):
        raise HTTPException(status_code=502, detail="Google refused it - check the log")
    return {"ok": True}


@app.get("/tasks", dependencies=[Depends(require_api_key)])
def list_all_tasks(limit: int = 200, status: Optional[str] = None, project_id: Optional[int] = None,
                   hidden: Optional[bool] = None, kind: Optional[str] = None):
    statuses = status.split(",") if status else None
    tasks = db.list_all_tasks(limit=limit, statuses=statuses, project_id=project_id, hidden=hidden,
                              kind=_check_kind(kind))
    # How long a run here usually takes, for the progress bar of one that hasn't written a plan.
    active = [t for t in tasks if t["lane"] == "agent" and t["status"] in ("running", "rate_limited")]
    if active:
        typical = db.typical_run_ms("agent")
        for t in active:
            t["typical_ms"] = typical.get(t["project_id"]) or typical.get(None)
    # Artifacts (diffs, file lists) are most of this payload and only the task detail page reads them.
    for t in tasks:
        t.pop("artifacts", None)
    return tasks


@app.get("/messages", dependencies=[Depends(require_api_key)])
def message_centre(q: str = "", status: str = "all", project_ids: Optional[str] = None,
                   lane: Optional[str] = None, limit: int = 40, offset: int = 0,
                   kind: Optional[str] = None, unread: bool = False, hidden: bool = False):
    """Chats, most recent message first (see db.list_chats). project_ids is a
    comma-separated list; leave it off for every project. kind=message is the
    inbox of answers to read (unread=true: only the ones not opened yet)."""
    try:
        pids = [int(x) for x in project_ids.split(",") if x] if project_ids is not None else None
    except ValueError:
        raise HTTPException(status_code=400, detail="project_ids must be comma-separated numbers")
    return db.list_chats(q=q.strip(), status=status, project_ids=pids, lane=lane or None,
                         limit=max(1, min(limit, 200)), offset=max(0, offset), kind=_check_kind(kind), unread=unread,
                         hidden=hidden)


@app.post("/tasks/{task_id}/read", dependencies=[Depends(require_api_key)])
def mark_message_read(task_id: int, read: bool = True):
    """The owner has read this message's answer (read=false marks it unread again).
    Only messages have a read state; a task is a 400."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not db.mark_read(task_id, read):
        raise HTTPException(status_code=400, detail="Only messages can be marked read")
    return db.get_task(task_id)


@app.post("/tasks/{task_id}/important", dependencies=[Depends(require_api_key)])
def mark_chat_important(task_id: int, important: bool = True):
    """Star (or unstar) a chat so it stands out in its project. Any message in the chat works."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    root = task["thread_id"] or task["id"]
    db.set_chat_important(root, important)
    return db.get_task(root)


@app.post("/messages/{task_id}/read", dependencies=[Depends(require_api_key)])
def mark_chat_read(task_id: int):
    """Everything answered in this message chat counts as read."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"marked": db.mark_thread_read(task["thread_id"] or task["id"]), "unread": db.unread_message_count()}


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
    try:
        images = uploads.validate(body.images)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    text = body.text.strip()
    if not text and not images:
        raise HTTPException(status_code=400, detail="Write something or attach a picture first")
    return db.add_project_note(project_id, text, body.kind, images=images)


@app.patch("/notes/{note_id}", dependencies=[Depends(require_api_key)])
def update_note(note_id: int, body: NoteUpdateIn):
    note = db.get_project_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    try:
        images = uploads.validate(body.images) if body.images is not None else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    text = body.text.strip() if body.text is not None else None
    left_text = note["text"] if text is None else text
    left_images = note.get("images") if images is None else images
    if not left_text and not left_images:
        raise HTTPException(status_code=400, detail="A note can't be empty - delete it instead")
    return db.update_project_note(note_id, text, body.kind, images=images, active=body.active)


@app.delete("/notes/{note_id}", dependencies=[Depends(require_api_key)])
def delete_note(note_id: int):
    if not db.get_project_note(note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete_project_note(note_id)
    return {"ok": True}


# ------------------------------------------------------------ project documents
# Files kept under a project. Upload is the raw file as the body (like /uploads), with the
# file's name in ?name=; the reply is the new record.

@app.post("/projects/{project_id}/image", dependencies=[Depends(require_api_key)])
async def upload_project_image(project_id: int, request: Request):
    current = db.get_project(project_id)
    if not current:
        raise HTTPException(status_code=404, detail="Project not found")
    if db.is_general_project(current):
        raise HTTPException(status_code=400, detail="General has no project record")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > project_images.MAX_BYTES:
            raise HTTPException(status_code=413, detail="That file is too large")
        chunks.append(chunk)
    try:
        name = project_images.save(project_id, b"".join(chunks))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.update_project_record(project_id, {"image": name})
    project_images.remove(current.get("image"))
    p = db.get_project(project_id)
    return {**crm.record(p), "is_general": db.is_general_project(p)}


@app.delete("/projects/{project_id}/image", dependencies=[Depends(require_api_key)])
def delete_project_image(project_id: int):
    current = db.get_project(project_id)
    if not current:
        raise HTTPException(status_code=404, detail="Project not found")
    db.update_project_record(project_id, {"image": None})
    project_images.remove(current.get("image"))
    p = db.get_project(project_id)
    return {**crm.record(p), "is_general": db.is_general_project(p)}


@app.get("/projects/{project_id}/documents", dependencies=[Depends(require_api_key)])
def list_documents(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_project_documents(project_id)


@app.post("/projects/{project_id}/documents", dependencies=[Depends(require_api_key)])
async def upload_document(project_id: int, request: Request, name: str = ""):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    too_large = HTTPException(status_code=413, detail=f"That file is over {documents.MAX_BYTES // (1024 * 1024)} MB")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad Content-Length header")
    if declared > documents.MAX_BYTES:
        raise too_large
    chunks, total = [], 0
    async for chunk in request.stream():             # the cap is enforced on what actually arrives
        total += len(chunk)
        if total > documents.MAX_BYTES:
            raise too_large
        chunks.append(chunk)
    try:
        return documents.save(project_id, name, request.headers.get("content-type"), b"".join(chunks))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/documents/{doc_id}", dependencies=[Depends(require_api_key)])
def download_document(doc_id: int):
    doc = db.get_project_document(doc_id)
    path = documents.path_of(doc) if doc else None
    if path is None:
        raise HTTPException(status_code=404, detail="Document not found")
    # Always a download, never rendered in the page, whatever the file claims to be.
    return FileResponse(path, media_type="application/octet-stream", filename=doc["name"],
                        headers={"X-Content-Type-Options": "nosniff"})


class DocumentRename(BaseModel):
    name: str


@app.patch("/documents/{doc_id}", dependencies=[Depends(require_api_key)])
def rename_document(doc_id: int, body: DocumentRename):
    if not db.get_project_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Give the document a name")
    return db.rename_project_document(doc_id, documents.clean_name(body.name))


@app.delete("/documents/{doc_id}", dependencies=[Depends(require_api_key)])
def delete_document(doc_id: int):
    doc = db.get_project_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    documents.remove(doc)
    return {"ok": True}


# ------------------------------------------------------------ notes pad
# Free-form notes (written on the iPad, say): filed under a project or left unfiled, and
# optionally saved out as a Markdown file in a project folder or any folder under home.

class PadNoteIn(BaseModel):
    title: str = ""
    body: str = ""
    project_id: Optional[int] = None
    event_id: Optional[int] = None     # the meeting / calendar item it is about
    chat_id: Optional[int] = None      # the chat it was saved from (Message center)
    kind: str = "note"                 # note | follow-up | decision
    pinned: bool = False
    images: list[str] = []             # upload ids (POST /uploads), in order
    content: Optional[list[dict[str, Any]]] = None   # the editor's document (Quill delta ops); body is its Markdown copy
    meta: Optional[dict[str, Any]] = None            # side panel: {links: [{id, url, title}], comments: [{id, text, at, done}]}


class PadNoteUpdateIn(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    project_id: Optional[int] = None
    event_id: Optional[int] = None     # null unties it
    kind: Optional[str] = None
    pinned: Optional[bool] = None
    images: Optional[list[str]] = None # the whole new list; [] removes them all
    content: Optional[list[dict[str, Any]]] = None   # null = rebuild it from body
    meta: Optional[dict[str, Any]] = None


class PadSaveIn(BaseModel):
    project_id: Optional[int] = None   # save into this project's folder
    folder: str = ""                   # a subfolder of the project, or (no project) a folder under home
    filename: Optional[str] = None


def _pad_check_project(pid):
    if pid is not None and not db.get_project(pid):
        raise HTTPException(status_code=404, detail="Project not found")


def _pad_check_event(eid):
    if eid is not None and not db.get_project_event(eid):
        raise HTTPException(status_code=404, detail="Meeting not found")


@app.get("/pad", dependencies=[Depends(require_api_key)])
def pad_list():
    return {"items": pad.list_notes()}


def _pad_images(ids: Optional[list]) -> list:
    """A note's pictures: uploads that exist, each once, at most pad.MAX_IMAGES."""
    ids = list(dict.fromkeys(ids or []))
    if len(ids) > pad.MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"A note can hold at most {pad.MAX_IMAGES} pictures")
    if any(uploads.path_of(i) is None for i in ids):
        raise HTTPException(status_code=400, detail="A picture wasn't found - attach it again")
    return ids


PAD_CONTENT_MAX = 2_000_000           # bytes of JSON - text and formatting; pictures and files are only ids
_PAD_URL_RE = re.compile(r"(?i)^(https?://|mailto:|tel:)\S+$")


def _pad_content(ops):
    if ops is not None and len(json.dumps(ops)) > PAD_CONTENT_MAX:
        raise HTTPException(status_code=400, detail="That note is too long to save - split it into two notes")
    return ops


def _pad_meta(meta):
    """The side panel's links and comments: a link is only kept with a web, mail or phone address
    (never javascript: or a file), so nothing saved here can run when it's clicked."""
    if meta is None:
        return None
    links, comments = meta.get("links") or [], meta.get("comments") or []
    if not isinstance(links, list) or not isinstance(comments, list) or len(links) > 300 or len(comments) > 500:
        raise HTTPException(status_code=400, detail="Too many links or comments on one note")
    for x in links:
        if not isinstance(x, dict) or not _PAD_URL_RE.match(str(x.get("url") or "")):
            raise HTTPException(status_code=400, detail="A link needs a web address (https://…), mailto: or tel:")
    if len(json.dumps(meta)) > PAD_CONTENT_MAX:
        raise HTTPException(status_code=400, detail="Too many links or comments on one note")
    return {"links": links, "comments": comments}


@app.post("/pad", dependencies=[Depends(require_api_key)])
def pad_create(body: PadNoteIn):
    _pad_check_project(body.project_id)
    _pad_check_event(body.event_id)
    return pad.create_note(body.title, body.body, body.project_id, body.event_id, chat_id=body.chat_id, kind=body.kind,
                           pinned=body.pinned, images=_pad_images(body.images), content=_pad_content(body.content),
                           meta=_pad_meta(body.meta))


@app.patch("/pad/{note_id}", dependencies=[Depends(require_api_key)])
def pad_update(note_id: int, body: PadNoteUpdateIn):
    if not pad.get_note(note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    fields = body.model_dump(exclude_unset=True)
    _pad_check_project(fields.get("project_id"))
    _pad_check_event(fields.get("event_id"))
    if "images" in fields:
        fields["images"] = _pad_images(fields["images"])
    if "content" in fields:
        _pad_content(fields["content"])
    if "meta" in fields:
        fields["meta"] = _pad_meta(fields["meta"]) or {}
    return pad.update_note(note_id, fields)


@app.delete("/pad/{note_id}", dependencies=[Depends(require_api_key)])
def pad_delete(note_id: int):
    note = pad.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    proj = db.get_project(note["project_id"]) if note.get("project_id") else None
    den_bin.snapshot("note", note, note.get("title") or "Untitled", "Notes" + (f" · {proj['name']}" if proj else ""), note.get("body") or "")
    pad.delete_note(note_id)
    return {"ok": True}


@app.post("/pad/{note_id}/save-file", dependencies=[Depends(require_api_key)])
def pad_save_file(note_id: int, body: PadSaveIn):
    note = pad.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    project = db.get_project(body.project_id) if body.project_id is not None else None
    if body.project_id is not None and not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project and not db.has_folder(project):
        # Never fall back to the shared notes folder for a project that has none.
        raise HTTPException(status_code=400, detail="This project has no folder to save into - " + db.NO_FOLDER_HINT.lower())
    try:
        folder = pad.resolve_folder(project["path"] if project else None, body.folder)
        path = pad.save_file(note, folder, body.filename)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": str(path), "note": pad.get_note(note_id)}


# ------------------------------------------------------------ to-do checklist
# Tick-off items for the right sidebar and the project pages. Text with several lines
# makes one item per line. Separate from a project's Backlog (see checklist.py).

class ChecklistIn(BaseModel):
    text: str = ""
    project_id: Optional[int] = None
    priority: int = 0                  # 0 none · 1 low · 2 medium · 3 high
    due_date: Optional[str] = None     # YYYY-MM-DD
    # Already-parsed lines ({text, project_id, priority, due_date, done}) - used instead of text.
    items: Optional[list[dict[str, Any]]] = Field(None, max_length=200)
    first: bool = False                # add at the top of the list


class ChecklistUpdateIn(BaseModel):
    text: Optional[str] = None
    priority: Optional[int] = None
    due_date: Optional[str] = None     # "" clears it
    done: Optional[bool] = None
    status: Optional[str] = None       # "todo" | "doing" | "done"; wins over done
    project_id: Optional[int] = None   # null moves it back to unfiled


class ChecklistApplyIn(BaseModel):
    upsert: list[dict] = []   # {id, ...fields}: changed fields, or a whole item to put back
    delete: list[int] = []


@app.get("/checklist", dependencies=[Depends(require_api_key)])
def checklist_list(project_id: Optional[int] = None):
    return {"items": checklist.list_items(project_id)}


@app.post("/checklist", dependencies=[Depends(require_api_key)])
def checklist_create(body: ChecklistIn):
    _pad_check_project(body.project_id)
    for it in body.items or []:
        _pad_check_project(it.get("project_id"))
    items = checklist.add_items(body.text, body.project_id, body.priority, body.due_date, items=body.items, first=body.first)
    if not items:
        raise HTTPException(status_code=400, detail="Nothing to add")
    return {"items": items}


@app.delete("/checklist", dependencies=[Depends(require_api_key)])
def checklist_clear_done(project_id: Optional[int] = None):
    """Delete every ticked item (of one project when project_id is given)."""
    return {"removed": checklist.clear_done(project_id)}


@app.patch("/checklist/{item_id}", dependencies=[Depends(require_api_key)])
def checklist_update(item_id: int, body: ChecklistUpdateIn):
    if not checklist.get_item(item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    fields = body.model_dump(exclude_unset=True)
    _pad_check_project(fields.get("project_id"))
    if "text" in fields and not (fields["text"] or "").strip():
        raise HTTPException(status_code=400, detail="Text can't be empty")
    if fields.get("done") is None:
        fields.pop("done", None)
    if fields.get("status") is None:
        fields.pop("status", None)
    try:
        return checklist.update_item(item_id, fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/checklist/apply", dependencies=[Depends(require_api_key)])
def checklist_apply(body: ChecklistApplyIn):
    """Undo / redo and drag to reorder: set items to a snapshot, putting deleted ones back under their old id."""
    try:
        checklist.apply(body.upsert, body.delete)
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e) or "Bad item")
    return {"items": checklist.list_items()}


class ChecklistSendIn(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=200)
    to: str                            # "queue" (Up next) | "backlog"
    project_id: Optional[int] = None   # for unfiled items (else General)


@app.post("/checklist/send", dependencies=[Depends(require_api_key)])
def checklist_send(body: ChecklistSendIn):
    """Hand to-dos to Octopus - one or many: each becomes a task in its project's Up next, or a
    Backlog item. They stay on the list, show how that's going, and tick off when it's done."""
    _pad_check_project(body.project_id)
    try:
        return checklist.send(body.ids, body.to, body.project_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/checklist/{item_id}", dependencies=[Depends(require_api_key)])
def checklist_delete(item_id: int):
    if not checklist.get_item(item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    checklist.delete_item(item_id)
    return {"ok": True}


# ------------------------------------------------- calendar / project timelines

class TimelineEventIn(BaseModel):
    kind: str = "deadline"
    title: str
    date: str                                  # YYYY-MM-DD, local
    time: Optional[str] = None                 # HH:MM, local; none = all day
    notes: Optional[str] = None
    remind_days: Optional[list[int]] = None    # none = the kind's defaults (timeline.KINDS)
    contact_ids: Optional[list[int]] = None    # who a meeting is with (CRM contacts); other kinds ignore it
    employee_ids: Optional[list[int]] = None   # which employees attend a meeting (see team.py)
    repeat_days: Optional[int] = None          # none = once; N = every N days (meetings only); repeat_rule takes over
    repeat_rule: Optional[dict] = None         # {"freq": days|weekdays|weeks|months, "every", "on", "nth"} - see timeline.REPEAT_FREQS
    repeat_until: Optional[str] = None         # YYYY-MM-DD, the last day it may repeat on; none = no end
    guests: Optional[list[str]] = None         # a meeting's other invitees: email addresses (besides its contacts)
    duration: Optional[int] = None             # a meeting's length in minutes; none = an hour
    invite: bool = False                       # send it as a Google Calendar invite (invites.py) - Google emails every guest
    meet: bool = False                         # ...with a Google Meet link (implies invite)


class TimelineEventUpdateIn(BaseModel):
    # Only what is sent changes; send time / notes / remind_days as null to clear them.
    kind: Optional[str] = None
    title: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    notes: Optional[str] = None
    remind_days: Optional[list[int]] = None
    status: Optional[Literal["open", "done", "cancelled"]] = None
    project_id: Optional[int] = None
    contact_ids: Optional[list[int]] = None    # replaces who a meeting is with
    employee_ids: Optional[list[int]] = None   # replaces which employees attend a meeting
    repeat_days: Optional[int] = None          # null = stop repeating (it keeps its first day)
    repeat_rule: Optional[dict] = None         # as on create; send repeat_days / repeat_rule as null to stop repeating
    repeat_until: Optional[str] = None
    guests: Optional[list[str]] = None         # replaces a meeting's other invitees
    duration: Optional[int] = None
    invite: Optional[bool] = None              # true sends the invite (or retries it), false cancels it; unset leaves it
    meet: Optional[bool] = None                # true adds a Meet link (sending the invite if it hasn't gone out)
    skip_dates: Optional[list[str]] = None     # days left out of a repeating item ("skip this one"); replaces the list


def _timeline_error(e: timeline.TimelineError) -> HTTPException:
    return HTTPException(status_code=404 if "not found" in str(e) else 422, detail=str(e))


@app.get("/timeline", dependencies=[Depends(require_api_key)])
def list_timeline(start: Optional[str] = None, end: Optional[str] = None, project_id: Optional[int] = None,
                  status: str = "open,done"):
    """The calendar: every project's items between start and end (YYYY-MM-DD,
    inclusive), plus each active project's on-track / at-risk read."""
    statuses = [s for s in status.split(",") if s in timeline.STATUSES] or None
    events = timeline.views(db.list_project_events(project_id, start, end, statuses))
    health_by = {p["id"]: timeline.health(p["id"]) for p in db.list_projects(active_only=True)
                 if not db.is_general_project(p) and (project_id is None or p["id"] == project_id)}
    return {"events": events, "health": health_by, "kinds": timeline.KINDS, "today": timeline.today().isoformat(),
            "repeats": timeline.REPEATS, "invites": invites.available()}


@app.get("/projects/{project_id}/timeline", dependencies=[Depends(require_api_key)])
def project_timeline(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"events": timeline.views(db.list_project_events(project_id)), "health": timeline.health(project_id)}


@app.post("/projects/{project_id}/timeline", dependencies=[Depends(require_api_key)])
def add_timeline_event(project_id: int, body: TimelineEventIn):
    """With invite / meet, a meeting also goes out as a Google Calendar invite. The meeting is
    saved either way; if the invite couldn't be sent, it comes back with invite_error."""
    invite = body.invite or body.meet
    if invite:
        if body.kind not in timeline.WITH_PEOPLE:
            raise HTTPException(status_code=422, detail="Only meetings can send invites")
        try:
            invites.require()
        except invites.InviteError as e:
            raise HTTPException(status_code=422, detail=str(e))
    try:
        ev = timeline.create(project_id, body.kind, body.title, body.date, body.time, body.notes, body.remind_days,
                             contact_ids=body.contact_ids, employee_ids=body.employee_ids, repeat_days=body.repeat_days, repeat_until=body.repeat_until,
                             repeat_rule=body.repeat_rule, guests=body.guests, duration=body.duration)
    except timeline.TimelineError as e:
        raise _timeline_error(e)
    if invite:
        ev = invites.send(ev, body.meet)
    return timeline.view(ev)


@app.patch("/timeline/{event_id}", dependencies=[Depends(require_api_key)])
def update_timeline_event(event_id: int, body: TimelineEventUpdateIn):
    """A meeting already sent as an invite keeps its Google event in step (guests get Google's
    update email); see invites.after_update for invite / meet."""
    changes = body.model_dump(exclude_unset=True)
    invite, meet = changes.pop("invite", None), changes.pop("meet", None)
    old = db.get_project_event(event_id)
    if old and (invite or meet) and not old.get("google_event_id"):
        try:
            invites.require()
        except invites.InviteError as e:
            raise HTTPException(status_code=422, detail=str(e))
    try:
        ev = timeline.update(event_id, changes)
    except timeline.TimelineError as e:
        raise _timeline_error(e)
    return timeline.view(invites.after_update(old, ev, invite, meet))


@app.delete("/timeline/{event_id}", dependencies=[Depends(require_api_key)])
def delete_timeline_event(event_id: int):
    """Deleting a meeting that was sent as an invite cancels it in Google Calendar (guests are
    told). If that fails it's deleted here anyway, and `warning` says the invite still stands."""
    ev = db.get_project_event(event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Timeline item not found")
    why = invites.cancel(ev)
    db.delete_project_event(event_id)
    if why:
        return {"ok": True, "warning": f"Deleted, but the Google Calendar invite couldn't be cancelled: {why}"}
    return {"ok": True, "invite_cancelled": bool(ev.get("google_event_id"))}


@app.get("/briefing", dependencies=[Depends(require_api_key)])
def get_briefing(calendar: bool = True):
    """The "Good morning" summary: today's items and meetings, what's overdue,
    the week ahead, and what is waiting on you. calendar=false skips reading
    Google Calendar."""
    return timeline.briefing(calendar=calendar)


@app.get("/projects/{project_id}/todos", dependencies=[Depends(require_api_key)])
def list_todos(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return db.list_todos(project_id)


@app.post("/projects/{project_id}/todos", dependencies=[Depends(require_api_key)])
def create_todo(project_id: int, body: TodoIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        images = uploads.validate(body.images)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    settings = {}
    if body.model not in (None, "auto"):
        if not router.get_model(body.model):
            raise HTTPException(status_code=400, detail=f"Unknown model '{body.model}'")
        settings["model_id"] = body.model
    if body.permission_mode is not None:
        settings["permission_mode"] = _check_mode(body.permission_mode)
    if body.priority is not None:
        if body.priority not in (-1, 0, 1, 2):
            raise HTTPException(status_code=400, detail="priority must be -1 (Low), 0 (Normal), 1 (High) or 2 (Urgent)")
        settings["priority"] = body.priority
    if body.only_account:
        settings["only_account"] = _check_account(body.only_account)
    if body.important:
        settings["important"] = True
    todo = db.add_todo(project_id, body.text, images=images, env_id=_check_env(project_id, body.env_id))
    return db.update_todo(todo["id"], settings=settings) if settings else todo


def _check_account(account_id: str) -> str:
    if not accounts.exists(account_id):
        raise HTTPException(status_code=400, detail="No such Claude account")
    return account_id


class TaskAccountIn(BaseModel):
    # The one account the task may run on - it's moved onto it. null or "" = no limit.
    only_account: Optional[str] = Field(None, max_length=40)


@app.put("/tasks/{task_id}/only-account", dependencies=[Depends(require_api_key)])
def set_task_only_account(task_id: int, body: TaskAccountIn):
    """Change which account a waiting task is limited to. A task limited to an
    account other than the one it would run on isn't started or sent to Claude;
    changing it here to the account it should use (or clearing it) releases it."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in ("queued", "rate_limited"):
        raise HTTPException(status_code=409, detail="Only a waiting task can change account")
    db.set_only_account(task_id, _check_account(body.only_account) if body.only_account else None)
    worker.wake_agent_lane()
    return _with_position(db.get_task(task_id))


class RunAfterIn(BaseModel):
    # The task this one waits for (it starts only once that one succeeds); null = queue order.
    after_id: Optional[int] = None


@app.put("/tasks/{task_id}/run-after", dependencies=[Depends(require_api_key)])
def set_task_run_after(task_id: int, body: RunAfterIn):
    """Home's "Run after" menu: hold a waiting task until another finishes."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in ("queued", "rate_limited"):
        raise HTTPException(status_code=409, detail="Only a waiting task can wait for another")
    try:
        db.set_run_after(task_id, body.after_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    worker.wake_agent_lane()
    return _with_position(db.get_task(task_id))


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
        if body.priority not in (-1, 0, 1, 2):
            raise HTTPException(status_code=400, detail="priority must be -1 (Low), 0 (Normal), 1 (High) or 2 (Urgent)")
        settings["priority"] = body.priority
    if "only_account" in sent:
        settings["only_account"] = _check_account(body.only_account) if body.only_account else None
    if "important" in sent:
        settings["important"] = bool(body.important)
    if "images" in sent:
        try:
            settings["images"] = uploads.validate(body.images)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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
    _require_folder(project)
    decisions = phases.todo_decisions(project, [t for t in db.list_todos(project_id) if t["checked"]])
    return db.run_checked_todos(project_id, decisions=decisions)


# ------------------------------------------------------------------ phases + autopilot

class PhaseIn(BaseModel):
    name: str
    goal: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    # Items to add to the new phase's to-do list, in order.
    todos: Optional[list[str]] = None
    # The environment it's the plan for; left out = the project's own folder.
    env_id: Optional[int] = None


class PhaseUpdateIn(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class PhaseGenerateIn(BaseModel):
    model_id: Optional[str] = None      # null = the cheapest usable model


class PhaseAssignIn(BaseModel):
    phase_id: Optional[int] = None      # null = take it out of its phase


def _phase_error(e: phases.PhaseError) -> HTTPException:
    return HTTPException(status_code=404 if "not found" in str(e) else 400, detail=str(e))


def _phase_or_404(phase_id: int) -> dict:
    phase = db.get_phase(phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail="Phase not found")
    return phase


@app.get("/phases", dependencies=[Depends(require_api_key)])
def all_phases():
    """Every project's phases with their progress, and each project's autopilot - the dashboard's overview."""
    projects = [p for p in db.list_projects(active_only=True) if db.list_phases(p["id"])]
    return {"projects": {p["id"]: phases.project_view(p["id"]) for p in projects}, "modes": phases.MODES}


@app.get("/projects/{project_id}/phases", dependencies=[Depends(require_api_key)])
def project_phases(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return phases.project_view(project_id)


@app.post("/projects/{project_id}/phases", dependencies=[Depends(require_api_key)])
def add_phase(project_id: int, body: PhaseIn):
    try:
        phase = phases.create(project_id, body.name, body.goal, body.start_date, body.end_date, body.todos, body.description,
                              env_id=_check_env(project_id, body.env_id))
    except phases.PhaseError as e:
        raise _phase_error(e)
    return phases.view(phase)


@app.post("/projects/{project_id}/phases/reorder", dependencies=[Depends(require_api_key)])
def reorder_phases(project_id: int, body: TodoReorderIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    db.reorder_phases(project_id, body.ids)
    return phases.project_view(project_id)


@app.patch("/phases/{phase_id}", dependencies=[Depends(require_api_key)])
def update_phase(phase_id: int, body: PhaseUpdateIn):
    try:
        return phases.view(phases.update(phase_id, body.model_dump(exclude_unset=True)))
    except phases.PhaseError as e:
        raise _phase_error(e)


@app.get("/phases/generation-models", dependencies=[Depends(require_api_key)])
def phase_generation_models():
    """Models the 'Generate tasks' button can use."""
    return [{"id": m["id"], "label": m.get("label") or m["id"], "available": m["available"],
             "unavailable_reason": m["unavailable_reason"]} for m in phases.generation_models()]


@app.post("/phases/{phase_id}/generate-tasks", dependencies=[Depends(require_api_key)])
def generate_phase_tasks(phase_id: int, body: PhaseGenerateIn):
    """Have a model write the phase's tasks from its goal and description and add them to its to-do list."""
    try:
        return phases.generate_tasks(phase_id, body.model_id)
    except phases.PhaseError as e:
        raise _phase_error(e)


@app.delete("/phases/{phase_id}", dependencies=[Depends(require_api_key)])
def delete_phase(phase_id: int):
    _phase_or_404(phase_id)
    db.delete_phase(phase_id)
    return {"ok": True}


def _phase_action(phase_id: int, fn):
    try:
        phase = fn(phase_id)
    except phases.PhaseError as e:
        raise _phase_error(e)
    worker.wake_agent_lane()
    worker.wake_api_lane()
    return phases.view(phase)


@app.post("/phases/{phase_id}/start", dependencies=[Depends(require_api_key)])
def start_phase(phase_id: int):
    """Your command: make it the active phase and begin its to-do list now."""
    return _phase_action(phase_id, phases.start)


@app.post("/phases/{phase_id}/pause", dependencies=[Depends(require_api_key)])
def pause_phase(phase_id: int):
    return _phase_action(phase_id, phases.pause)


@app.post("/phases/{phase_id}/complete", dependencies=[Depends(require_api_key)])
def complete_phase(phase_id: int):
    return _phase_action(phase_id, phases.complete)


@app.post("/phases/{phase_id}/reopen", dependencies=[Depends(require_api_key)])
def reopen_phase(phase_id: int):
    return _phase_action(phase_id, phases.reopen)


class ReportIn(BaseModel):
    phase_ids: Optional[list[int]] = None   # null / empty = the whole project
    overview: bool = True                   # a model writes a few sentences on top, if one is free


@app.post("/projects/{project_id}/report", dependencies=[Depends(require_api_key)])
def project_report(project_id: int, body: Optional[ReportIn] = None):
    """A markdown progress report on the project, or only on the phases picked."""
    body = body or ReportIn()
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return phases.report(project_id, body.phase_ids, body.overview)
    except phases.PhaseError as e:
        raise _phase_error(e)


@app.put("/todos/{todo_id}/phase", dependencies=[Depends(require_api_key)])
def assign_todo_phase(todo_id: int, body: PhaseAssignIn):
    try:
        phases.assign_todo(todo_id, body.phase_id)
    except phases.PhaseError as e:
        raise _phase_error(e)
    return db.get_todo(todo_id)


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
            source=body.source, review=body.review, images=body.images, account=body.account)
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


@app.get("/old", response_class=HTMLResponse)
def dashboard_old():
    # The dashboard before The Den 2.0 (static/js/), kept at /old. No auth on the page
    # shell itself (it's static HTML/JS) - the data requests it makes still need the
    # API key, entered once and kept in the browser's localStorage.
    return HTMLResponse(DASHBOARD_HTML_PATH.read_text(), headers={"Cache-Control": "no-cache"})


@app.get("/dashboard/new")
def dashboard_new_moved(request: Request):
    # Where The Den 2.0 lived while it was being built - old bookmarks land on /dashboard.
    q = request.url.query
    return RedirectResponse("/dashboard" + (f"?{q}" if q else ""), status_code=301)


# ---------------------------------------------------------------- dashboard sign-in (login.py)
# A username and password give the browser a session cookie, so the API key never has to live in
# it (or in a link). The key still opens everything over the X-API-Key header, and is what creates
# the login or resets a forgotten password. None of these need the key themselves.

class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    remember: bool = True


class LoginSetupIn(LoginIn):
    key: Optional[str] = Field(default=None, max_length=500)                 # the API key, or
    current_password: Optional[str] = Field(default=None, max_length=256)  # the password it replaces


class LogoutIn(BaseModel):
    everywhere: bool = False


def _set_session_cookie(request: Request, response: Response, token: str, max_age: Optional[int]) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(login.COOKIE, token, max_age=max_age, httponly=True, samesite="strict", secure=secure, path="/")


def _start_session(request: Request, response: Response, remember: bool) -> None:
    token, max_age = login.new_session(remember, request.headers.get("user-agent", ""))
    _set_session_cookie(request, response, token, max_age)


@app.get("/auth/state")
def auth_state(request: Request, response: Response, x_api_key: Optional[str] = Header(default=None)):
    """Is there a login yet, and is this browser signed in (by session, or by a key it still sends)?
    A remembered session is renewed here, so a device used now and then stays signed in."""
    client, now, recent = _client_and_lockout(request)
    token = request.cookies.get(login.COOKIE)
    by_session = login.valid_session(token)
    by_key = False
    if x_api_key:
        by_key = _key_ok(x_api_key)
        if not by_key:
            _record_failure(client, recent, now)
    if by_session:
        max_age = login.renew(token)
        if max_age:
            _set_session_cookie(request, response, token, max_age)
    signed_in = by_session or by_key or not API_KEY
    return {"configured": login.configured(), "signed_in": signed_in,
            "via": "session" if by_session else "key" if by_key else None,
            "username": login.username() if signed_in else None}


@app.post("/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response):
    client, now, recent = _client_and_lockout(request)
    if not login.configured():
        raise HTTPException(status_code=409, detail="No login has been set up yet - create one with the API key.")
    if not login.check_password(body.username, body.password):
        _record_failure(client, recent, now)
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    _start_session(request, response, body.remember)
    return {"ok": True, "username": login.username()}


@app.post("/auth/setup")
def auth_setup(body: LoginSetupIn, request: Request, response: Response, x_api_key: Optional[str] = Header(default=None)):
    """Create the login, or replace it (new username and/or password). Needs the API key, or the
    current password. Every device is signed out; this one gets a fresh session."""
    client, now, recent = _client_and_lockout(request)
    key = body.key or x_api_key
    allowed = (not API_KEY) or (bool(key) and _key_ok(key)) or (
        bool(body.current_password) and login.configured() and login.check_password(login.username() or "", body.current_password))
    if not allowed:
        if key or body.current_password:
            _record_failure(client, recent, now)
        raise HTTPException(status_code=401, detail="That API key or current password isn't right.")
    problem = login.validate(body.username, body.password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    login.set_login(body.username, body.password)
    _start_session(request, response, body.remember)
    return {"ok": True, "username": login.username()}


@app.post("/auth/key-session")
def auth_key_session(request: Request, response: Response, x_api_key: Optional[str] = Header(default=None)):
    """A browser that still keeps the API key swaps it for a session once a login exists, then forgets the key."""
    client, now, recent = _client_and_lockout(request)
    if not x_api_key or not _key_ok(x_api_key):
        if x_api_key:
            _record_failure(client, recent, now)
        raise HTTPException(status_code=401, detail="Not signed in: invalid or missing X-API-Key header")
    if not login.configured():
        raise HTTPException(status_code=409, detail="No login has been set up yet.")
    _start_session(request, response, True)
    return {"ok": True, "username": login.username()}


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response, body: Optional[LogoutIn] = None,
                x_api_key: Optional[str] = Header(default=None)):
    if body and body.everywhere:
        require_api_key(request, x_api_key)
        login.end_all()
    else:
        login.end_session(request.cookies.get(login.COOKIE))
    response.delete_cookie(login.COOKIE, path="/")
    return {"ok": True}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    # A link with the API key in it (?key=...) would leave the key in the address bar, history and
    # bookmarks: send it on without it. (Nothing reads it - signing in happens on the page.)
    if "key" in request.query_params:
        rest = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "key"])
        return RedirectResponse("/dashboard" + (f"?{rest}" if rest else ""), status_code=302,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    # The Den 2.0: the designer's components (static/den/*.dc.html) running
    # live on the API. The shell needs no auth; the data it loads does.
    # Every component and script is linked with its file time (?v=...), so a
    # browser never keeps running an old copy after an update (Chrome and iOS
    # Safari both did, despite no-cache).
    den_dir = DEN2_HTML_PATH.parent
    versions = {f"./{p.name}": f"/static/den/{p.name}?v={p.stat().st_mtime_ns}" for p in den_dir.glob("*.dc.html")}
    page = DEN2_HTML_PATH.read_text()
    for script in ("den.js", "support.js"):
        page = page.replace(f'src="/static/den/{script}"', f'src="/static/den/{script}?v={(den_dir / script).stat().st_mtime_ns}"', 1)
    page = page.replace("<script>\nwindow.__resources = {", "<script>\nwindow.__resources = {\n" + "".join(
        f"  {json.dumps(k)}: {json.dumps(v)},\n" for k, v in versions.items()), 1)
    # The Octopus is on the loading splash, so he ships inside the page instead of as a
    # second request: if that request failed (the service mid-restart, a flaky phone
    # connection) the runtime never retried, and its grey placeholder box stood in for
    # him until a full reload. ("</" is escaped so his own </script> can't end ours.)
    inline = {versions[f"./{n}"]: json.dumps((den_dir / n).read_text()).replace("</", "<\\/") for n in ("DenOcto.dc.html",)}
    page = page.replace("\nwindow.__resources = {", "\nwindow.__resourceBlobs = {\n" + "".join(
        f"  {json.dumps(k)}: new Blob([{v}], {{ type: 'text/html' }}),\n" for k, v in inline.items()) + "};\nwindow.__resources = {", 1)
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/dashboard/legacy", response_class=HTMLResponse)
def dashboard_legacy():
    # The pre-redesign single-file dashboard, kept reachable until the new
    # one has fully replaced it.
    return LEGACY_DASHBOARD_HTML_PATH.read_text()


@app.get("/sw.js")
def service_worker():
    # Served from the root (not /static) so it may control /dashboard. No auth,
    # like the page itself: it only shows pushes and opens the dashboard.
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


class PushKeysIn(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscriptionIn(BaseModel):
    """The browser's PushSubscription.toJSON()."""
    endpoint: str = Field(min_length=1, max_length=2000)
    keys: Optional[PushKeysIn] = None

    @field_validator("endpoint")
    @classmethod
    def https_only(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("endpoint must be an https push service URL")
        return v


@app.get("/push/key", dependencies=[Depends(require_api_key)])
def push_key():
    return {"publicKey": webpush.public_key()}


@app.post("/push/subscribe", dependencies=[Depends(require_api_key)])
def push_subscribe(body: PushSubscriptionIn, request: Request):
    if not body.keys:
        raise HTTPException(status_code=400, detail="keys.p256dh and keys.auth are required to subscribe")
    origin = request.headers.get("origin") or ""
    if origin.startswith("https://"):
        db.set_setting(webpush.ORIGIN_SETTING, origin)   # the VAPID subject (webpush.subject)
    db.add_push_subscription(body.endpoint, body.keys.p256dh, body.keys.auth, request.headers.get("user-agent") or "")
    return {"subscribed": True, "count": len(db.list_push_subscriptions())}


@app.delete("/push/subscribe", dependencies=[Depends(require_api_key)])
def push_unsubscribe(body: PushSubscriptionIn):
    db.delete_push_subscription(body.endpoint)
    return {"subscribed": False, "count": len(db.list_push_subscriptions())}



# ------------------------------------------------------------ queue control

class RequeueIn(BaseModel):
    # True: Retry / "Retry now" - straight to the front. False: Undo on a
    # cancel - back to the spot it had.
    front: bool = True
    # "Edit and retry": the changed prompt, saved and requeued in one step.
    prompt: Optional[str] = None


class QueueReorderIn(BaseModel):
    ids: list[int]


@app.post("/tasks/{task_id}/requeue", dependencies=[Depends(require_api_key)])
def requeue_task(task_id: int, body: RequeueIn = RequeueIn()):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in db.REQUEUEABLE:
        raise HTTPException(status_code=409, detail=f"A {task['status']} task can't be requeued")
    _require_folder(db.get_project(task["project_id"]) or {})
    prompt = body.prompt.strip() if body.prompt is not None else None
    if body.prompt is not None and not prompt:
        raise HTTPException(status_code=400, detail="The prompt can't be empty")
    if prompt == task["prompt"]:
        prompt = None
    return _with_position(db.requeue_task(task_id, front=body.front, prompt=prompt))


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


class TransferIn(BaseModel):
    project_id: int


@app.post("/tasks/{task_id}/transfer", dependencies=[Depends(require_api_key)])
def transfer_task(task_id: int, body: TransferIn):
    """Move a chat that isn't under way - finished, or still waiting in line -
    to another project, all its messages with it."""
    task = db.get_task(task_id)
    if not task or task["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Task not found")
    project = db.get_project(body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["id"] == task["project_id"]:
        raise HTTPException(status_code=400, detail="It's already in that project")
    try:
        ids = db.transfer_chat(task_id, project["id"])
    except ValueError as e:
        raise HTTPException(status_code=404 if str(e) == "Task not found" else 409, detail=str(e))
    return {"moved": ids, "task": _with_position(db.get_task(task_id))}


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
    # The Claude account it runs on (GET /accounts ids); "" = Auto (its project's own / the default).
    claude_account: Optional[str] = Field(None, max_length=40)


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
        if body.priority not in (-1, 0, 1, 2):
            raise HTTPException(status_code=400, detail="priority must be -1 (Low), 0 (Normal), 1 (High) or 2 (Urgent)")
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

    if body.claude_account is not None:
        new = _check_account(body.claude_account) if body.claude_account else None
        fields["pinned_account"] = new
        if task.get("only_account"):   # a limit to one account follows the pick, so the task isn't left waiting
            fields["only_account"] = new

    if fields and not db.update_queued_task(task_id, fields):
        raise HTTPException(status_code=409, detail="It started while you were editing - nothing was changed")
    if "pinned_account" in fields:
        worker.wake_agent_lane()
    return _with_position(db.get_task(task_id))


class TaskModeIn(BaseModel):
    permission_mode: str


@app.post("/tasks/{task_id}/mode", dependencies=[Depends(require_api_key)])
def switch_task_mode(task_id: int, body: TaskModeIn):
    """Change the permission mode of a Claude Code task that is running now."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    mode = _check_mode(body.permission_mode)
    if task["status"] != "running" or not worker.switch_mode(task_id, mode):
        raise HTTPException(status_code=409, detail="Only a running Claude Code task can change mode")
    db.set_task_permission_mode(task_id, mode)
    return {"ok": True}


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

class DeployTargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    instructions: str = Field("", max_length=1000)


class ShortcutIn(BaseModel):
    label: str = Field("", max_length=40)
    prompt: str = Field(min_length=1, max_length=2000)


class ProjectSettingsIn(BaseModel):
    default_priority: Optional[int] = None
    notify_approvals: Optional[bool] = None
    notify_failures: Optional[bool] = None
    notify_completions: Optional[bool] = None
    quiet_hours: Optional[bool] = None
    allow_commands: Optional[list[str]] = None
    budget_usd: Optional[float] = None
    auto_in_progress: Optional[bool] = None
    # An accounts.py id: every Claude Code task of the project runs on it. "" = the default account.
    claude_account: Optional[str] = Field(None, max_length=40)
    # Phase autopilot (phases.py): manual | timeline | capacity, and the weekly-usage % it won't feed work past.
    autopilot: Optional[str] = Field(None, max_length=20)
    autopilot_ceiling: Optional[int] = None
    # One-tap shortcut buttons (label + the prompt they send), saved as a whole list.
    shortcuts: Optional[list[ShortcutIn]] = Field(None, max_length=30)
    # Where the chat's Deploy button ships the project, saved as a whole list.
    deploy_targets: Optional[list[DeployTargetIn]] = Field(None, max_length=20)
    # Defaults for new tasks here, and standing run rules (the project Settings tab). "" clears a default.
    default_model: Optional[str] = Field(None, max_length=60)
    default_mode: Optional[str] = Field(None, max_length=20)
    run_tests: Optional[bool] = None
    auto_format: Optional[bool] = None
    nightly_backlog: Optional[bool] = None


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
    # Project calendar: deadline / meeting alerts, and the daily "Good morning" push at briefing_time.
    notify_timeline: Optional[bool] = None
    briefing_push: Optional[bool] = None
    briefing_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    monthly_budget_usd: Optional[float] = None
    daily_budget_usd: Optional[float] = Field(None, ge=0)
    sleep_auto_deny: Optional[bool] = None
    quiet_hours: Optional[bool] = None
    task_timeout_min: Optional[int] = Field(None, ge=1, le=24 * 60)
    stuck_after_min: Optional[int] = Field(None, ge=2, le=24 * 60)
    budget_warn: Optional[bool] = None
    budget_pause: Optional[bool] = None
    routing_rules: Optional[list[dict[str, Any]]] = Field(None, max_length=50)
    default_model: Optional[str] = Field(None, max_length=60)
    failover: Optional[dict[str, bool]] = None
    # Smart devices / Voice: "Pause Octopus", voice quiet hours, read back before running a command.
    queue_paused: Optional[bool] = None
    voice_quiet: Optional[bool] = None
    voice_readback: Optional[bool] = None
    voice_wake: Optional[str] = Field(None, max_length=30)
    # Dashboard layout prefs, saved as a whole (see db.SETTINGS_DEFAULTS["ui_prefs"]).
    ui_prefs: Optional[dict[str, Any]] = None
    # Octopus: how much it may do without asking, and when it shows its plan first.
    autonomy: Optional[Literal["assist", "execute", "autonomous"]] = None
    plan_review: Optional[Literal["risky", "always", "never"]] = None
    # Voice (see octopus/voice): provider ids and whether replies are spoken.
    voice_stt: Optional[str] = None
    voice_tts: Optional[str] = None
    voice_tts_voice: Optional[str] = None
    voice_manual: Optional[str] = Field(None, max_length=80)
    voice_speak_replies: Optional[bool] = None
    voice_rate: Optional[float] = Field(None, ge=0.5, le=2.0)
    # Which macOS voice reads which language: {"hi": "Lekha"}. Saved as a whole; a language left out uses the default voice.
    voice_language_voices: Optional[dict[str, str]] = None

    @field_validator("voice_language_voices")
    @classmethod
    def _known_languages(cls, v):
        if v is None:
            return v
        unknown = sorted(set(v) - set(voice_languages.BY_CODE))
        if unknown:
            raise ValueError(f"unknown language: {', '.join(unknown)}")
        return {k: name.strip() for k, name in v.items() if name.strip() and len(name) <= 80}


@app.get("/projects/{project_id}/settings", dependencies=[Depends(require_api_key)])
def get_project_settings(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {**db.get_project_settings(project_id), "month_spend_usd": db.project_month_spend(project_id),
            "claude_account_in_use": accounts.for_project(project_id)}


@app.patch("/projects/{project_id}/settings", dependencies=[Depends(require_api_key)])
def update_project_settings(project_id: int, body: ProjectSettingsIn):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    fields = body.model_dump(exclude_none=True)
    try:
        if "autopilot" in fields and fields["autopilot"] not in phases.MODES:
            raise phases.PhaseError(f"Autopilot mode must be one of: {', '.join(phases.MODES)}")
        if fields.get("autopilot", "manual") != "manual" and not db.has_folder(db.get_project(project_id)):
            raise phases.PhaseError(db.NO_FOLDER_HINT)
        if "autopilot_ceiling" in fields:
            fields["autopilot_ceiling"] = phases.clean_ceiling(fields["autopilot_ceiling"])
    except phases.PhaseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if fields.get("default_mode"):
        _check_mode(fields["default_mode"])
    if fields.get("default_model") not in (None, "", "auto") and not router.get_model(fields["default_model"]):
        raise HTTPException(status_code=400, detail="Unknown model")
    moved = False
    if "claude_account" in fields:
        new = fields["claude_account"].strip()
        if new and not accounts.exists(new):
            raise HTTPException(status_code=400, detail="No such Claude account - add it in Settings › Claude account first")
        moved = accounts.for_project(project_id, own=new or None) != accounts.for_project(project_id)
    db.update_project_settings(project_id, **fields)
    if moved:
        # A task waiting out the old account's limit may try the new account at once.
        db.resume_rate_limited_now(project_id=project_id)
        worker.wake_agent_lane()
    return get_project_settings(project_id)


def _settings_view() -> dict:
    pace = worker.weekly_pace(db.get_setting("claude_usage"))
    if pace and pace["level"]:
        pace["title"], pace["message"] = worker.weekly_alert_text(pace)
    return {**db.get_settings(), "push_configured": bool(worker.ACTIVITYSMITH_API_KEY),
            "webpush_subscriptions": len(db.list_push_subscriptions()), "claude_usage": accounts.current(db.get_setting("claude_usage")),
            "weekly_pace": pace, "claude_account": accounts.active_id(), "claude_accounts": accounts.public_list(),
            "claude_mac_hidden": not accounts.mac_shown()}


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


# ---------------------------------------------------------------- export and backup (backup.py)

@app.get("/export/{kind}", dependencies=[Depends(require_api_key)])
def export_data(kind: str, format: str = "json"):
    try:
        body, media, name = backup.export(kind, format)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/backup", dependencies=[Depends(require_api_key)])
def download_backup():
    body, name = backup.backup_file()
    return Response(body, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def _upload_body(request: Request) -> bytes:
    if int(request.headers.get("content-length") or 0) > backup.MAX_RESTORE_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large to be a backup.")
    return await request.body()


@app.post("/restore/check", dependencies=[Depends(require_api_key)])
async def restore_check(request: Request):
    """Look inside an uploaded backup without changing anything - the dashboard shows this in its warning."""
    try:
        return backup.inspect(await _upload_body(request))
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/restore", dependencies=[Depends(require_api_key)])
async def restore_backup(request: Request, confirm: str = ""):
    """Replace ALL current data with the uploaded backup. Needs ?confirm=replace-everything."""
    if confirm != "replace-everything":
        raise HTTPException(status_code=400, detail="Restore replaces everything - confirm=replace-everything is required.")
    try:
        return backup.restore(await _upload_body(request))
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------- Claude accounts (accounts.py)

class AccountIn(BaseModel):
    label: str = Field(..., max_length=60)
    token: str = Field(..., max_length=400)   # from `claude setup-token`
    email: Optional[str] = Field(None, max_length=120)


@app.get("/accounts", dependencies=[Depends(require_api_key)])
def list_accounts():
    return {"active": accounts.active_id(), "accounts": accounts.public_list()}


@app.post("/accounts", dependencies=[Depends(require_api_key)])
def add_account(body: AccountIn):
    """Checks the token with one tiny request before saving it (a few seconds)."""
    try:
        added = accounts.add(body.label, body.token, body.email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"account": added, **list_accounts()}


@app.delete("/accounts/{account_id}", dependencies=[Depends(require_api_key)])
def remove_account(account_id: str):
    try:
        if not accounts.remove(account_id):
            raise HTTPException(status_code=404, detail="Account not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    worker.wake_agent_lane()   # its projects are on the default account now
    return _settings_view()


class AccountAliasIn(BaseModel):
    alias: str = ""


@app.put("/accounts/{account_id}/alias", dependencies=[Depends(require_api_key)])
def set_account_alias(account_id: str, body: AccountAliasIn):
    if not accounts.set_alias(account_id, body.alias):
        raise HTTPException(status_code=404, detail="Account not found")
    return _settings_view()


class AccountDetailsIn(BaseModel):
    name: Optional[str] = Field(None, max_length=60)
    alias: Optional[str] = Field(None, max_length=24)
    email: Optional[str] = Field(None, max_length=120)


class AccountColorIn(BaseModel):
    color: str = Field("", max_length=7)


@app.put("/accounts/{account_id}/color", dependencies=[Depends(require_api_key)])
def set_account_color(account_id: str, body: AccountColorIn):
    try:
        found = accounts.set_color(account_id, body.color)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail="Account not found")
    return _settings_view()


@app.put("/accounts/{account_id}/details", dependencies=[Depends(require_api_key)])
def set_account_details(account_id: str, body: AccountDetailsIn):
    try:
        found = accounts.set_details(account_id, body.name, body.alias, body.email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail="Account not found")
    return _settings_view()


@app.post("/accounts/mac/show", dependencies=[Depends(require_api_key)])
def show_mac_account():
    """Bring back this Mac's login after it was removed from the list."""
    accounts.show_mac()
    return _settings_view()


@app.post("/accounts/{account_id}/activate", dependencies=[Depends(require_api_key)])
def activate_account(account_id: str):
    """Switch the default account: new Claude Code runs of projects without their
    own account use it, the usage bars show its limits (refreshed in the
    background), and those projects' tasks waiting out the old account's usage
    limit may try again at once. A project with its own account is unaffected."""
    try:
        accounts.activate(account_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    resumed = db.resume_rate_limited_now(default_account_only=True)
    worker.wake_agent_lane()
    accounts.probe_in_background(account_id)
    return {**_settings_view(), "resumed": resumed}


@app.post("/accounts/{account_id}/refresh", dependencies=[Depends(require_api_key)])
def refresh_account(account_id: str):
    """Read this account's live usage now (one tiny Haiku request)."""
    if not accounts.exists(account_id):
        raise HTTPException(status_code=404, detail="Account not found")
    if account_id == accounts.MAC:
        accounts.mac_login(refresh=True)
    check = accounts.probe(account_id)
    if not check["ok"]:
        raise HTTPException(status_code=502, detail="Couldn't read its usage: " + (check["error"] or "unknown error"))
    return _settings_view()


@app.post("/notify/test", dependencies=[Depends(require_api_key)])
def send_test_push():
    if not worker.ACTIVITYSMITH_API_KEY and not db.list_push_subscriptions():
        raise HTTPException(status_code=409, detail="No push set up: ACTIVITYSMITH_API_KEY isn't set in the launchd plist "
                                                    "and no browser has allowed desktop alerts over https")
    return {"sent": worker.notify_phone("Test push from Octopus", "If you can read this, pushes work.")}


@app.get("/usage/tokens", dependencies=[Depends(require_api_key)])
def token_usage(start: Optional[str] = None, end: Optional[str] = None, project_id: Optional[int] = None,
                model_id: Optional[str] = None, lane: Optional[Literal["agent", "api", "assistant", "background"]] = None,
                account: Optional[str] = None):
    """The Token usage page: tokens and cost over a local date range
    (YYYY-MM-DD, both days included; default the last 30 days) - totals, per
    day, per project / model / lane / category, and every run behind them."""
    try:
        out = db.token_usage(start, end, project_id, model_id, lane, account or None, accounts.active_id())
        labels = {a: accounts.label(a) for a in out["options"]["accounts"]}
        out["options"]["accounts"] = [{"id": a, "label": labels[a]} for a in out["options"]["accounts"]]
        for g in out["by_account"]:
            g["label"] = accounts.label(g["key"])
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/usage/summary", dependencies=[Depends(require_api_key)])
def usage_summary():
    """Real API spend (today / month / last 7 days / by project) plus the
    last 5-hour and weekly Claude usage Claude Code reported."""
    return {**db.spend_summary(), "claude_usage": accounts.current(db.get_setting("claude_usage"))}


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
    r = db.get_recipe(recipe_id)
    if r:
        den_bin.snapshot("recipe", r, r["name"], "Recipes & schedules", r.get("prompt") or "")
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
        projects = [p for p in db.list_projects(active_only=True) if not db.is_general_project(p) and db.has_folder(p)]
    else:
        pid = body.project_id if body.project_id is not None else (
            int(recipe["target"]) if recipe["target"].isdigit() else None)
        if pid is None:
            raise HTTPException(status_code=400, detail="Pick a project to run this recipe in")
        project = db.get_project(pid)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        _require_folder(project)
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
    # Only the fields sent are changed. project_id: null means every active project.
    enabled: Optional[bool] = None
    prompt: Optional[str] = None
    label: Optional[str] = None
    project_id: Optional[int] = None
    cadence: Optional[str] = None
    time: Optional[str] = None
    weekday: Optional[int] = None
    date: Optional[str] = None  # once: "YYYY-MM-DD"
    model: Optional[str] = None
    priority: Optional[int] = None


def _check_schedule(cadence: str, time_: Optional[str], weekday: Optional[int], date_: Optional[str],
                    project_id: Optional[int], prompt: str) -> None:
    import re as _re
    from datetime import datetime as _dt
    if cadence not in db.CADENCES:
        raise HTTPException(status_code=400, detail=f"cadence must be one of {', '.join(db.CADENCES)}")
    if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_ or ""):
        raise HTTPException(status_code=400, detail="time must be HH:MM")
    if cadence == "weekly" and weekday not in range(7):
        raise HTTPException(status_code=400, detail="weekly schedules need a weekday (0 = Monday)")
    if date_ is not None:
        try:
            _dt.strptime(date_, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if project_id is not None and not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")


@app.get("/schedules", dependencies=[Depends(require_api_key)])
def list_schedules():
    return db.list_schedules()


@app.post("/schedules", dependencies=[Depends(require_api_key)])
def create_schedule(body: ScheduleIn):
    _check_schedule(body.cadence, body.time, body.weekday, body.date, body.project_id, body.prompt)
    label = (body.label or body.prompt).strip()
    return db.add_schedule(label[:80], body.prompt.strip(), body.project_id, body.cadence, body.time,
                           body.weekday, body.model, body.priority, on_date=body.date)


@app.patch("/schedules/{schedule_id}", dependencies=[Depends(require_api_key)])
def update_schedule(schedule_id: int, body: ScheduleUpdateIn):
    """Change any part of a schedule: what it runs, where, when, or whether it's on."""
    sc = db.get_schedule(schedule_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    sent = body.model_fields_set
    for k in ("enabled", "prompt", "cadence", "time", "model", "priority"):
        if k in sent and getattr(body, k) is None:
            raise HTTPException(status_code=400, detail=f"{k} can't be empty")
    changes: dict = {}
    if "prompt" in sent:
        changes["prompt"] = body.prompt.strip()
        if "label" not in sent and sc["label"] == sc["prompt"][:80]:
            changes["label"] = changes["prompt"][:80]     # a label that was just the prompt follows it
    if "label" in sent:
        changes["label"] = ((body.label or "").strip() or changes.get("prompt", sc["prompt"]))[:80]
    if "time" in sent:
        changes["time_of_day"] = body.time
    for k in ("project_id", "cadence", "weekday", "model", "priority", "enabled"):
        if k in sent:
            changes[k] = getattr(body, k)
    new = {**sc, **changes}
    _check_schedule(new["cadence"], new["time_of_day"], new["weekday"], body.date, new["project_id"], new["prompt"])
    return db.update_schedule(schedule_id, changes, on_date=body.date)


@app.delete("/schedules/{schedule_id}", dependencies=[Depends(require_api_key)])
def delete_schedule(schedule_id: int):
    sc = db.get_schedule(schedule_id)
    if sc:
        sc = {k: v for k, v in sc.items() if k not in ("project_name", "unread")}
        den_bin.snapshot("schedule", sc, sc.get("label") or "Schedule", "Recipes & schedules",
                         f"{sc.get('cadence')} · {sc.get('time_of_day')}\n{sc.get('prompt') or ''}")
    db.delete_schedule(schedule_id)
    return {"ok": True}


# ----------------------------------------------------------------- polish

POLISH_SYSTEM_PROMPT = (
    "You polish requests a developer sends to an AI assistant (Claude Code or a chat model). Rewrite the text so it is clear, "
    "specific and well-formed: fix grammar, spelling and punctuation, and tighten vague wording. Keep the same intent, language "
    "and tone. Keep file names, commands, code, numbers and names exactly as written. Do not add new requirements or explanations. "
    "Return only the rewritten text, with no quotes or preamble."
)
# Notes that aren't requests to an assistant (a calendar item's agenda...): tidy them, don't turn them into asks.
POLISH_NOTE_PROMPT = (
    "You polish short notes a person writes for themselves (an event's agenda, a description, a reminder). Rewrite the text so "
    "it is clear and well-formed: fix grammar, spelling and punctuation, and tidy the wording and any lists. Keep the same meaning, "
    "language and tone. Keep names, dates, times, numbers, links and code exactly as written. Do not add new details or turn it "
    "into a request. Return only the rewritten text, with no quotes or preamble."
)


# The Notes editor polishes a whole note as its Markdown copy, then rebuilds the formatting from it.
POLISH_RICH_NOTE_PROMPT = POLISH_NOTE_PROMPT + (
    " The text is Markdown from a notes editor: keep every line's marker exactly - headings (#), bullets (- ), numbered "
    "items (1. ), checkboxes (- [ ] and - [x]), quotes (> ), Important lines (!! ) and code fences - and keep every "
    "[text](address) and ![image](upload:...) exactly as written, in the same place."
)


class PolishIn(BaseModel):
    text: str
    kind: str = "prompt"  # prompt | note | richnote (a Notes page note, as Markdown) | summary (the Notes page's Summarize)


SUMMARY_NOTE_PROMPT = ("Summarize this note in one or two short sentences: what it is about and anything to act on. "
                       "Plain text, no preamble, no bullet points.")


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
        system = POLISH_NOTE_PROMPT if body.kind == "note" else POLISH_RICH_NOTE_PROMPT if body.kind == "richnote" else SUMMARY_NOTE_PROMPT if body.kind == "summary" else POLISH_SYSTEM_PROMPT
        out = providers.complete(model, [{"role": "user", "content": text[:12000 if body.kind == "summary" else 4000]}], system, 30)
    except providers.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    db.log_usage("polish", model["id"], out.input_tokens, out.output_tokens,
                 providers.estimate_cost(model, out.input_tokens, out.output_tokens))
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
