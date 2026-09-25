"""
HTTP endpoints for the personal assistant, mounted by api.py under
/assistant. Same X-API-Key protection as the rest of the API - except the
OAuth callback, which is a browser redirect from Google and is instead tied
to the sign-in this server started by its single-use `state`.

Nothing here returns a token: connection status carries the account name and
granted services, never credentials.
"""

import html
import json
import os
import time
from datetime import timedelta
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import db
from assistant import capabilities, connections, intent, oauth, planner, providers, scope, timeutil, tools
from assistant.tools import ai as ai_tool

CALENDAR_CACHE_SECONDS = 60
_calendar_cache: dict[str, Any] = {"at": 0.0, "data": None}


class ConnectIn(BaseModel):
    # Which services to ask Google for; omit for everything the provider offers.
    services: Optional[list[str]] = None


class RouteIn(BaseModel):
    text: str
    # True when the text continues an assistant chat ("move it to 4").
    in_thread: bool = False


def _approvals(prefix: str) -> list[dict[str, Any]]:
    out = []
    for a in db.list_pending_approvals():
        if not a["tool_name"].startswith(prefix):
            continue
        try:
            payload = json.loads(a["tool_input"])
        except ValueError:
            continue
        out.append({"id": a["id"], "task_id": a["task_id"], "created_at": a["created_at"],
                    "describe": payload.get("describe"), "preview": payload.get("preview"), "tool_name": a["tool_name"]})
    return out


def _planner_status() -> dict[str, Any]:
    try:
        m = planner.choose_model()
        return {"available": True, "model_id": m["id"], "label": m.get("label", m["id"]),
                "note": "Your Claude subscription, with every tool switched off" if m.get("provider") == "claude_code" else ""}
    except planner.PlannerUnavailable as e:
        return {"available": False, "reason": str(e)}


def _provider_view(service: str) -> Optional[dict[str, Any]]:
    """The connection that serves this service, for the Email / Calendar tabs."""
    for st in connections.list_status():
        if st["status"] in (connections.CONNECTED, connections.REAUTH) and service in st["services_granted"]:
            return st
    return None


def build_router(auth: Callable) -> APIRouter:
    router = APIRouter(prefix="/assistant")
    dep = [Depends(auth)]

    @router.get("/status", dependencies=dep)
    def status():
        return {"connections": connections.list_status(), "policies": capabilities.all_policies("assistant"),
                "planner": _planner_status(), "tools": {t.id: [a for a in t.actions] for t in tools.REGISTRY.values()},
                "tools_refused": {t.id: r for t in tools.REGISTRY.values() if (r := scope.tool_refusal(t.id))},
                "models": ai_tool.model_rows(),
                "push_configured": bool(os.environ.get("ACTIVITYSMITH_API_KEY")), "timezone": timeutil.tz_name()}

    # ------------------------------------------------------------ connections

    @router.post("/connections/{provider_id}/connect", dependencies=dep)
    def connect(provider_id: str, body: ConnectIn = ConnectIn()):
        if provider_id not in oauth.PROVIDERS:
            raise HTTPException(status_code=404, detail="Unknown provider")
        try:
            return connections.begin(provider_id, body.services)
        except oauth.OAuthError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @router.get("/oauth/{provider_id}/callback")
    def oauth_callback(provider_id: str, code: Optional[str] = None, state: Optional[str] = None,
                       error: Optional[str] = None):
        # No API key here: Google redirects the browser. The single-use state
        # ties this request to a sign-in that /connect started.
        label = oauth.PROVIDERS[provider_id].label if provider_id in oauth.PROVIDERS else "The provider"

        def page(title: str, body: str, ok: bool) -> HTMLResponse:
            colour = "#3fb950" if ok else "#f85149"
            return HTMLResponse(
                f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{html.escape(title)}</title><body style=\"font:16px -apple-system,system-ui,sans-serif;background:#0B1016;"
                f"color:#e6edf3;display:grid;place-items:center;min-height:100vh;margin:0\"><div style='max-width:420px;padding:24px'>"
                f"<h2 style='color:{colour}'>{html.escape(title)}</h2><p>{html.escape(body)}</p>"
                f"<p><a style='color:#58a6ff' href='/dashboard#/personal/connections'>Back to the dashboard</a></p></div>",
                status_code=200 if ok else 400)
        if error:
            return page("Not connected", f"{label} said: {error}", False)
        if not code or not state:
            return page("Not connected", "The sign-in came back without the details it needs.", False)
        try:
            connections.finish(state, code, expected_provider=provider_id)
        except oauth.OAuthError as e:
            return page("Not connected", str(e), False)
        except Exception:
            return page("Not connected", "Something went wrong saving the connection. Check the service log.", False)
        return RedirectResponse("/dashboard#/personal/connections", status_code=303)

    @router.delete("/connections/{provider_id}", dependencies=dep)
    def disconnect(provider_id: str):
        if provider_id not in oauth.PROVIDERS:
            raise HTTPException(status_code=404, detail="Unknown provider")
        _calendar_cache["data"] = None
        return connections.disconnect(provider_id)

    # ------------------------------------------------------------ permissions

    @router.get("/policies", dependencies=dep)
    def policies():
        return capabilities.all_policies()

    @router.patch("/policies", dependencies=dep)
    def set_policies(body: dict[str, str]):
        try:
            for cap_name, policy in body.items():
                capabilities.set_policy(cap_name, policy)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return capabilities.all_policies()

    # ---------------------------------------------------------------- routing

    @router.post("/route", dependencies=dep)
    def route_dry_run(body: RouteIn):
        """What Octopus would do with this text - intent, tool, permissions,
        approval, model or not - without doing any of it."""
        return intent.classify(body.text, in_thread=body.in_thread).to_dict()

    # --------------------------------------------------------------- activity

    @router.get("/activity", dependencies=dep)
    def activity(limit: int = 100, tool: Optional[str] = None):
        return db.list_activity(limit, tool)

    # ------------------------------------------------------- dashboard views

    @router.get("/email/overview", dependencies=dep)
    def email_overview():
        drafts = [{"id": r["id"], "created_at": r["created_at"], "target": r["target"], "ref": r["ref"]}
                  for r in db.list_activity(50, "email", "create_draft") if r["result"] == "ok"][:10]
        return {"connection": _provider_view("email"), "pending": _approvals("assistant:email."), "drafts": drafts,
                "activity": db.list_activity(15, "email")}

    @router.get("/calendar/overview", dependencies=dep)
    def calendar_overview():
        out: dict[str, Any] = {"connection": _provider_view("calendar"), "pending": _approvals("assistant:calendar."),
                               "today": [], "upcoming": [], "error": None, "activity": db.list_activity(15, "calendar")}
        if not out["connection"]:
            return out
        if _calendar_cache["data"] and time.time() - _calendar_cache["at"] < CALENDAR_CACHE_SECONDS:
            return {**out, **_calendar_cache["data"]}
        try:
            provider = providers.calendar()
            now = timeutil.now()
            midnight = timeutil.at(now.date(), tz=now.tzinfo)
            events = provider.list_events(midnight, midnight + timedelta(days=8), max_results=60)
            me = provider.account()
            today_end = midnight + timedelta(days=1)
            brief = [{"id": e["id"], "title": e["title"], "start": e["start"], "end": e["end"], "all_day": e["all_day"],
                      "location": e.get("location"), "link": e.get("link"),
                      "attendees": [a["name"] or a["email"] for a in e["attendees"] if not a["self"] and a["email"] != me]}
                     for e in events]
            data = {"today": [e for e in brief if timeutil.parse_iso(e["start"]) < today_end],
                    "upcoming": [e for e in brief if timeutil.parse_iso(e["start"]) >= today_end]}
            _calendar_cache.update(at=time.time(), data=data)
            out.update(data)
        except Exception as e:  # the tab should still show approvals and activity
            out["error"] = str(e)[:300]
        return out

    @router.get("/reminders", dependencies=dep)
    def reminders():
        return db.list_reminders(["pending", "fired"], 60)

    @router.delete("/reminders/{reminder_id}", dependencies=dep)
    def cancel_reminder(reminder_id: int):
        if not db.cancel_reminder(reminder_id):
            raise HTTPException(status_code=404, detail="No pending reminder with that id")
        return {"ok": True}

    return router
