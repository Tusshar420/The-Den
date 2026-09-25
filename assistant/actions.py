"""
The gate. Every tool call - from the deterministic path, from a planning
model, from anywhere - goes through invoke(), and nothing else may run a
tool. Here is where the capability policy is applied:

    prepare  (read-only: resolve arguments, decide capabilities)
    policy   ALLOW -> run now      ASK -> park as an approval      DENY -> refuse
    audit    every outcome is written to the activity log

An approval stores the exact resolved arguments; deciding it runs those and
nothing else, once. The model that proposed an action never sees the
approval mechanism and cannot skip it.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import db
from assistant import audit, capabilities, connections, net, oauth, providers, scope, timeutil, tools
from assistant.tools import Prepared, ToolContext, ToolError

tools.load_all()
log = logging.getLogger("the-den.assistant")

PREFIX = "assistant:"
APPROVAL_TTL_HOURS = 24

_EXPECTED = (ToolError, providers.ProviderError, connections.NotConnected, oauth.OAuthError, ValueError, net.ApiError)


@dataclass
class Outcome:
    status: str  # ok | awaiting_approval | denied | error
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    approval_id: Optional[int] = None
    describe: str = ""
    untrusted: bool = False  # the text/data include words written by other people


def make_context(task: Optional[dict[str, Any]], memory: Optional[dict[str, Any]] = None) -> ToolContext:
    return ToolContext(email=providers.email, calendar=providers.calendar, now=timeutil.now,
                       task=task, memory=memory if memory is not None else {})


def is_assistant_approval(approval: dict[str, Any]) -> bool:
    return str(approval.get("tool_name", "")).startswith(PREFIX)


def _message(e: Exception) -> str:
    return net.scrub(str(e))[:400] or e.__class__.__name__


def _trust_state(caps: list[str], policy: str) -> str:
    """'trusted' when the owner switched a capability that asks by default to
    allow - the log should show that an action ran without a human OK."""
    if policy == capabilities.ALLOW and any(capabilities.CAPABILITIES[c]["default"] != capabilities.ALLOW for c in caps):
        return "trusted"
    return "not_required"


# ------------------------------------------------------------------- invoke

def invoke(tool_id: str, action: str, args: dict[str, Any], ctx: ToolContext, tainted: bool = False,
           notify: Optional[Callable[[str, str], None]] = None) -> Outcome:
    task_id = (ctx.task or {}).get("id")
    try:
        tool = tools.get(tool_id)
        spec = tool.actions.get(action)
        if not spec:
            raise ToolError(f"{tool.label} has no action '{action}'. Available: {', '.join(tool.actions)}")
        # Before prepare(): a tool that has been taken away doesn't get to look anything up either.
        refused = scope.tool_refusal(tool_id)
        if refused:
            audit.log(tool_id, action, None, approval="denied", result="denied", error=refused, task_id=task_id)
            return Outcome("denied", refused)
        prepared = tool.prepare(action, args, ctx)
    except ToolError as e:
        return Outcome("error", _message(e))
    except _EXPECTED as e:
        audit.log(tool_id, action, None, result="error", error=_message(e), task_id=task_id)
        return Outcome("error", _message(e))

    caps = prepared.capabilities
    refused = scope.capability_refusal(caps)
    if refused:
        audit.log(tool_id, action, prepared.target, approval="denied", result="denied", error=refused, task_id=task_id)
        return Outcome("denied", refused, describe=prepared.describe)
    policy = capabilities.effective(caps, tainted)
    if policy == capabilities.DENY:
        blocked = ", ".join(c for c in caps if capabilities.policy_for(c) == capabilities.DENY)
        audit.log(tool_id, action, prepared.target, approval="denied", result="denied",
                  error=f"Blocked by your policy for {blocked}", task_id=task_id)
        return Outcome("denied", f"That's blocked by your permission settings ({blocked}). "
                                 "You can change it under Personal > Connections.", describe=prepared.describe)
    if policy == capabilities.ASK:
        return _request_approval(tool_id, action, prepared, ctx, notify)
    return _run(tool, tool_id, action, prepared, ctx, _trust_state(caps, policy), None)


def _run(tool: tools.Tool, tool_id: str, action: str, prepared: Prepared, ctx: ToolContext, approval: str,
         approval_id: Optional[int]) -> Outcome:
    task_id = (ctx.task or {}).get("id")
    spec = tool.actions[action]
    try:
        result = tool.execute(action, prepared.args, ctx)
    except _EXPECTED as e:
        audit.log(tool_id, action, prepared.target, approval=approval, result="error", error=_message(e),
                  task_id=task_id, approval_id=approval_id)
        return Outcome("error", _message(e), describe=prepared.describe)
    except Exception as e:  # a bug in a tool must not take the worker loop down
        log.exception("assistant tool %s.%s crashed", tool_id, action)
        audit.log(tool_id, action, prepared.target, approval=approval, result="error", error=_message(e),
                  task_id=task_id, approval_id=approval_id)
        return Outcome("error", "Something went wrong while doing that. It has been logged.", describe=prepared.describe)
    audit.log(tool_id, action, prepared.target, approval=approval, result="ok", task_id=task_id,
              approval_id=approval_id, ref=result.get("ref"))
    tool.remember(action, result, ctx.memory)
    return Outcome("ok", result["text"], result.get("data", {}), describe=prepared.describe,
                   untrusted=spec.reads_untrusted)


def _request_approval(tool_id: str, action: str, prepared: Prepared, ctx: ToolContext,
                      notify: Optional[Callable[[str, str], None]]) -> Outcome:
    task = ctx.task or {}
    if not task.get("id"):
        return Outcome("error", "This needs your approval, but there's no chat to ask you in.")
    payload = {"tool": tool_id, "action": action, "args": prepared.args, "capabilities": prepared.capabilities,
               "target": prepared.target, "describe": prepared.describe,
               "preview": prepared.preview or {"kind": "action", "title": "APPROVAL", "action": prepared.describe,
                                               "approve": "Approve", "deny": "Cancel", "fields": [], "warnings": []}}
    tool_name, body = f"{PREFIX}{tool_id}.{action}", json.dumps(payload)
    # The model may propose the same thing twice in one run; ask once.
    for a in db.list_pending_approvals():
        if a["task_id"] == task["id"] and a["tool_name"] == tool_name and a["tool_input"] == body:
            return Outcome("awaiting_approval", f"Waiting for your approval: {prepared.describe}.", approval_id=a["id"],
                           describe=prepared.describe)
    approval = db.create_approval(task["id"], tool_name, body)
    audit.log(tool_id, action, prepared.target, approval="requested", result="pending", task_id=task["id"],
              approval_id=approval["id"])
    db.add_task_event(task["id"], "approval", "Waiting for your OK: " + prepared.describe)
    if notify:
        try:
            notify("Approval needed", prepared.describe)
        except Exception:
            log.warning("approval notification failed", exc_info=True)
    return Outcome("awaiting_approval", f"Waiting for your approval: {prepared.describe}.", approval_id=approval["id"],
                   describe=prepared.describe)


# ------------------------------------------------------------------- decide

def _redact(payload: dict[str, Any]) -> str:
    """Once decided, keep what the card said (short) but not the content that
    was needed to run it - message bodies, event notes."""
    slim = {k: v for k, v in payload.items() if k != "args"}
    pv = dict(slim.get("preview") or {})
    pv["fields"] = [{**f, "value": (f["value"][:120] + "…") if len(f["value"]) > 120 else f["value"]}
                    for f in pv.get("fields", [])]
    slim["preview"] = pv
    slim["redacted"] = True
    return json.dumps(slim)


def _memory_for(task: Optional[dict[str, Any]]) -> tuple[Optional[int], dict[str, Any]]:
    if not task:
        return None, {}
    root = task.get("thread_id") or task["id"]
    return root, db.get_assistant_context(root)


def decide(approval: dict[str, Any], approved: bool, note: Optional[str] = None,
           ctx_factory: Callable[..., ToolContext] = make_context) -> dict[str, Any]:
    """Apply the owner's decision. Idempotent: a second call on a decided
    approval reports its state and does nothing."""
    if approval["status"] != "pending":
        return {"status": approval["status"], "ok": approval["status"] == "approved", "text": ""}
    payload = json.loads(approval["tool_input"])
    tool_id, action = payload["tool"], payload["action"]
    task = db.get_task(approval["task_id"])
    describe, target = payload.get("describe", ""), payload.get("target")

    def finish(line: str, kind: str = "approval") -> None:
        db.add_task_event(approval["task_id"], kind, line)
        db.append_task_result(approval["task_id"], line)
        db.set_approval_input(approval["id"], _redact(payload))

    if not approved:
        if not db.claim_approval(approval["id"], False, note):
            return {"status": "already_decided", "ok": False, "text": ""}
        audit.log(tool_id, action, target, approval="denied", result="denied", task_id=approval["task_id"],
                  approval_id=approval["id"])
        finish(f"✕ You declined: {describe}" + (f' ("{note.strip()}")' if note and note.strip() else ""))
        return {"status": "denied", "ok": True, "text": "Declined - nothing was done."}

    created = datetime.fromisoformat(approval["created_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - created > timedelta(hours=APPROVAL_TTL_HOURS):
        db.timeout_approval(approval["id"])
        audit.log(tool_id, action, target, approval="expired", result="denied", task_id=approval["task_id"],
                  approval_id=approval["id"])
        finish(f"⏱ This approval expired, so nothing was done: {describe}")
        return {"status": "expired", "ok": False, "text": "That approval expired. Ask again."}
    scoped_out = scope.refusal(tool_id, payload.get("capabilities", []))
    if scoped_out:  # the tool or permission was taken away while this waited
        db.claim_approval(approval["id"], False, "no longer permitted")
        audit.log(tool_id, action, target, approval="denied", result="denied", task_id=approval["task_id"],
                  approval_id=approval["id"], error=scoped_out)
        finish(f"✕ Not done - {scoped_out}")
        return {"status": "blocked", "ok": False, "text": scoped_out}
    if capabilities.resolve(payload.get("capabilities", [])) == capabilities.DENY:
        db.claim_approval(approval["id"], False, "blocked by policy")
        audit.log(tool_id, action, target, approval="denied", result="denied", task_id=approval["task_id"],
                  approval_id=approval["id"], error="Blocked by your permission settings")
        finish(f"✕ Blocked by your permission settings: {describe}")
        return {"status": "blocked", "ok": False, "text": "Your permission settings now block this action."}
    if not db.claim_approval(approval["id"], True, note):
        return {"status": "already_decided", "ok": False, "text": ""}

    root, memory = _memory_for(task)
    ctx = ctx_factory(task, memory)
    tool = tools.get(tool_id)
    prepared = Prepared(args=payload["args"], capabilities=payload.get("capabilities", []), target=target or "",
                        describe=describe)
    outcome = _run(tool, tool_id, action, prepared, ctx, "approved", approval["id"])
    if root is not None:
        db.set_assistant_context(root, memory)
    if outcome.status == "ok":
        finish("✓ You approved. " + outcome.text)
        return {"status": "approved", "ok": True, "text": outcome.text}
    finish(f"⚠ You approved, but it failed: {outcome.text}")
    return {"status": "approved", "ok": False, "text": outcome.text}


def expire_stale(now: Optional[datetime] = None) -> int:
    """Time out approvals nobody answered, and say so in their chats."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=APPROVAL_TTL_HOURS)).isoformat()
    expired = db.expire_assistant_approvals(cutoff)
    for a in expired:
        payload = json.loads(a["tool_input"])
        audit.log(payload.get("tool", "?"), payload.get("action", "?"), payload.get("target"), approval="expired",
                  result="denied", task_id=a["task_id"], approval_id=a["id"])
        db.add_task_event(a["task_id"], "approval", "Approval expired: " + payload.get("describe", ""))
        db.append_task_result(a["task_id"], f"⏱ No answer in {APPROVAL_TTL_HOURS}h, so nothing was done: {payload.get('describe', '')}")
        db.set_approval_input(a["id"], _redact(payload))
    return len(expired)
