"""
HTTP endpoints for Alexa, mounted by api.py.

    POST /alexa                      the skill's endpoint. NO X-API-Key (Amazon can't send one): every request
                                     is checked by alexa.verify instead. This is the only path Tailscale Funnel
                                     should expose to the internet.
    GET  /alexa/status               set up? paired? notifications allowed? last request / notification
    PATCH /alexa/prefs               {"notify_approvals"?, "notify_results"?, "approval_pin"?}
    POST /alexa/test-notification    send one notification now
    DELETE /alexa/link               forget the paired Amazon account (the next one to use the skill is paired)
    POST /presence                   the dashboard's "I'm here" heartbeat (see presence.py)
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import presence
from alexa import notifier, skill, state, verify

log = logging.getLogger("the-den.alexa")

MAX_BODY_BYTES = 128 * 1024


class PrefsIn(BaseModel):
    notify_approvals: Optional[bool] = None
    notify_results: Optional[bool] = None
    approval_pin: Optional[str] = None


class PresenceIn(BaseModel):
    device: Optional[str] = None


def status() -> dict[str, Any]:
    st, p = state.get(), state.prefs()
    cid, secret = state.client_credentials()
    ok, why = notifier.configured()
    return {
        "skill_id_set": bool(state.skill_id()),
        "notifications_configured": ok, "notifications_reason": why,
        "client_credentials_set": bool(cid and secret),
        "paired": bool(st.get("user_id")), "paired_at": st.get("linked_at"),
        "user_pinned": bool(os.environ.get("ALEXA_USER_ID")),
        "subscribed": st.get("subscribed"),
        "locale": st.get("locale"), "last_request_at": st.get("last_request_at"),
        "last_notify": st.get("last_notify"), "stage": notifier.stage(),
        "public_url": (os.environ.get("ALEXA_PUBLIC_URL") or "").rstrip("/") or None,
        "prefs": {"notify_approvals": p["notify_approvals"], "notify_results": p["notify_results"],
                  "approval_pin_set": bool(p.get("approval_pin"))},
        "presence": presence.status(),
        "waiting": {"approvals": len(state.pending_approvals()), "finished": len(state.finished_watched())},
    }


def build_routers(require_api_key: Callable) -> tuple[APIRouter, APIRouter]:
    public = APIRouter()
    private = APIRouter(dependencies=[Depends(require_api_key)])

    @public.post("/alexa")
    async def alexa_endpoint(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=400, detail="request too large")
        skill_id = state.skill_id()
        if not skill_id:
            raise HTTPException(status_code=503, detail="Alexa isn't set up here: ALEXA_SKILL_ID is not set")
        try:
            envelope = json.loads(body)
            if not isinstance(envelope, dict):
                raise ValueError("not an object")
            await asyncio.to_thread(verify.check_signature, body, request.headers.get("SignatureCertChainUrl", ""),
                                    request.headers.get("Signature-256", ""))
            verify.check_timestamp(envelope)
            verify.check_application(envelope, skill_id)
        except (ValueError, verify.VerifyError) as e:
            log.warning("Refused an /alexa request: %s", e)
            raise HTTPException(status_code=400, detail="request could not be verified")
        try:
            return JSONResponse(await asyncio.to_thread(skill.handle, envelope))
        except Exception:
            log.exception("Alexa request failed")
            return JSONResponse({"version": "1.0", "response": {
                "outputSpeech": {"type": "PlainText", "text": "Sorry, something went wrong in The Den. The details are in its log."},
                "shouldEndSession": True}})

    @private.get("/alexa/status")
    def alexa_status():
        return status()

    @private.patch("/alexa/prefs")
    def alexa_prefs(body: PrefsIn):
        changes = body.model_dump(exclude_unset=True)
        pin = changes.get("approval_pin")
        if pin is not None:
            pin = re.sub(r"\s", "", pin)
            if pin and not re.fullmatch(r"\d{4,8}", pin):
                raise HTTPException(status_code=400, detail="The approval code must be 4 to 8 digits (or empty to turn it off)")
            changes["approval_pin"] = pin
        state.set_prefs(changes)
        return status()

    @private.post("/alexa/test-notification")
    def alexa_test():
        try:
            notifier.send(1)
        except notifier.NotifyError as e:
            state.update(lambda s: s.update(last_notify={"at": state.now_iso(), "ok": False, "error": str(e), "test": True}))
            raise HTTPException(status_code=409, detail=str(e))
        state.update(lambda s: s.update(last_notify={"at": state.now_iso(), "ok": True, "count": 1, "test": True}))
        return {"sent": True}

    @private.delete("/alexa/link")
    def alexa_unlink():
        state.update(lambda s: [s.pop(k, None) for k in ("user_id", "linked_at", "subscribed", "consent_card_sent", "conversation")])
        return status()

    @private.post("/presence")
    def seen(body: Optional[PresenceIn] = None):
        presence.seen((body.device if body else None) or "")
        return {"ok": True}

    return public, private
