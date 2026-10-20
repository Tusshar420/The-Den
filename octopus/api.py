"""
HTTP endpoints for Octopus, mounted by api.py. Same X-API-Key protection as
the rest of the API.

    POST /octopus/plan                dry run: what would Octopus do with this request?
    POST /octopus/plans/{id}/run      [Run] on a plan card - optionally changing project / model / agent first
    POST /octopus/plans/{id}/cancel   [Cancel]
    GET  /routing, /routing/{id}      every request Octopus has routed, with what it decided and what came of it
    GET  /octopus/overview            everything the Den Home shows, in one call
    GET  /octopus/agents, /tools      the registries (agents.json, tools.json)
    GET  /octopus/providers           models grouped by provider, with availability, limits and usage
    PATCH /octopus/models/{id}        switch a model on or off
    POST /octopus/models              add a model (octopus/models.py); PUT / DELETE /octopus/models/{id} change or remove one added here
    POST /octopus/api-keys            another OpenAI / Gemini key (api_keys.py); DELETE /octopus/api-keys/{id},
                                      POST /octopus/api-keys/{id}/default - each answers with the providers view
    GET  /tasks/{id}/detail, /diff    the task page: plan, steps, files, git, tests, approvals, activity
    GET  /octopus/images/{name}       a picture the Creative Agent made
    (voice)                           see octopus/voice
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import accounts
import api_keys
import db
import providers
import router
import uploads
from assistant import capabilities as cap
from octopus import execution, models as model_admin, plan as planner, registry, service, sites, voice

PROVIDER_LABELS = {
    "claude_code": ("Claude Code", "Runs on your Claude subscription, inside a project folder"),
    "openai": ("OpenAI", "API keys - OPENAI_API_KEY and any added here"),
    "gemini": ("Google Gemini", "API keys - GEMINI_API_KEY and any added here"),
    "tools": ("Personal assistant", "Email, calendar and reminders through your connected accounts"),
    "control": ("Octopus", "Instant commands about Octopus itself"),
}


class PlanIn(BaseModel):
    text: str
    project_id: Optional[int] = None
    model: str = "auto"
    agent: Optional[str] = None
    images: Optional[list[str]] = None      # upload ids, see uploads.py


class RunPlanIn(BaseModel):
    # Anything given here changes the plan first (the "Advanced" overrides).
    project_id: Optional[int] = None
    model: Optional[str] = None
    agent: Optional[str] = None
    priority: int = 0
    permission_mode: Optional[str] = None


class ModelPatchIn(BaseModel):
    # The model panel on the AI Providers page; only what is sent changes. Works for
    # models.json models too (kept in settings["model_overrides"], see router / providers).
    enabled: Optional[bool] = None
    label: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_addon: Optional[str] = None
    use_for: Optional[list[str]] = None
    price_per_mtok: Optional[dict[str, float]] = None   # {input, output} USD per million tokens
    context_tokens: Optional[int] = None
    caps: Optional[list[str]] = None                     # shown on the page only


class SiteIn(BaseModel):
    url: str


class PriceIn(BaseModel):
    input: Optional[float] = None
    output: Optional[float] = None


class ModelIn(BaseModel):
    # The Add a model form; octopus/models.py checks the values.
    label: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    modality: Optional[str] = None
    use_for: Optional[list[str]] = None
    coding: Optional[int] = None
    reasoning: Optional[int] = None
    cost: Optional[int] = None
    speed: Optional[int] = None
    vision: Optional[bool] = None
    web_search: Optional[bool] = None
    context_tokens: Optional[int] = None
    price_per_mtok: Optional[PriceIn] = None
    price_per_image: Optional[float] = None
    note: Optional[str] = None
    enabled: Optional[bool] = None
    base_url: Optional[str] = None      # OpenAI-compatible servers (Mistral, Ollama, ...)
    api_key: Optional[str] = None       # their key - kept in api_keys.json, never in the model


class ApiKeyIn(BaseModel):
    provider: str
    label: str
    key: str


class SpeakIn(BaseModel):
    text: str
    voice: Optional[str] = None
    provider: Optional[str] = None
    # "device": return audio for the caller to play. "mac": speak on the Mac's own speakers.
    on: str = "device"


MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _raise(e: service.ServiceError) -> None:
    raise HTTPException(status_code=e.status, detail=e.detail)


def _agent_label(agent_id: Optional[str]) -> Optional[str]:
    a = registry.get_agent(agent_id)
    return a.get("label", agent_id) if a else agent_id


def _model_label(model_id: Optional[str]) -> Optional[str]:
    m = router.get_model(model_id)
    return m.get("label", model_id) if m else model_id


def _ai_tool(task: dict[str, Any]) -> dict[str, Any]:
    """Which AI tool ran (or will run) a task, and for Claude Code which Claude
    account it is on: the one it started on, else its project's."""
    m = router.get_model(task.get("model_id")) or {}
    provider = m.get("provider")
    out: dict[str, Any] = {"provider": provider, "label": m.get("label") or task.get("model_id"), "account": None}
    if provider == "claude_code" or task.get("lane") == "agent":
        acct = task.get("claude_account") or accounts.for_project(task["project_id"])
        out["account"] = {"id": acct, "label": accounts.label(acct)}
    return out


def _progress(task: dict[str, Any]) -> Optional[dict[str, int]]:
    steps = task.get("plan_steps") or []
    if not steps:
        return None
    done = sum(1 for s in steps if s.get("status") == "completed")
    return {"done": done, "total": len(steps), "percent": round(100 * done / len(steps))}


def _running_view(task: dict[str, Any]) -> dict[str, Any]:
    project = db.get_project(task["project_id"]) or {}
    return {"id": task["id"], "thread_id": task.get("thread_id") or task["id"], "title": task.get("title") or task["prompt"][:80],
            "status": task["status"], "lane": task.get("lane"), "category": task.get("category"),
            "project": {"id": project.get("id"), "name": project.get("name"), "is_general": db.is_general_project(project)} if project else None,
            "agent": {"id": task.get("agent_id"), "label": _agent_label(task.get("agent_id"))} if task.get("agent_id") else None,
            "model": {"id": task.get("model_id"), "label": _model_label(task.get("model_id"))},
            "current_step": task.get("current_step"), "started_at": task.get("started_at"), "progress": _progress(task)}


def _tool_view(t: dict[str, Any]) -> dict[str, Any]:
    granted = [c for c in t.get("capabilities", []) if c in cap.CAPABILITIES]
    return {**{k: t.get(k) for k in ("id", "label", "kind", "description", "enabled", "connection", "mcp")},
            "capabilities": [{"capability": c, "label": cap.CAPABILITIES[c]["label"], "risk": cap.CAPABILITIES[c]["risk"],
                              "policy": cap.effective_policy_for(c)} for c in granted],
            "agents": [a["id"] for a in registry.agents() if t["id"] in a.get("tools", [])],
            "when": t.get("when") or {}}


def _agent_view(a: dict[str, Any]) -> dict[str, Any]:
    cats = router.registry().get("routing", {}).get("categories", {})
    models = [router.get_model(i) for i in (a.get("preferred_models") or [])]
    return {**{k: a.get(k) for k in ("id", "label", "description", "categories", "instructions", "context", "execution")},
            "preferred_models": [{"id": m["id"], "label": m.get("label", m["id"])} for m in models if m],
            "tools": [{"id": t, "label": (registry.get_tool(t) or {}).get("label", t)} for t in a.get("tools", [])],
            "permissions": [{"capability": c, "label": cap.CAPABILITIES[c]["label"], "risk": cap.CAPABILITIES[c]["risk"],
                             "policy": cap.effective_policy_for(c)} for c in a.get("permissions", []) if c in cap.CAPABILITIES],
            "category_models": {c: (cats.get(c) or {}).get("candidates", []) for c in a.get("categories", [])}}


def providers_view() -> dict[str, Any]:
    usage = {r["key"]: r for r in db.usage_summary(30)["by_model"]}
    groups: dict[str, dict[str, Any]] = {}
    for m in router.models_status():
        g = groups.setdefault(m["provider"], {"id": m["provider"], "label": PROVIDER_LABELS.get(m["provider"], (m["provider"], ""))[0],
                                             "note": PROVIDER_LABELS.get(m["provider"], ("", ""))[1], "models": []})
        u = usage.get(m["id"]) or {}
        g["models"].append({**m, "usage": {"tasks": u.get("tasks", 0), "done": u.get("done") or 0, "failed": u.get("failed") or 0,
                                           "avg_duration_ms": u.get("avg_duration_ms"), "cost_usd": u.get("cost_usd")}})
    for gid, g in groups.items():
        ready, why = providers.provider_ready(gid)
        g["ready"], g["not_ready_reason"] = ready, why
        g["available_models"] = sum(1 for m in g["models"] if m["available"])
        if gid in api_keys.PROVIDERS:
            g["keys"] = api_keys.public_list(gid)
    order = list(PROVIDER_LABELS)
    return {"providers": sorted(groups.values(), key=lambda g: order.index(g["id"]) if g["id"] in order else 99),
            "claude_usage": accounts.current(db.get_setting("claude_usage")), "claude_window": planner.claude_window(),
            "categories": {c: {"candidates": v.get("candidates", []), "optimize": v.get("optimize", "balanced"),
                               "transferable": v.get("transferable", True), "description": v.get("description", "")}
                           for c, v in router.registry().get("routing", {}).get("categories", {}).items()},
            "add": model_admin.choices()}


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    # ----------------------------------------------------------------- plans
    @r.post("/octopus/plan")
    def dry_run(body: PlanIn):
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="request is empty")
        try:
            return planner.build_plan(text, project_id=body.project_id, force_model=body.model, force_agent=body.agent, source="dashboard",
                                      images=uploads.validate(body.images))
        except ValueError as e:
            raise HTTPException(status_code=404 if "not found" in str(e).lower() else 400, detail=str(e))

    @r.post("/octopus/plans/{routing_id}/run")
    def run_plan(routing_id: int, body: Optional[RunPlanIn] = None):
        body = body or RunPlanIn()
        try:
            return service.run_plan(routing_id, project_id=body.project_id, model=body.model, agent=body.agent,
                                    priority=body.priority, permission_mode=body.permission_mode)
        except service.ServiceError as e:
            _raise(e)

    @r.post("/octopus/plans/{routing_id}/cancel")
    def cancel_plan(routing_id: int):
        try:
            return service.cancel_plan(routing_id)
        except service.ServiceError as e:
            _raise(e)

    # --------------------------------------------------------------- routing
    @r.get("/routing")
    def routing_list(limit: int = 100, status: Optional[str] = None, category: Optional[str] = None):
        rows = db.list_routing(max(1, min(limit, 500)), status or None, category or None)
        return [{**row, "agent_label": _agent_label(row.get("agent_id")), "model_label": _model_label(row.get("model_id"))} for row in rows]

    @r.get("/routing/{routing_id}")
    def routing_get(routing_id: int):
        row = db.get_routing(routing_id)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        task = db.get_task(row["task_id"]) if row.get("task_id") else None
        return {**row, "agent_label": _agent_label(row.get("agent_id")), "model_label": _model_label(row.get("model_id")),
                "task": task}

    # ------------------------------------------------------------- overview
    @r.get("/octopus/overview")
    def overview():
        q = db.queue_status()
        running = [_running_view(t) for t in q.get("running_all") or []]
        waiting = db.list_tasks(statuses=["queued"])
        approvals = db.list_pending_approvals()
        plans = [row for row in db.list_routing(20) if row["status"] in ("proposed", "needs_input")]
        pv = providers_view()
        return {
            "autonomy": cap.autonomy(), "plan_review": db.get_setting("plan_review"),
            "current": running,
            "queue": {"waiting": len(waiting), "next": [{"id": t["id"], "title": t.get("title") or t["prompt"][:80],
                                                         "model": _model_label(t.get("model_id"))} for t in waiting[:5]],
                      "rate_limited": (q.get("earliest_rate_limit_resume") or {}).get("resume_at")},
            "approvals": {"count": len(approvals), "items": [{"id": a["id"], "task_id": a["task_id"], "tool_name": a["tool_name"],
                                                              "created_at": a["created_at"]} for a in approvals[:5]]},
            "plans_waiting": [{"routing_id": p["id"], "request": p["request"], "status": p["status"], "plan": p["plan"]} for p in plans],
            "activity": db.list_recent_events(15),
            "providers": [{"id": g["id"], "label": g["label"], "ready": g["ready"], "available_models": g["available_models"],
                           "models": len(g["models"])} for g in pv["providers"]],
            "claude_window": pv["claude_window"],
        }

    # ------------------------------------------------------------ registries
    @r.get("/octopus/agents")
    def agents_list():
        return [_agent_view(a) for a in registry.agents()]

    @r.get("/octopus/tools")
    def tools_list():
        return [_tool_view(t) for t in registry.tools()]

    @r.get("/octopus/providers")
    def providers_list():
        return providers_view()

    # ------------------------------------------------- OpenAI / Gemini keys
    @r.post("/octopus/api-keys")
    def add_api_key(body: ApiKeyIn):
        try:
            api_keys.add(body.provider, body.label, body.key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return providers_view()

    @r.delete("/octopus/api-keys/{key_id}")
    def remove_api_key(key_id: str):
        try:
            found = api_keys.remove(key_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if not found:
            raise HTTPException(status_code=404, detail="No such key")
        return providers_view()

    @r.post("/octopus/api-keys/{key_id}/default")
    def default_api_key(key_id: str):
        try:
            api_keys.make_default(key_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return providers_view()

    # ----------------------------------------------------------- learned sites
    @r.get("/octopus/sites")
    def sites_list():
        return sites.list_sites()

    @r.post("/octopus/sites/learn")
    def sites_learn(body: SiteIn):
        try:
            url = sites.normalize_url(body.url)
        except sites.SiteError as e:
            raise HTTPException(status_code=400, detail=str(e))
        host = urlsplit(url).hostname
        task = db.add_task(planner.general_project()["id"], sites.learn_prompt(url), title=f"Learn site: {host}",
                           source="dashboard", route_reason="You asked Octopus to learn a website", routed_by="sites")
        return {"task": task, "url": url}

    @r.patch("/octopus/models/{model_id}")
    def patch_model(model_id: str, body: ModelPatchIn):
        if not router.get_model(model_id):
            raise HTTPException(status_code=404, detail="Unknown model")
        over = db.get_setting("model_overrides", {}) or {}
        sent = body.model_dump(exclude_unset=True)
        if "temperature" in sent and sent["temperature"] is not None and not 0 <= sent["temperature"] <= 2:
            raise HTTPException(status_code=400, detail="temperature must be between 0 and 2")
        if "max_tokens" in sent and sent["max_tokens"] is not None and not 1 <= sent["max_tokens"] <= 200000:
            raise HTTPException(status_code=400, detail="max_tokens must be between 1 and 200000")
        cats = router.registry().get("routing", {}).get("categories", {})
        if sent.get("use_for") is not None and any(c not in cats for c in sent["use_for"]):
            raise HTTPException(status_code=400, detail="use_for: unknown category")
        cur = {**over.get(model_id, {})}
        for k, v in sent.items():
            if v is None or v == "":
                cur.pop(k, None)          # null / "" clears it back to the default
            else:
                cur[k] = v.strip()[:2000] if isinstance(v, str) else v
        over[model_id] = cur
        db.set_setting("model_overrides", over)
        for g in providers_view()["providers"]:
            for m in g["models"]:
                if m["id"] == model_id:
                    return m

    def _model_row(model_id: str) -> dict[str, Any]:
        return next(m for g in providers_view()["providers"] for m in g["models"] if m["id"] == model_id)

    @r.post("/octopus/models")
    def add_model(body: ModelIn):
        try:
            fields = body.model_dump(exclude_unset=True)
            key = (fields.pop("api_key", None) or "").strip()
            m = model_admin.add(fields)
            if key and m.get("provider") == "openai_compat":
                api_keys.set_compat_key(m["id"], key)
        except model_admin.ModelError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _model_row(m["id"])

    @r.put("/octopus/models/{model_id}")
    def edit_model(model_id: str, body: ModelIn):
        if not model_admin.is_custom(model_id):
            raise HTTPException(status_code=409 if router.get_model(model_id) else 404,
                                detail="That model comes from models.json - change it there" if router.get_model(model_id) else "Unknown model")
        try:
            model_admin.update(model_id, body.model_dump(exclude_unset=True))
        except model_admin.ModelError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _model_row(model_id)

    @r.delete("/octopus/models/{model_id}")
    def delete_model(model_id: str):
        if not model_admin.is_custom(model_id):
            raise HTTPException(status_code=409 if router.get_model(model_id) else 404,
                                detail="That model comes from models.json - switch it off instead, or remove it there" if router.get_model(model_id) else "Unknown model")
        model_admin.remove(model_id)
        return {"ok": True}

    # ------------------------------------------------------------ task detail
    @r.get("/tasks/{task_id}/detail")
    def task_detail(task_id: int):
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        project = db.get_project(task["project_id"]) or {}
        routing = db.get_routing(task["routing_id"]) if task.get("routing_id") else None
        art = task.get("artifacts") or {}
        events = db.list_task_events([task_id]).get(task_id, [])
        approvals = []
        for a in db.list_task_approvals(task_id):
            try:
                payload = json.loads(a["tool_input"] or "{}")
            except ValueError:
                payload = {}
            approvals.append({"id": a["id"], "tool_name": a["tool_name"], "status": a["status"], "created_at": a["created_at"],
                              "describe": payload.get("describe") or payload.get("command") or a["tool_name"],
                              "decided_at": a.get("decided_at")})
        tool_ids = task.get("tools") or []
        return {
            "task": task,
            "project": {"id": project.get("id"), "name": project.get("name"), "path": project.get("path"),
                        "is_general": db.is_general_project(project)} if project else None,
            "agent": {"id": task.get("agent_id"), "label": _agent_label(task.get("agent_id"))} if task.get("agent_id") else None,
            "model": {"id": task.get("model_id"), "label": _model_label(task.get("model_id"))},
            "ai_tool": _ai_tool(task),
            "tools": [{"id": t, "label": (registry.get_tool(t) or {}).get("label", t)} for t in tool_ids],
            "routing": routing,
            "plan_steps": task.get("plan_steps") or [],
            "progress": _progress(task),
            "events": events,
            "files": art.get("files") or [],
            "git": art.get("git"),
            "tests": art.get("tests") or [],
            "validation": art.get("validation"),
            "approvals": approvals,
            "result": task.get("result_summary") if task["status"] == "done" else task.get("error_message"),
        }

    @r.get("/tasks/{task_id}/diff")
    def task_diff(task_id: int):
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        project = db.get_project(task["project_id"])
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if not db.has_folder(project):   # "git -C ''" would look at the service's own folder
            return {"available": False, "diff": "", "note": "This project has no folder."}
        return execution.full_diff(task, project["path"])

    @r.get("/tasks/{task_id}/file")
    def task_file(task_id: int, path: str):
        """A file the task created or changed (a PDF it wrote, say), for the preview sheet. Only paths the run
        itself recorded, and only if they really sit inside the project folder."""
        task = db.get_task(task_id)
        project = db.get_project(task["project_id"]) if task else None
        if not task or not project or not db.has_folder(project):
            raise HTTPException(status_code=404, detail="Task not found")
        recorded = {f.get("path") for f in (task.get("artifacts") or {}).get("files") or [] if f.get("status") != "D"}
        if path not in recorded:
            raise HTTPException(status_code=404, detail="That file isn't one this task changed")
        root = Path(project["path"]).resolve()
        file = (root / path).resolve()
        if root not in file.parents or not file.is_file():
            raise HTTPException(status_code=404, detail="That file is gone")
        # Always sent as a download; the page shows it from a local copy, never as a page of its own.
        return FileResponse(file, media_type="application/octet-stream", filename=file.name,
                            headers={"X-Content-Type-Options": "nosniff"})

    # ----------------------------------------------------------------- voice
    # Voice is another way in and out of the same pipeline - see octopus/voice.
    async def _audio(request: Request) -> bytes:
        audio = await request.body()
        if not audio:
            raise HTTPException(status_code=400, detail="send the recording as the request body")
        if len(audio) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="recording is too large")
        return audio

    @r.get("/voice")
    def voice_status():
        return voice.status()

    @r.post("/voice/transcribe")
    async def voice_transcribe(request: Request, provider: Optional[str] = None):
        """Speech to text with a server-side provider (the browser one transcribes in the browser)."""
        audio = await _audio(request)
        try:
            text = await asyncio.to_thread(voice.transcribe, audio, request.headers.get("content-type", ""), provider)
        except voice.VoiceError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"text": text}

    @r.post("/voice/speak")
    async def voice_speak(body: SpeakIn):
        """Text to speech. on="device" returns audio to play; on="mac" speaks on the Mac's speakers."""
        try:
            if body.on == "mac":
                await asyncio.to_thread(voice.speak_on_mac, body.text, body.voice)
                return {"ok": True}
            audio, mime = await asyncio.to_thread(voice.synthesize, body.text, body.voice, body.provider)
        except voice.VoiceError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return Response(content=audio, media_type=mime)

    @r.post("/voice/command")
    async def voice_command(request: Request):
        """A spoken request end to end: a JSON body {"text"} (already transcribed by the browser) or a raw
        recording is turned into text, then goes through exactly the pipeline typed requests use. The reply
        carries `spoken` - what to say back."""
        if "json" in request.headers.get("content-type", ""):
            data = await request.json()
            text, extra = str(data.get("text") or ""), {k: data[k] for k in ("project_id", "priority") if k in data}
        else:
            try:
                text = await asyncio.to_thread(voice.transcribe, await _audio(request), request.headers.get("content-type", ""), None)
            except voice.VoiceError as e:
                raise HTTPException(status_code=409, detail=str(e))
            extra = {}
        # Your own spoken commands (Connections › Smart devices) run straight away; the rest goes to Octopus.
        import den_connections
        said, yes = den_connections.confirm_prefix(text)
        own = await asyncio.to_thread(den_connections.run_command, said, yes)
        if own:
            return {**own, "transcript": text}
        try:
            outcome = await asyncio.to_thread(lambda: service.submit(text, source="voice", continue_recent=True, **extra))
        except service.ServiceError as e:
            _raise(e)
        return {**outcome, "transcript": text}

    # ---------------------------------------------------------------- images
    @r.get("/octopus/images/{name}")
    def image(name: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise HTTPException(status_code=400, detail="bad name")
        path = Path(providers.IMAGES_DIR) / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    return r
