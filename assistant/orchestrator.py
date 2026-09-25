"""
Runs one assistant-lane task: decides between the no-model path and the
planner, keeps the chat's working memory, and returns text for the chat.

    text -> intent.classify -> fastpath.plan  -- steps found -> actions.invoke each  (no model)
                                              -- None        -> planner.run           (a model proposes tool calls)

Both paths end in actions.invoke, so policy, approval and the activity log
are identical however the request was understood.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import db
from assistant import actions, fastpath, intent as intent_mod, planner, tools

log = logging.getLogger("the-den.assistant")


@dataclass
class TaskOutput:
    text: str
    spoken: str
    approvals: list[int] = field(default_factory=list)
    ok: bool = True
    used_model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    duration_ms: int = 0


def approval_sentence(describes: list[str]) -> str:
    """What to say aloud when something is waiting on the owner. Built from
    what the approval card says, never from a model's wording."""
    if len(describes) == 1 and describes[0]:
        d = describes[0]
        return f"I need your approval to {d[:1].lower() + d[1:]}. Check your dashboard or phone."
    return f"I need your approval for {max(len(describes), 1)} things. Check your dashboard or phone."


def spoken_summary(text: str, approval_describes: Optional[list[str]] = None) -> str:
    """A sentence fit for text-to-speech from a chat answer: the first line,
    without markup, trimmed. Pending approvals take over: that is what the
    owner has to act on."""
    if approval_describes:
        return approval_sentence(approval_describes)
    first = next((l for l in text.splitlines() if l.strip()), "")
    first = re.sub(r"[•*_`#>]", "", first).strip()
    return (first[:220].rsplit(" ", 1)[0] + "…") if len(first) > 220 else first


def _event(task: dict[str, Any], line: Optional[str]) -> None:
    if line:
        db.add_task_event(task["id"], "tool", line)


def run_fast(steps: list[fastpath.Step], ctx: tools.ToolContext,
             notify: Optional[Callable[[str, str], None]]) -> TaskOutput:
    texts: list[str] = []
    approvals: list[int] = []
    describes: list[str] = []
    last: Optional[actions.Outcome] = None
    for step in steps:
        args = dict(step.args)
        if args.get("message_id") == fastpath.FIRST_MESSAGE:
            found = (last.data.get("messages") if last else None) or []
            if not found:
                return TaskOutput(texts[-1] if texts else "I couldn't find a matching email.", "I couldn't find a matching email.")
            args["message_id"] = found[0]["id"]
        # No model is involved, so nothing has been interpreted from untrusted
        # text: a fast-path step is never "tainted" by earlier steps.
        outcome = actions.invoke(step.tool, step.action, args, ctx, tainted=False, notify=notify)
        last = outcome
        if outcome.status == "ok":
            _event(ctx.task, outcome.describe)
            texts.append(outcome.text)
        elif outcome.status == "awaiting_approval":
            texts.append(outcome.text)
            if outcome.approval_id:
                approvals.append(outcome.approval_id)
                describes.append(outcome.describe)
        else:
            _event(ctx.task, f"Couldn't {outcome.describe.lower()}: {outcome.text}" if outcome.describe else None)
            return TaskOutput(outcome.text, spoken_summary(outcome.text))
    # A search-then-read chain shows only the message, not the search listing.
    text = texts[-1] if len(steps) > 1 and steps[-1].action == "read" else "\n\n".join(texts)
    return TaskOutput(text, spoken_summary(text, describes), approvals)


def run_task(task: dict[str, Any], notify: Optional[Callable[[str, str], None]] = None,
             ctx_factory: Callable[..., tools.ToolContext] = actions.make_context,
             complete: Optional[Callable[..., Any]] = None) -> TaskOutput:
    """May raise planner.PlannerUnavailable (the worker requeues or reports it)."""
    started = time.time()
    text = task["prompt"]
    root = task.get("thread_id") or task["id"]
    memory = db.get_assistant_context(root)
    ctx = ctx_factory(task, memory)
    in_thread = bool(task.get("thread_id"))
    found = intent_mod.classify(text, in_thread=in_thread)
    steps = None
    if found.matched and found.intent not in intent_mod.ALWAYS_AI:
        steps = fastpath.plan(found.intent, text, ctx.now())

    if steps:
        out = run_fast(steps, ctx, notify)
    else:
        kwargs = {"complete": complete} if complete else {}
        plan = planner.run(task, text, ctx, notify, **kwargs)
        describes = [json.loads(db.get_approval(i)["tool_input"]).get("describe", "") for i in plan.approvals]
        # Models the ai tool called on the planner's behalf count towards the task's usage too.
        used = ctx.usage
        spent = [c for c in (plan.cost_usd, used["cost_usd"]) if c is not None]
        out = TaskOutput(plan.message, spoken_summary(plan.message, describes) if describes else
                         (plan.spoken or spoken_summary(plan.message)), plan.approvals,
                         used_model=plan.model_id, input_tokens=(plan.input_tokens + used["input_tokens"]) or None,
                         output_tokens=(plan.output_tokens + used["output_tokens"]) or None,
                         cost_usd=round(sum(spent), 6) if spent else None)
        if plan.model_id:
            db.add_task_event(task["id"], "note", f"Interpreted by {plan.model_id}")
        if used["models"]:
            db.add_task_event(task["id"], "note", "Also asked " + ", ".join(used["models"]))
    db.set_assistant_context(root, ctx.memory)
    out.duration_ms = int((time.time() - started) * 1000)
    return out
