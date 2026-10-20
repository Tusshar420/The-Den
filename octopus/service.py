"""
The one pipeline every request goes through, whether it was typed on the
dashboard, spoken, sent from a Shortcut or posted to the API:

    submit(text)  ->  answer a plan that was waiting on you?
                  ->  continue a chat?
                  ->  build the plan (octopus/plan.py)
                  ->  an instant command?   run it now                       (status: done)
                  ->  needs a look first?   store the plan, wait for Run     (status: proposed / needs_input)
                  ->  otherwise             queue it                         (status: queued)

Every outcome is logged in the routing table (db.add_routing) so the Routing
page can show what Octopus decided and why. Raises ServiceError with an HTTP
status; api.py turns that into a response.
"""

import re
from typing import Any, Optional

import accounts
import db
import uploads
from octopus import control, plan as planner

FOLLOWUP_RE = re.compile(
    r"\b(it|that|this|those|them|these|afterwards?|again|too|also|next|instead|the (first|second|third|fourth|fifth|last|previous|other|same)"
    r"( one| issue| bug| problem| item| thing| file)?|number \d+|#\d+)\b|^\s*(and|then|also|now|ok|okay|but|so|after)\b", re.IGNORECASE)
FOLLOWUP_MAX_WORDS = 14
# "@octopus ..." in a chat: Octopus itself takes the message, not the model the chat is on (see handoff).
MENTION_RE = re.compile(r"(?<![\w@])@octopus\b[ \t,:;.!-]*", re.IGNORECASE)
# How a hand-off note in the chat starts - the dashboard reads these to offer "Open #N" / "Review plan".
HANDOFF_QUEUED, HANDOFF_PLAN = "Octopus is on it in #", "Octopus has a plan for this"
# Short spoken or typed replies to a plan that is waiting: matched on normalised words, so
# "Yes, go ahead!" and "yeah do it" both count. "cancel" / "stop" are left to the control commands.
YES_WORDS = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "go", "ahead", "do", "it", "run", "proceed", "approve", "approved",
             "confirm", "confirmed", "for", "sounds", "good", "please", "start", "thats", "right", "thanks", "thank", "you", "now"}
YES_CORE = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "go", "proceed", "approve", "approved", "confirm", "confirmed", "start", "do", "run"}


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]", " ", text.lower().replace("'", "")).split()


def _is_yes(text: str) -> bool:
    w = _words(text)
    return 0 < len(w) <= 5 and all(t in YES_WORDS for t in w) and any(t in YES_CORE for t in w)


def _is_no(text: str) -> bool:
    w = _words(text)
    if not 0 < len(w) <= 5:
        return False
    return w[0] in ("no", "nope", "nah", "dont", "nevermind") or w[:2] in (["do", "not"], ["never", "mind"], ["not", "now"], ["skip", "it"])


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def is_followup(text: str) -> bool:
    """A short reply that only makes sense as part of the last conversation:
    "fix the second one", "run the tests afterward", "and then push it"."""
    return len(text.split()) <= FOLLOWUP_MAX_WORDS and bool(FOLLOWUP_RE.search(text))


def _thread_mode(root_id: int) -> Optional[str]:
    """The Claude Code mode the chat last ran under - what a reply inherits."""
    return next((t["permission_mode"] for t in reversed(db.get_thread(root_id)) if t.get("permission_mode")), None)


def _with_position(task: dict[str, Any]) -> dict[str, Any]:
    return {**task, "position": db.queue_position(task["id"])}


def spoken_reply(status: str, plan: dict[str, Any], result: Optional[dict[str, Any]] = None) -> str:
    """What to say aloud about a request, built from the plan - never from a model's wording.
    The answer itself is fetched later with GET /octopus/result/{task_id}."""
    if result:
        return result.get("spoken") or result.get("text", "")
    agent = plan["agent"]["label"]
    model = (plan.get("model") or {}).get("label", "a model")
    where = f" in {plan['project']['name']}" if plan.get("project") and not plan["project"].get("is_general") else ""
    if status == "needs_input":
        names = [o["name"] for o in (plan.get("needs_input") or {}).get("options", [])][:4]
        return (plan["needs_input"]["question"] + (" " + ", ".join(names[:-1]) + (" or " if len(names) > 1 else "") + names[-1] + "?" if names else ""))
    if status == "proposed":
        risky = [p["label"].lower() for p in plan.get("permissions", []) if p.get("flagged")]
        why = f" It involves {' and '.join(risky[:2])}." if risky else (f" {plan['show_plan_reason'].capitalize()}." if plan.get("show_plan_reason") else "")
        if plan.get("blocked"):
            return f"I can't do that: {', '.join(plan['blocked']).lower()} is blocked by your permission settings."
        return f"Here's my plan: the {agent} on {model}{where}.{why} Say yes to go ahead, or no to cancel."
    if status == "queued":
        return f"On it. The {agent} is handling that with {model}{where}."
    return ""


def _outcome(status: str, plan: dict[str, Any], routing: dict[str, Any], task: Optional[dict[str, Any]] = None,
             **extra: Any) -> dict[str, Any]:
    return {"status": status, "task": _with_position(task) if task else None, "decision": planner.decision_of(plan),
            "plan": plan, "routing_id": routing["id"], "spoken": spoken_reply(status, plan, extra.get("result")), **extra}


def _queue(plan: dict[str, Any], routing: dict[str, Any], priority: int, mode: Optional[str],
           decided_by: str) -> dict[str, Any]:
    task = planner.queue_plan(plan, priority, routing["id"], mode)
    routing = db.update_routing(routing["id"], plan=plan, status="queued", task_id=task["id"], decided_by=decided_by)
    planner.log_events(task, plan, decided_by)
    return _outcome("queued", plan, routing, db.get_task(task["id"]))


def _run_control(plan: dict[str, Any], routing: dict[str, Any], text: str, source: str) -> dict[str, Any]:
    """An instant command: do it now, record it as a finished task so it shows
    up in history and a voice client can fetch the answer like any other."""
    out = control.run(plan["control"])
    general = planner.general_project()
    task = db.add_task(general["id"], text, 0, None, lane="control", model_id="octopus-control", category=plan["category"],
                       route_reason=plan["route_reason"], routed_by="rules", agent_id="operator", tools=["workspace"],
                       routing_id=routing["id"], source=source, risk="low", title=text[:60])
    db.mark_running(task["id"])
    db.mark_done(task["id"], out["text"])
    routing = db.update_routing(routing["id"], status="done", task_id=task["id"])
    return _outcome("done", plan, routing, db.get_task(task["id"]), result={"text": out["text"], "spoken": out["spoken"], "ok": out["ok"]})


# ------------------------------------------------------------------ pending

def _answer_pending(text: str, source: str, priority: int, mode: Optional[str]) -> Optional[dict[str, Any]]:
    """A short reply to a plan that is waiting on the human: "yes, go ahead",
    "no", or (when Octopus asked which project) the project's name."""
    pending = db.pending_plan()
    if not pending or len(text) > 80 or "\n" in text:
        return None
    plan = pending["plan"]
    if _is_yes(text):
        if plan.get("needs_input"):
            return None                       # a "yes" doesn't say which project
        return run_plan(pending["id"], priority=priority, permission_mode=mode)
    if _is_no(text):
        db.claim_routing(pending["id"], "cancelled")
        return {"status": "cancelled", "task": None, "plan": plan, "routing_id": pending["id"], "decision": planner.decision_of(plan),
                "spoken": "Okay, cancelled. Nothing was run.",
                "result": {"text": "Okay, cancelled - nothing was run.", "spoken": "Okay, cancelled. Nothing was run.", "ok": True}}
    if (plan.get("needs_input") or {}).get("kind") == "project":
        found = planner.find_project(text)
        if found:
            return run_plan(pending["id"], project_id=found["id"], priority=priority, permission_mode=mode)
    return None


# ------------------------------------------------------------------- submit

def submit(text: str, *, project_id: Optional[int] = None, model: str = "auto", agent: Optional[str] = None,
           priority: int = 0, permission_mode: Optional[str] = None, thread_id: Optional[int] = None,
           continue_recent: bool = False, source: str = "api", review: str = "auto",
           images: Optional[list[str]] = None, account: Optional[str] = None) -> dict[str, Any]:
    """account: a Claude account id to run this on instead of its project's (accounts.py).
    review: "auto" (Octopus decides whether to show the plan first) | "skip"
    (the caller has already seen it - run it) | "force" (always show it).
    images: upload ids (uploads.py) - the model chosen is one that can read them."""
    text = (text or "").strip()
    if not text:
        raise ServiceError(400, "request is empty")
    try:
        images = uploads.validate(images)
    except ValueError as e:
        raise ServiceError(400, str(e))
    if permission_mode is not None and permission_mode not in db.PERMISSION_MODES:
        raise ServiceError(400, f"permission_mode must be one of {', '.join(db.PERMISSION_MODES)}")
    if review not in ("auto", "skip", "force"):
        raise ServiceError(400, "review must be auto, skip or force")
    if account and not accounts.exists(account):
        raise ServiceError(400, "That Claude account isn't set up (any more)")
    account = account or None
    if project_id is not None:
        target = db.get_project(project_id)
        if target and not db.has_folder(target):   # a CRM-only project: nothing runs in it
            raise ServiceError(400, db.NO_FOLDER_HINT)
    db.expire_old_plans()

    if thread_id is None and project_id is None and model == "auto" and not agent and not images:
        answered = _answer_pending(text, source, priority, permission_mode)
        if answered:
            return answered

    # ---- a reply within a chat
    root = None
    if thread_id is not None:
        root = db.get_task(thread_id)
        if not root:
            raise ServiceError(404, "thread_id does not refer to a task")
    elif continue_recent:
        recent = db.recent_thread(15) if is_followup(text) else None
        recent = recent or db.recent_assistant_thread(15)
        root = db.get_task(recent) if recent else None
    if root:
        root = db.get_task(root["thread_id"] or root["id"])
        if thread_id is not None and mentions_octopus(text):
            return handoff(text, root, priority=priority, permission_mode=permission_mode, source=source,
                           images=images, account=account)
        plan = planner.build_plan(text, thread_root=root, force_model=model, force_agent=agent, source=source, images=images)
        if plan["blocked"] or not plan["model"]:
            raise ServiceError(409, plan["unavailable"] or "Blocked by your permission settings: " + ", ".join(plan["blocked"]))
        plan["account"] = account
        routing = db.add_routing(text, plan, "queued", source, "auto")
        return _queue(plan, routing, priority, permission_mode or _thread_mode(root["id"]), "auto")

    # ---- a new request
    try:
        plan = planner.build_plan(text, project_id=project_id, force_model=model, force_agent=agent, source=source, images=images)
    except ValueError as e:
        raise ServiceError(404 if "Project not found" in str(e) else 409 if "No agent" in str(e) else 400, str(e))
    plan["account"] = account
    decided_by = "user" if plan["overrides"] or project_id is not None else "auto"

    if plan["control"]:
        routing = db.add_routing(text, plan, "queued", source, decided_by)
        return _run_control(plan, routing, text, source)

    if not plan["model"] and not plan["needs_input"]:
        db.add_routing(text, plan, "failed", source, decided_by)
        raise ServiceError(409, "No model can take this right now: " + (plan["unavailable"] or "nothing is configured for it"))

    if plan["blocked"] and review == "skip":
        raise ServiceError(403, "Blocked by your permission settings: " + ", ".join(plan["blocked"]))

    # "skip" means the caller already saw the plan - unless it still needs an answer.
    needs_look = bool(plan["show_plan"] or review == "force" or plan["needs_input"])
    if review == "skip" and not plan["needs_input"]:
        needs_look = False
    if needs_look:
        status = "needs_input" if plan["needs_input"] else "proposed"
        routing = db.add_routing(text, plan, status, source, decided_by)
        return _outcome(status, plan, routing)

    routing = db.add_routing(text, plan, "queued", source, decided_by)
    return _queue(plan, routing, priority, permission_mode, decided_by)


# ------------------------------------------------------------------ mention

def mentions_octopus(text: str) -> bool:
    return bool(MENTION_RE.search(text or ""))


def strip_mention(text: str) -> str:
    """ "@octopus, run the tests" -> "run the tests"; "can @Octopus check it" -> "can check it"."""
    return re.sub(r"[ \t]{2,}", " ", MENTION_RE.sub(" ", text or "")).strip()


def handoff(text: str, root: dict[str, Any], *, priority: int = 0, permission_mode: Optional[str] = None,
            source: str = "dashboard", images: Optional[list[str]] = None,
            account: Optional[str] = None) -> dict[str, Any]:
    """A chat message that mentions @octopus. Octopus handles it as a request of
    its own - its own pick of agent, model and tools, in the chat's project unless
    the request names another - and the model the chat is on never gets it. The
    chat keeps a finished note (control lane, so no worker ever runs it) pointing
    at Octopus's task or plan. Returns submit()'s outcome plus `note`, that record."""
    request = strip_mention(text)
    if not request:
        raise ServiceError(400, "Tell Octopus what to do after @octopus")
    root = db.get_task(root["thread_id"] or root["id"])
    project = db.get_project(root["project_id"])
    here = (project if project and db.has_folder(project) and not db.is_general_project(project)
            and not planner.find_project(request) else None)
    out = submit(request, project_id=here["id"] if here else None, priority=priority, permission_mode=permission_mode,
                 source=source, images=images, account=account)
    plan, task, status = out["plan"], out.get("task"), out["status"]
    if status == "queued":
        m = plan.get("model") or {}
        summary = f"{HANDOFF_QUEUED}{task['id']} · the {plan['agent']['label']} on {m.get('label', m.get('id', 'a model'))}"
    elif status in ("proposed", "needs_input"):
        summary = f"{HANDOFF_PLAN} - " + ((plan.get("needs_input") or {}).get("question") or "it needs your OK first")
    else:                                     # an instant answer, or "no" to a waiting plan
        summary = (out.get("result") or {}).get("text") or "Done"
    note = db.add_task(root["project_id"], text, priority, root["id"], lane="control", model_id=None,
                       category=root.get("category"), routed_by="mention", routing_id=out.get("routing_id"),
                       route_reason="You mentioned @octopus, so Octopus took this instead of the chat's model",
                       source=source, images=images)
    db.mark_done(note["id"], summary)
    return {**out, "note": db.get_task(note["id"])}


# ---------------------------------------------------------------- run/cancel

def run_plan(routing_id: int, *, project_id: Optional[int] = None, model: Optional[str] = None,
             agent: Optional[str] = None, priority: int = 0, permission_mode: Optional[str] = None) -> dict[str, Any]:
    """Run a plan that was waiting on the human ([Run] on the plan card), optionally
    changing the project, model or agent first. Exactly once."""
    row = db.get_routing(routing_id)
    if not row:
        raise ServiceError(404, "Plan not found")
    if row["status"] not in ("proposed", "needs_input"):
        raise ServiceError(409, f"That plan was already {row['status']}")
    if permission_mode is not None and permission_mode not in db.PERMISSION_MODES:
        raise ServiceError(400, f"permission_mode must be one of {', '.join(db.PERMISSION_MODES)}")
    plan = row["plan"]
    changed = project_id is not None or (model and model != "auto") or agent
    if changed or plan.get("needs_input"):
        keep_project = (plan.get("project") or {}) if not (plan.get("project") or {}).get("is_general") else {}
        try:
            plan = planner.build_plan(
                plan["request"], project_id=project_id if project_id is not None else keep_project.get("id"),
                force_model=model if model and model != "auto" else plan["overrides"].get("model"),
                force_agent=agent or plan["overrides"].get("agent"), source=plan.get("source", "api"),
                images=plan.get("images"))
        except ValueError as e:
            raise ServiceError(400, str(e))
    if plan["needs_input"]:
        raise ServiceError(409, plan["needs_input"]["question"])
    if not plan["model"]:
        raise ServiceError(409, "No model can take this right now: " + (plan["unavailable"] or "nothing is configured for it"))
    if plan["blocked"]:
        raise ServiceError(403, "Blocked by your permission settings: " + ", ".join(plan["blocked"]))
    if not db.claim_routing(routing_id, "queued"):
        raise ServiceError(409, "That plan was already run or cancelled")
    try:
        return _queue(plan, db.get_routing(routing_id), priority, permission_mode, "user")
    except Exception:
        db.update_routing(routing_id, status="proposed")     # let them try again
        raise


def cancel_plan(routing_id: int) -> dict[str, Any]:
    row = db.get_routing(routing_id)
    if not row:
        raise ServiceError(404, "Plan not found")
    if not db.claim_routing(routing_id, "cancelled"):
        raise ServiceError(409, f"That plan was already {row['status']}")
    return db.get_routing(routing_id)
