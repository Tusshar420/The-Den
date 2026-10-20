"""
The Alexa skill: another way in and out of Octopus, like the dashboard's microphone - never a second
assistant. Every request goes through the pipeline typed requests use (octopus.service.submit), every
approval through the dashboard's own decide endpoint.

    "Alexa, open my octopus"            anything waiting on you (approvals, finished tasks), then "what would you like?"
    "what's in the queue?"              running + next up + approvals              (QueueIntent)
    "what is octopus working on?"       running tasks, their current step, progress (WorkingOnIntent)
    "what approvals are waiting?"       read each one, "approve" / "deny" / "skip" (ApprovalsIntent, Yes / No)
    "add a new task"                    which project? what should it do? read back, yes -> queued for Claude Code
    "push the current code to GitHub"   which project, read back, yes -> a Claude Code task that commits + pushes
    "let's discuss a task for X"        a real back-and-forth with the Voice Partner agent (agents.json "voice"),
                                        kept as one chat in The Den; "queue it" hands the whole conversation to
                                        Claude Code as a task
    anything else                       octopus.service.submit - instant commands, assistant, questions. Work that
                                        would start Claude Code is read back and needs a yes first.

Alexa must answer within 8 seconds, so an answer that isn't ready after REPLY_SECONDS is held: "still
thinking - say 'go on'", and it's read out on the next turn. Conversation state lives in the session
attributes Alexa hands back each turn, plus state.get()["conversation"] so a chat survives the session
timing out.
"""

import difflib
import json
import os
import re
import time
from typing import Any, Callable, Optional

import db
import risk
import router
from alexa import state
from octopus import control, service
from octopus import plan as planner
from octopus.voice import clean_for_speech

REPLY_SECONDS = float(os.environ.get("ALEXA_REPLY_SECONDS", "6"))
RESUME_WITHIN_SECONDS = 15 * 60       # "open my octopus" again soon after: pick the conversation back up
SPEAK_LIMIT = 700
CARD_LIMIT = 7000
TRANSCRIPT_MAX_CHARS = 12000
ANYTHING_ELSE = "Anything else?"
UNFINISHED = ("queued", "running", "rate_limited")

PUSH_PROMPT = ("Push the current code to GitHub: commit any uncommitted changes with a clear commit message, then push "
               "the current branch to its remote. Do not force-push. If the push is rejected, or there is nothing to "
               "push, stop and say why.")
DISCUSSION_TASK_PROMPT = (
    "Do the work Tushar talked through with Octopus in the voice conversation below. It happened over Alexa, so "
    "some words may be mis-transcribed - go by the meaning. Where the conversation changed its mind, the later part "
    "wins. If something essential is still unclear, make a sensible choice and say what you chose in your summary."
    "\n\nThe conversation:\n")
HELP = ("You can ask me what's in the queue, what Octopus is working on, or what approvals are waiting. You can say "
        "add a new task, push the current code to GitHub, or let's discuss a task, to talk something through before "
        "it's queued. Anything else goes to Octopus like a message on the dashboard.")

NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10"}


# ------------------------------------------------------------------ the turn

class Turn:
    """One request from Alexa and what we'll say back."""

    def __init__(self, envelope: dict[str, Any]):
        self.env = envelope
        self.req = envelope.get("request") or {}
        self.session = envelope.get("session") or {}
        self.attrs: dict[str, Any] = dict(self.session.get("attributes") or {})
        self.started = time.monotonic()
        self.pre: list[str] = []          # said before the answer: an earlier answer now ready, a new approval
        self.card: Optional[tuple[str, str]] = None
        self.consent_card = False

    def left(self) -> float:
        return REPLY_SECONDS - (time.monotonic() - self.started)

    def intent(self) -> str:
        return ((self.req.get("intent") or {}).get("name")) or ""

    def slot(self, name: str) -> str:
        return str((((self.req.get("intent") or {}).get("slots") or {}).get(name) or {}).get("value") or "").strip()

    def slot_id(self, name: str) -> Optional[str]:
        """The entity-resolution id Alexa matched the slot to (project slots carry the project id)."""
        s = (((self.req.get("intent") or {}).get("slots") or {}).get(name) or {})
        for auth in ((s.get("resolutions") or {}).get("resolutionsPerAuthority") or []):
            if (auth.get("status") or {}).get("code") == "ER_SUCCESS_MATCH" and auth.get("values"):
                return str(auth["values"][0]["value"].get("id") or "")
        return None

    def mode(self, mode: Optional[str] = None, **extra: Any) -> None:
        for k in ("mode", "approval_id", "routing_id", "draft", "pin_tries"):
            self.attrs.pop(k, None)
        if mode:
            self.attrs["mode"] = mode
        self.attrs.update({k: v for k, v in extra.items() if v is not None})


def respond(t: Turn, text: str, reprompt: Optional[str] = ANYTHING_ELSE, end: bool = False) -> dict[str, Any]:
    speech = " ".join(p for p in t.pre + [text] if p).strip() or "Okay."
    out: dict[str, Any] = {"version": "1.0", "sessionAttributes": t.attrs,
                           "response": {"outputSpeech": {"type": "PlainText", "text": speech[:7900]}, "shouldEndSession": end}}
    if reprompt and not end:
        out["response"]["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    if t.consent_card:
        out["response"]["card"] = {"type": "AskForPermissionsConsent", "permissions": ["alexa::devices:all:notifications:write"]}
    elif t.card:
        out["response"]["card"] = {"type": "Simple", "title": t.card[0][:100], "content": t.card[1][:CARD_LIMIT]}
    _remember(t)
    return out


def _remember(t: Turn) -> None:
    """Keep the conversation past the session (a pause longer than Alexa waits ends the session)."""
    keep = {k: t.attrs[k] for k in ("thread_id", "project_id", "chat") if t.attrs.get(k)}
    if t.attrs.get("mode") == "discuss":
        keep["mode"] = "discuss"
    state.update(lambda st: st.update(conversation={**keep, "at": time.time()}))


def empty() -> dict[str, Any]:
    return {"version": "1.0", "response": {}}


# ------------------------------------------------------------------ speech bits

def speakable(text: str, limit: int = SPEAK_LIMIT) -> str:
    return clean_for_speech(text or "", limit)


def _quote(text: str, n: int = 90) -> str:
    t = speakable(text, n)
    return t.rstrip(".…")


def _name(task: dict[str, Any]) -> str:
    return _quote(task.get("title") or task.get("prompt") or "a task", 70)


def _where(project_id: Optional[int]) -> str:
    p = db.get_project(project_id) if project_id else None
    return "" if not p or db.is_general_project(p) else f" in {p['name']}"


def _minutes(iso: Optional[str]) -> str:
    from datetime import datetime, timezone
    if not iso:
        return ""
    try:
        mins = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() // 60)
    except ValueError:
        return ""
    if mins < 1:
        return "just started"
    if mins < 90:
        return f"for {mins} minute{'s' if mins != 1 else ''}"
    return f"for about {round(mins / 60)} hours"


def _command_words(cmd: str) -> str:
    c = re.sub(r"\s+", " ", cmd).strip()
    c = c.replace(" && ", ", then ").replace(" | ", ", piped into ").replace(" > ", " into ").replace("`", "")
    return c if len(c) <= 160 else c[:157].rsplit(" ", 1)[0] + "..."


def _answer_text(task: dict[str, Any]) -> str:
    return (task.get("result_summary") if task["status"] == "done" else task.get("error_message")) or ""


# ------------------------------------------------------------------ projects

def _project_key(text: str) -> str:
    words = [NUMBER_WORDS.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower())]
    return "".join(words)


def match_project(spoken: str, resolved_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """A project from what Alexa heard: the id Alexa resolved the slot to, a name in the words, or the
    closest name ("code aura seven" -> KodeAura7). "General" means the General workspace."""
    if resolved_id and resolved_id.isdigit():
        p = db.get_project(int(resolved_id))
        if p and p.get("active", 1):
            return p
    if not spoken:
        return None
    if re.fullmatch(r"(the )?general( workspace| project)?|(no|none|nothing|any)( project)?", spoken.lower().strip()):
        return planner.general_project()
    found = planner.find_project(spoken)
    if found:
        return found
    key = _project_key(spoken)
    best, score = None, 0.0
    for p in planner.real_projects():
        pk = _project_key(re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", p["name"]))
        r = difflib.SequenceMatcher(None, key, pk).ratio()
        if r > score:
            best, score = p, r
    return best if score >= 0.75 else None


def _likely_project(t: Turn) -> Optional[dict[str, Any]]:
    """"The current code": the session's project, else what Claude Code is working in, else where it last worked."""
    if t.attrs.get("project_id"):
        p = db.get_project(t.attrs["project_id"])
        if p and not db.is_general_project(p):
            return p
    running = [x for x in (db.queue_status().get("running_all") or []) if x.get("lane") == "agent"]
    recent = sorted((x for x in db.list_tasks(statuses=["done", "needs_review", "error"]) if x.get("lane") == "agent"),
                    key=lambda x: x.get("finished_at") or "", reverse=True)
    for x in running + recent[:1]:
        p = db.get_project(x["project_id"])
        if p and not db.is_general_project(p):
            return p
    real = planner.real_projects()
    return real[0] if len(real) == 1 else None


def _ask_project(t: Turn, mode: str, question: str, **extra: Any) -> dict[str, Any]:
    names = [p["name"] for p in planner.real_projects()][:5]
    t.mode(mode, **extra)
    options = (" You have " + ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1] + ".") if names else ""
    return respond(t, question + options, "Which project? Say its name, or cancel.")


# ------------------------------------------------------------------ waiting on answers

def _wait(t: Turn, task_id: int) -> dict[str, Any]:
    try:
        import worker
        worker.wake_api_lane()
    except Exception:
        pass
    task = db.get_task(task_id)
    while task and task["status"] in UNFINISHED and t.left() > 0.35:
        time.sleep(0.25)
        task = db.get_task(task_id)
    return task or {}


def _deliver(t: Turn, task_id: int, lead: str = "") -> dict[str, Any]:
    """Speak the answer to a task we queued, if it's ready in time; otherwise hold it for 'go on'."""
    task = _wait(t, task_id)
    mine = [a for a in db.list_pending_approvals() if a["task_id"] == task_id]
    if mine:
        t.attrs.pop("pending_task_id", None)
        return _offer(t, ("approval", mine[0]), lead=lead)
    if not task or task["status"] in UNFINISHED:
        t.attrs["pending_task_id"] = task_id
        return respond(t, lead + "Still thinking about that. Say 'go on' in a moment to hear it.", "Say 'go on' to hear the answer.")
    t.attrs.pop("pending_task_id", None)
    text = _answer_text(task)
    said = speakable(text)
    if len(said) < len(clean_for_speech(text, 100000)):
        t.card = ("Octopus", text)
        said += " The full answer is in the Alexa app and on The Den."
    if task["status"] != "done":
        said = "That didn't work: " + (said or "no reason given.")
    reprompt = "Go on, I'm listening." if t.attrs.get("mode") == "discuss" else ANYTHING_ELSE
    return respond(t, lead + said, reprompt)


def _ready_earlier_answer(t: Turn) -> None:
    """A held answer that has finished since: say it before whatever comes next."""
    tid = t.attrs.get("pending_task_id")
    task = db.get_task(tid) if tid else None
    if task and task["status"] not in UNFINISHED:
        t.attrs.pop("pending_task_id", None)
        t.pre.append("About your earlier question: " + (speakable(_answer_text(task), 400) or "it finished."))


def _heads_up(t: Turn) -> None:
    """Mid-conversation, mention an approval that arrived since - once per session."""
    if t.attrs.get("mode") in ("approval", "pin"):
        return
    mentioned = set(t.attrs.get("mentioned") or [])
    new = [a for a in state.pending_approvals() if a["id"] not in mentioned]
    if new:
        a = new[0]
        t.attrs["mentioned"] = sorted(mentioned | {a["id"] for a in new})
        t.pre.append(f"Heads up: {_approval_where(a)} needs your OK. Say 'approvals' when you want to hear it.")
        state.update(lambda st: st.update(announced=st.get("announced", []) + [x["id"] for x in new]))


# ------------------------------------------------------------------ approvals and waiting plans

def _approval_where(a: dict[str, Any]) -> str:
    p = db.get_project(a.get("project_id")) if a.get("project_id") else None
    return p["name"] if p and not db.is_general_project(p) else "Octopus"


def describe_approval(a: dict[str, Any]) -> tuple[str, str]:
    """(what to say, what the card shows) for one approval."""
    from assistant import actions as assistant_actions
    try:
        payload = json.loads(a.get("tool_input") or "{}")
    except ValueError:
        payload = {}
    task = _name({"title": a.get("task_title"), "prompt": a.get("task_prompt")})
    if assistant_actions.is_assistant_approval(a):
        d = payload.get("describe") or "do something on your behalf"
        return f"Octopus wants to {d[:1].lower() + d[1:]}.", d
    where = _approval_where(a)
    if a["tool_name"] == "Bash":
        cmd = risk.command_of("Bash", a.get("tool_input") or "{}")
        read = risk.assess("Bash", cmd, None)
        warn = next((r["text"] for r in read["reasons"] if r["kind"] == "warn"), "")
        said = f"{where}, working on '{task}', wants to run: {_command_words(cmd)}."
        if read["level"] >= 3 and warn:
            said += f" That's high risk: {warn.rstrip('.').lower()}."
        return said, f"{where} · {task}\n\n$ {cmd}\n\nRisk: {read['risk']}"
    target = payload.get("file_path") or payload.get("path") or payload.get("url") or ""
    target = os.path.basename(str(target)) if target else ""
    return (f"{where}, working on '{task}', wants to use {a['tool_name']}" + (f" on {target}" if target else "") + ".",
            f"{where} · {task}\n\n{a['tool_name']}: {json.dumps(payload)[:1500]}")


def _waiting_plans() -> list[dict[str, Any]]:
    db.expire_old_plans()
    return [r for r in db.list_routing(20) if r["status"] in ("proposed", "needs_input")]


def _waiting(t: Turn) -> list[tuple[str, dict[str, Any]]]:
    skipped = set(t.attrs.get("skipped") or [])
    items: list[tuple[str, dict[str, Any]]] = [("approval", a) for a in db.list_pending_approvals() if f"a{a['id']}" not in skipped]
    items += [("plan", p) for p in _waiting_plans() if f"p{p['id']}" not in skipped]
    return items


def _offer(t: Turn, item: tuple[str, dict[str, Any]], lead: str = "") -> dict[str, Any]:
    kind, x = item
    if kind == "approval":
        said, card = describe_approval(x)
        t.mode("approval", approval_id=x["id"])
        t.attrs["mentioned"] = sorted(set(t.attrs.get("mentioned") or []) | {x["id"]})
        t.card = ("Approval needed", card)
        return respond(t, f"{lead}{said} Approve or deny?", "Say approve or deny, or skip to leave it for later.")
    t.mode("plan", routing_id=x["id"])
    spoken = service.spoken_reply(x["status"], x["plan"])
    if x["status"] == "needs_input":
        return respond(t, f"{lead}Octopus has a request waiting: '{_quote(x['request'])}'. {spoken}", "Which project?")
    return respond(t, f"{lead}Octopus has a plan waiting for '{_quote(x['request'])}'. {spoken}", "Say yes to run it, or no to cancel it.")


def _next_waiting(t: Turn, done_text: str) -> dict[str, Any]:
    items = _waiting(t)
    if items:
        more = f"{len(items)} more waiting. " if len(items) > 1 else "One more waiting. "
        return _offer(t, items[0], lead=f"{done_text} {more}".lstrip())
    if t.attrs.get("chat") == "discuss":           # an approval came up mid-discussion: go back to it
        t.mode("discuss")
        return respond(t, f"{done_text} Nothing else is waiting. Back to our conversation.".strip(), "Go on, I'm listening.")
    t.mode(None)
    return respond(t, f"{done_text} Nothing else is waiting on you.".strip())


def _decide(t: Turn, approved: bool, code: str = "") -> dict[str, Any]:
    aid = t.attrs.get("approval_id")
    a = db.get_approval(aid) if aid else None
    if not a or a["status"] != "pending":
        return _next_waiting(t, "That one was already dealt with.")
    pin = str(state.prefs().get("approval_pin") or "").strip()
    if approved and pin:
        digits = re.sub(r"\D", "", code)
        if not digits:
            t.attrs["mode"] = "pin"
            return respond(t, "What's your approval code?", "Say your approval code, or cancel.")
        if digits != pin:
            tries = int(t.attrs.get("pin_tries") or 0) + 1
            if tries >= 3:
                t.mode(None)
                return respond(t, "That code doesn't match either. Nothing was approved - you can still approve it in The Den.")
            t.attrs.update(mode="pin", pin_tries=tries)
            return respond(t, "That code doesn't match, so nothing was approved. Try again?", "Say your approval code, or cancel.")
    import api                                   # the dashboard's own decide, so both paths behave the same
    out = api.decide_approval(a["id"], api.ApprovalDecisionIn(approved=approved, note=None if approved else "Denied by voice, from Alexa."))
    assistant_result = (out or {}).get("assistant_result") if isinstance(out, dict) else None
    said = "Approved. Octopus is carrying on." if approved else "Denied. Octopus will work around it."
    if assistant_result and assistant_result.get("text"):
        said = ("Done. " if approved else "Denied. ") + speakable(assistant_result["text"], 250)
    return _next_waiting(t, said)


def _answer_plan(t: Turn, yes: bool) -> dict[str, Any]:
    rid = t.attrs.get("routing_id")
    row = db.get_routing(rid) if rid else None
    if not row or row["status"] not in ("proposed", "needs_input"):
        return _next_waiting(t, "That plan was already dealt with.")
    if not yes:
        try:
            service.cancel_plan(rid)
        except service.ServiceError:
            pass
        return _next_waiting(t, "Okay, cancelled. Nothing was run.")
    if row["status"] == "needs_input":
        return respond(t, service.spoken_reply("needs_input", row["plan"]), "Which project?")
    try:
        out = service.run_plan(rid)
    except service.ServiceError as e:
        return _next_waiting(t, f"I couldn't run it: {e.detail}")
    t.mode("discuss" if t.attrs.get("chat") == "discuss" else None)
    return _after_queue(t, out, lead="Running it. ")


# ------------------------------------------------------------------ queueing

def _submit(t: Turn, text: str, **kw: Any) -> Optional[dict[str, Any]]:
    try:
        return service.submit(text, source="alexa", **kw)
    except service.ServiceError as e:
        t.pre.append(f"Octopus couldn't take that: {speakable(e.detail, 200)}")
        return None


def _after_queue(t: Turn, out: dict[str, Any], lead: str = "") -> dict[str, Any]:
    """What to say once something is queued: wait for a quick answer, or say where a long job went."""
    task = out.get("task") or {}
    if not task:
        return respond(t, lead + (out.get("spoken") or "Done."))
    t.attrs["thread_id"] = task.get("thread_id") or task["id"]
    if task.get("lane") in ("api", "assistant"):
        return _deliver(t, task["id"], lead=lead)
    state.watch_task(task["id"])
    pos = task.get("position")
    line = f"Queued{_where(task['project_id'])} for Claude Code" + (f", number {pos} in line" if pos and pos > 1 else "") + "."
    return respond(t, f"{lead}{line} I'll tell you when it's done, or if it needs your OK.")


def _queue_for_claude(t: Turn, text: str, project: dict[str, Any], lead: str = "") -> dict[str, Any]:
    """Words already read back and confirmed: queue them in that project, Claude Code doing the work."""
    real = not db.is_general_project(project)
    out = _submit(t, text, project_id=project["id"], agent="coding" if real else None, review="skip")
    t.mode(None)
    if not out:
        return respond(t, "")
    if out["status"] in ("proposed", "needs_input"):
        return _offer(t, ("plan", db.get_routing(out["routing_id"])))
    return _after_queue(t, out, lead=lead)


# ------------------------------------------------------------------ status

def queue_summary() -> str:
    q = db.queue_status()
    running = q.get("running_all") or []
    queued = sorted(db.list_tasks(statuses=["queued"]), key=lambda x: (x.get("queue_pos") or 0, x["id"]))
    paused = [x for x in queued if x.get("paused")]
    queued = [x for x in queued if not x.get("paused")]
    parts = []
    if running:
        parts.append("Running now: " + " and ".join(f"'{_name(x)}'{_where(x['project_id'])}" for x in running) + ".")
    else:
        parts.append("Nothing is running right now.")
    if queued:
        names = [f"'{_name(x)}'{_where(x['project_id'])}" for x in queued[:3]]
        more = len(queued) - len(names)
        parts.append(f"{len(queued)} waiting in the queue. Next up: " + ", then ".join(names)
                     + (f", and {more} more." if more else "."))
    else:
        parts.append("The queue is empty.")
    if paused:
        parts.append(f"{len(paused)} paused.")
    rl = q.get("earliest_rate_limit_resume")
    if rl:
        parts.append(f"'{_name(rl)}' is waiting for a usage limit to reset.")
    n = len(db.list_pending_approvals())
    if n:
        parts.append(f"{n} approval{'s' if n != 1 else ''} waiting for you.")
    return " ".join(parts)


def working_summary() -> str:
    running = db.queue_status().get("running_all") or []
    if not running:
        queued = db.list_tasks(statuses=["queued"])
        return "Octopus isn't working on anything right now." + (f" {len(queued)} task{'s are' if len(queued) != 1 else ' is'} waiting." if queued else "")
    lines = []
    for x in running:
        model = router.get_model(x.get("model_id")) or {}
        line = f"'{_name(x)}'{_where(x['project_id'])}, with {model.get('label') or 'a model'}, {_minutes(x.get('started_at'))}."
        if x.get("current_step"):
            line += f" Right now: {_quote(x['current_step'], 80)}."
        steps = x.get("plan_steps") or []
        if steps:
            done = sum(1 for s in steps if s.get("status") == "completed")
            line += f" {done} of {len(steps)} steps done."
        lines.append(line)
    return ("Octopus is working on " if len(lines) == 1 else f"Octopus is working on {len(lines)} things. ") + " ".join(lines)


def _inbox(t: Turn) -> list[str]:
    """Finished tasks from Alexa he hasn't heard about."""
    out, told = [], []
    for x in state.finished_watched()[:3]:
        final = x["final"]
        text = speakable(_answer_text(final), 180)
        how = "finished" if final["status"] in ("done", "needs_review") else "was cancelled" if final["status"] == "cancelled" else "failed"
        out.append(f"'{_name(x)}'{_where(x['project_id'])} {how}" + (f": {text}" if text else "."))
        told.append(x["id"])
    state.mark_told(told)
    return out


# ------------------------------------------------------------------ handlers

def launch(t: Turn) -> dict[str, Any]:
    conv = state.get().get("conversation") or {}
    if conv and time.time() - float(conv.get("at") or 0) < RESUME_WITHIN_SECONDS:
        t.attrs.update({k: conv[k] for k in ("thread_id", "project_id", "mode", "chat") if conv.get(k)})
    news = _inbox(t)
    lead = ("While you were away: " + " ".join(news) + " ") if news else ""
    _offer_consent(t)
    items = _waiting(t)
    if items:
        return _offer(t, items[0], lead=f"{lead}{len(items)} thing{'s' if len(items) != 1 else ''} waiting on you. ")
    if t.attrs.get("mode") == "discuss":
        return respond(t, f"{lead}We were talking about '{_thread_title(t)}'. Carry on, or say stop discussing.", "Go on, I'm listening.")
    return respond(t, f"{lead}Octopus here. {control.status()['spoken']} What would you like?", "What would you like? You can say help.")


def _offer_consent(t: Turn) -> None:
    """Once: put the "allow notifications" card in the Alexa app, if notifications are set up but not allowed."""
    st = state.get()
    if all(state.client_credentials()) and st.get("subscribed") is not True and not st.get("consent_card_sent"):
        t.consent_card = True
        t.pre.append("I've put a card in your Alexa app - allow notifications there, so I can tell you when something needs your OK.")
        state.update(lambda s: s.update(consent_card_sent=True))


def _thread_title(t: Turn) -> str:
    root = db.get_task(t.attrs.get("thread_id")) if t.attrs.get("thread_id") else None
    return _name(root) if root else "your task"


def on_queue(t: Turn) -> dict[str, Any]:
    return respond(t, queue_summary())


def on_working(t: Turn) -> dict[str, Any]:
    return respond(t, working_summary())


def on_approvals(t: Turn) -> dict[str, Any]:
    t.attrs.pop("skipped", None)
    items = _waiting(t)
    if not items:
        return respond(t, "Nothing is waiting for your approval.")
    n_a = sum(1 for k, _ in items if k == "approval")
    n_p = len(items) - n_a
    bits = ([f"{n_a} approval{'s' if n_a != 1 else ''}"] if n_a else []) + ([f"{n_p} plan{'s' if n_p != 1 else ''}"] if n_p else [])
    return _offer(t, items[0], lead=" and ".join(bits) + " waiting. First: ")


def on_yes(t: Turn, code: str = "") -> dict[str, Any]:
    mode = t.attrs.get("mode")
    if mode in ("approval", "pin"):
        return _decide(t, True, code)
    if mode == "plan":
        return _answer_plan(t, True)
    if mode == "confirm_task":
        p = db.get_project(t.attrs.get("project_id"))
        return _queue_for_claude(t, t.attrs.get("draft") or "", p) if p else respond(t, "I lost track of the project - say add a new task to start again.")
    if mode == "confirm_push":
        p = db.get_project(t.attrs.get("project_id"))
        return _queue_for_claude(t, PUSH_PROMPT, p, lead="On it. ") if p else respond(t, "Which project? Say push, and the project's name.")
    if mode == "confirm_discussion":
        return _queue_discussion(t)
    if mode == "confirm_followup":
        return _send_followup(t, t.attrs.get("draft") or "")
    if mode == "confirm_stop":
        return _stop_running(t)
    if mode == "confirm_command":
        return _answer_plan(t, True)
    items = _waiting(t)
    if items:
        return _offer(t, items[0], lead="Which one? ")
    return respond(t, "There's nothing waiting for a yes right now.")


def on_no(t: Turn) -> dict[str, Any]:
    mode = t.attrs.get("mode")
    if mode in ("approval", "pin"):
        return _decide(t, False)
    if mode in ("plan", "confirm_command"):
        return _answer_plan(t, False)
    if mode == "confirm_task":
        t.mode("task_prompt", project_id=t.attrs.get("project_id"))
        return respond(t, "Okay. Tell me again what it should do, or say cancel.", "What should the task do?")
    if mode == "confirm_discussion":
        t.mode("discuss")
        return respond(t, "Okay, not yet. Let's keep talking.", "Go on, I'm listening.")
    if mode in ("confirm_push", "confirm_followup", "confirm_stop"):
        t.mode(None)
        return respond(t, "Okay, I won't.")
    return respond(t, "Okay.")


def on_skip(t: Turn) -> dict[str, Any]:
    mode = t.attrs.get("mode")
    key = f"a{t.attrs.get('approval_id')}" if mode in ("approval", "pin") else f"p{t.attrs.get('routing_id')}" if mode == "plan" else None
    if not key:
        return respond(t, "There's nothing to skip.")
    t.attrs["skipped"] = sorted(set(t.attrs.get("skipped") or []) | {key})
    return _next_waiting(t, "Okay, leaving that one for later.")


def on_add_task(t: Turn) -> dict[str, Any]:
    p = match_project(t.slot("project"), t.slot_id("project")) if t.slot("project") else None
    if t.slot("project") and not p:
        return _ask_project(t, "task_project", f"I don't know a project called {t.slot('project')}. Which one is it for?")
    p = p or (db.get_project(t.attrs["project_id"]) if t.attrs.get("project_id") else None)
    if not p:
        return _ask_project(t, "task_project", "Sure. Which project is it for?")
    t.mode("task_prompt", project_id=p["id"])
    return respond(t, f"A new task{_where(p['id']) or ' in the General workspace'}. What should it do?", "What should the task do?")


def on_push(t: Turn) -> dict[str, Any]:
    p = match_project(t.slot("project"), t.slot_id("project")) if t.slot("project") else _likely_project(t)
    if not p or db.is_general_project(p):
        return _ask_project(t, "push_project", "Which project should I push?")
    t.mode("confirm_push", project_id=p["id"])
    return respond(t, f"Push the current code in {p['name']} to GitHub? Claude Code will commit anything uncommitted first.",
                   "Should I push it? Yes or no.")


def on_discuss(t: Turn) -> dict[str, Any]:
    p = match_project(t.slot("project"), t.slot_id("project")) if t.slot("project") else None
    t.mode("discuss")
    t.attrs.pop("thread_id", None)
    t.attrs.pop("chat", None)
    if p:
        t.attrs["project_id"] = p["id"]
    about = f" for {p['name']}" if p and not db.is_general_project(p) else ""
    return respond(t, f"Sure, let's talk it through{about}. What's the task? When we're done, say 'queue it' and I'll hand it to Claude Code.",
                   "Go on, I'm listening.")


def on_queue_it(t: Turn) -> dict[str, Any]:
    if not (t.attrs.get("mode") in ("discuss", "confirm_discussion") or t.attrs.get("chat") == "discuss") or not t.attrs.get("thread_id"):
        return respond(t, "We haven't talked a task through yet. Say 'let's discuss a task' to start, or 'add a new task' to dictate one.")
    p = db.get_project(t.attrs["project_id"]) if t.attrs.get("project_id") else None
    if not p or db.is_general_project(p):
        return _ask_project(t, "discussion_project", "Which project should Claude Code do this in?", thread_id=t.attrs.get("thread_id"))
    t.mode("confirm_discussion", project_id=p["id"])
    return respond(t, f"Should I queue '{_thread_title(t)}' in {p['name']} for Claude Code, with everything we just discussed?",
                   "Should I queue it? Yes or no.")


def on_end_discussion(t: Turn) -> dict[str, Any]:
    t.mode(None)
    t.attrs.pop("chat", None)
    return respond(t, "Okay, we're done with that. The conversation is saved in The Den if you want to pick it up later.")


def on_continue(t: Turn) -> dict[str, Any]:
    tid = t.attrs.get("pending_task_id")
    if tid:
        return _deliver(t, tid)
    if t.attrs.get("mode") == "discuss":
        return respond(t, "I'm listening.", "Go on, I'm listening.")
    return respond(t, "There's nothing I'm still working on for you.")


def on_inbox(t: Turn) -> dict[str, Any]:
    news = _inbox(t)
    items = _waiting(t)
    lead = (" ".join(news) + " ") if news else ""
    if items:
        return _offer(t, items[0], lead=f"{lead}{len(items)} thing{'s' if len(items) != 1 else ''} waiting on you. First: ")
    return respond(t, lead or "Nothing new since you last asked. " + queue_summary())


def on_stop_task(t: Turn) -> dict[str, Any]:
    running = [x for x in (db.queue_status().get("running_all") or []) if x.get("lane") == "agent"]
    if not running:
        return respond(t, "Claude Code isn't running anything right now.")
    x = running[0]
    t.mode("confirm_stop")
    return respond(t, f"Stop '{_name(x)}'{_where(x['project_id'])}? It'll lose any unfinished step.", "Should I stop it? Yes or no.")


def _stop_running(t: Turn) -> dict[str, Any]:
    import worker
    t.mode(None)
    stopped = [x for x in (db.queue_status().get("running_all") or []) if x.get("lane") == "agent" and worker.stop_task(x["id"])]
    return respond(t, ("Stopped '" + _name(stopped[0]) + "'. You can say 'continue the previous task' to pick it up again.")
                   if stopped else "It had already finished.")


def on_help(t: Turn) -> dict[str, Any]:
    return respond(t, HELP, "What would you like?")


def on_cancel(t: Turn) -> dict[str, Any]:
    if t.attrs.get("mode") and t.attrs.get("mode") != "discuss":
        t.mode(None)
        return respond(t, "Okay, cancelled. Nothing was changed.")
    return on_stop(t)


def on_stop(t: Turn) -> dict[str, Any]:
    t.attrs.pop("mode", None)
    return respond(t, "Bye for now.", end=True)


def on_fallback(t: Turn) -> dict[str, Any]:
    return respond(t, "Sorry, I didn't catch that. Say help to hear what I can do.", "What would you like?")


# ------------------------------------------------------------------ free speech

def on_utterance(t: Turn, text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return on_fallback(t)
    mode = t.attrs.get("mode")
    if mode in ("approval", "pin", "plan") or (mode or "").startswith("confirm_"):
        if mode == "pin" and re.search(r"\d", _digits_in(text)):
            return _decide(t, True, _digits_in(text))
        if service._is_yes(text):
            return on_yes(t)
        if service._is_no(text):
            return on_no(t)
        if mode == "plan":
            row = db.get_routing(t.attrs.get("routing_id"))
            if row and row["status"] == "needs_input":
                p = match_project(text)
                if p:
                    try:
                        return _after_queue(t, service.run_plan(row["id"], project_id=p["id"]), lead="Okay. ")
                    except service.ServiceError as e:
                        return respond(t, f"I couldn't run it: {e.detail}")
        if mode in ("approval", "pin", "plan"):
            # Moved on without deciding - it stays waiting in The Den; carry on with whatever we were doing.
            t.mode("discuss" if t.attrs.get("chat") == "discuss" else None)
            mode = t.attrs.get("mode")
        elif mode == "confirm_task":
            t.attrs["draft"] = text
            return respond(t, f"I heard: '{_quote(text, 300)}'. Should I queue that?", "Should I queue it? Yes or no.")
        else:
            return respond(t, "Sorry - yes or no?", "Yes or no?")
    if mode in ("task_project", "push_project", "discussion_project"):
        p = match_project(text)
        if not p:
            return _ask_project(t, mode, f"I couldn't match {text} to a project. Which one?", thread_id=t.attrs.get("thread_id"))
        if mode == "task_project":
            t.mode("task_prompt", project_id=p["id"])
            return respond(t, f"In {p['name']}. What should it do?", "What should the task do?")
        if mode == "push_project":
            t.mode("confirm_push", project_id=p["id"])
            return respond(t, f"Push the current code in {p['name']} to GitHub?", "Yes or no?")
        t.mode("confirm_discussion", project_id=p["id"])
        return respond(t, f"Should I queue '{_thread_title(t)}' in {p['name']} for Claude Code?", "Yes or no?")
    if mode == "task_prompt":
        t.mode("confirm_task", project_id=t.attrs.get("project_id"), draft=text)
        return respond(t, f"I heard: '{_quote(text, 300)}'. Should I queue that{_where(t.attrs.get('project_id'))}?",
                       "Should I queue it? Yes or no.")
    if mode == "discuss":
        return _discuss(t, text)
    return _command(t, text)


def _digits_in(text: str) -> str:
    words = [NUMBER_WORDS.get(w, w) for w in re.findall(r"[a-z]+|\d+", text.lower())]
    return "".join(w for w in words if w.isdigit())


def _discuss(t: Turn, text: str) -> dict[str, Any]:
    """One turn of a conversation with the Voice Partner. The whole chat is one thread in The Den."""
    thread = t.attrs.get("thread_id")
    root = db.get_task(thread) if thread else None
    if root and root.get("agent_id") == "voice":
        out = _submit(t, text, thread_id=root["id"], agent="voice", review="skip")
    else:
        out = _submit(t, text, project_id=t.attrs.get("project_id"), agent="voice", review="skip")
    if not out or not out.get("task"):
        return respond(t, "", "Go on, I'm listening.")
    t.attrs["chat"] = "discuss"
    return _after_queue(t, out)


def _transcript(root_id: int) -> str:
    lines = []
    for x in db.get_thread(root_id):
        lines.append(f"Tushar: {x['prompt']}")
        if x.get("result_summary"):
            lines.append(f"Octopus: {x['result_summary']}")
    text = "\n".join(lines)
    return text if len(text) <= TRANSCRIPT_MAX_CHARS else "(earlier part cut)\n" + text[-TRANSCRIPT_MAX_CHARS:]


def _queue_discussion(t: Turn) -> dict[str, Any]:
    p = db.get_project(t.attrs.get("project_id")) if t.attrs.get("project_id") else None
    root = t.attrs.get("thread_id")
    if not p or not root:
        t.mode(None)
        return respond(t, "I lost track of that conversation - it's still in The Den under Messages.")
    t.attrs.pop("chat", None)
    return _queue_for_claude(t, DISCUSSION_TASK_PROMPT + _transcript(root), p, lead="Handing it over. ")


def _command(t: Turn, text: str) -> dict[str, Any]:
    """Anything said outside a flow goes to Octopus like a dashboard message. A follow-up ("and also add
    tests") continues the last chat from this Alexa session. Work that would start Claude Code is read back first."""
    thread = t.attrs.get("thread_id")
    root = db.get_task(thread) if thread else None
    if root and service.is_followup(text):
        latest = (db.get_thread(root["id"]) or [root])[-1]
        if latest.get("lane") == "agent":
            t.mode("confirm_followup", draft=text)
            return respond(t, f"Add that to '{_name(root)}'{_where(root['project_id'])}, for Claude Code? I heard: '{_quote(text, 200)}'.",
                           "Should I send it? Yes or no.")
        return _send_followup(t, text)
    out = _submit(t, text, review="force")
    if not out:
        return respond(t, "")
    status, plan = out["status"], out.get("plan") or {}
    if status == "done":
        result = out.get("result") or {}
        full = result.get("text") or ""
        if full and len(full) > len(result.get("spoken") or ""):
            t.card = ("Octopus", full)
        return respond(t, speakable(result.get("spoken") or full or "Done."))
    if status == "cancelled":
        return respond(t, out.get("spoken") or "Cancelled.")
    if status in ("proposed", "needs_input"):
        if status == "proposed" and not plan.get("show_plan") and plan.get("lane") != "agent":
            try:                                  # only held because we asked - a quick answer, run it
                return _after_queue(t, service.run_plan(out["routing_id"]))
            except service.ServiceError as e:
                return respond(t, f"Octopus couldn't take that: {speakable(e.detail, 200)}")
        t.mode("plan", routing_id=out["routing_id"])
        if status == "proposed" and plan.get("lane") == "agent" and not plan.get("show_plan"):
            t.attrs["mode"] = "confirm_command"
            return respond(t, f"That's a job for Claude Code{_where((plan.get('project') or {}).get('id'))}: '{_quote(text, 200)}'. Should I queue it?",
                           "Should I queue it? Yes or no.")
        return respond(t, out.get("spoken") or "Should I go ahead?", "Yes or no?")
    return _after_queue(t, out)


def _send_followup(t: Turn, text: str) -> dict[str, Any]:
    out = _submit(t, text, thread_id=t.attrs.get("thread_id"), review="skip")
    t.mode(None)
    if not out:
        return respond(t, "")
    return _after_queue(t, out, lead="Added to the conversation. " if (out.get("task") or {}).get("lane") == "agent" else "")


# ------------------------------------------------------------------ entry

def _record_contact(env: dict[str, Any]) -> None:
    system = (env.get("context") or {}).get("System") or {}
    user = ((system.get("user") or {}).get("userId")) or (((env.get("session") or {}).get("user") or {}).get("userId"))
    locale = (env.get("request") or {}).get("locale")

    def fn(st: dict[str, Any]) -> None:
        if user and not st.get("user_id"):       # pairing: the first account to use it (allowed_user has already checked a pinned one)
            st["user_id"], st["linked_at"] = user, state.now_iso()
        if system.get("apiEndpoint"):
            st["api_endpoint"] = system["apiEndpoint"]
        if locale:
            st["locale"] = locale
        if (env.get("request") or {}).get("type") != "SessionEndedRequest":
            st["last_request_at"] = state.now_iso()
    state.update(fn)


def user_of(env: dict[str, Any]) -> str:
    system = (env.get("context") or {}).get("System") or {}
    return str(((system.get("user") or {}).get("userId")) or (((env.get("session") or {}).get("user") or {}).get("userId"))
               or ((env.get("request") or {}).get("body") or {}).get("userId") or "")


def _skill_event(env: dict[str, Any]) -> None:
    req = env.get("request") or {}
    body = req.get("body") or {}
    kind = req.get("type", "")
    if kind == "AlexaSkillEvent.ProactiveSubscriptionChanged":
        subscribed = bool(body.get("subscriptions"))
    elif kind in ("AlexaSkillEvent.SkillPermissionAccepted", "AlexaSkillEvent.SkillPermissionChanged"):
        scopes = [p.get("scope") for p in body.get("acceptedPermissions") or []]
        subscribed = "alexa::devices:all:notifications:write" in scopes
    elif kind == "AlexaSkillEvent.SkillDisabled":
        subscribed = False
    else:
        return
    state.update(lambda st: st.update(subscribed=subscribed, subscription_changed_at=state.now_iso()))


INTENTS: dict[str, Callable[[Turn], dict[str, Any]]] = {
    "QueueIntent": on_queue,
    "WorkingOnIntent": on_working,
    "ApprovalsIntent": on_approvals,
    "ApproveIntent": lambda t: on_yes(t, t.slot("code")) if t.attrs.get("mode") in ("approval", "pin") else on_approvals(t),
    "DenyIntent": lambda t: on_no(t) if t.attrs.get("mode") in ("approval", "pin", "plan") else on_approvals(t),
    "PinIntent": lambda t: _decide(t, True, t.slot("code")) if t.attrs.get("mode") in ("approval", "pin") else on_fallback(t),
    "SkipIntent": on_skip,
    "AddTaskIntent": on_add_task,
    "PushIntent": on_push,
    "DiscussIntent": on_discuss,
    "QueueItIntent": on_queue_it,
    "EndDiscussionIntent": on_end_discussion,
    "ContinueIntent": on_continue,
    "InboxIntent": on_inbox,
    "StopTaskIntent": on_stop_task,
    "ChatIntent": lambda t: on_utterance(t, t.slot("utterance")),
    "AMAZON.YesIntent": on_yes,
    "AMAZON.NoIntent": on_no,
    "AMAZON.HelpIntent": on_help,
    "AMAZON.StopIntent": on_stop,
    "AMAZON.CancelIntent": on_cancel,
    "AMAZON.NavigateHomeIntent": on_cancel,
    "AMAZON.FallbackIntent": on_fallback,
}
# Intents that speak to the flow in progress - everything else first says any answer that became
# ready and mentions a new approval.
_NO_INTERJECTION = {"ContinueIntent", "ApprovalsIntent", "ApproveIntent", "DenyIntent", "PinIntent", "SkipIntent",
                    "AMAZON.YesIntent", "AMAZON.NoIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"}


def handle(env: dict[str, Any]) -> dict[str, Any]:
    """An Alexa request that has already passed alexa.verify -> the response to send back."""
    kind = (env.get("request") or {}).get("type", "")
    if kind.startswith("AlexaSkillEvent."):
        if state.allowed_user(user_of(env)):
            _skill_event(env)
        return empty()
    if not state.allowed_user(user_of(env)):
        return {"version": "1.0", "response": {"outputSpeech": {"type": "PlainText", "text": "This Octopus belongs to a different Amazon account."},
                                               "shouldEndSession": True}}
    _record_contact(env)
    if kind == "SessionEndedRequest":
        return empty()
    t = Turn(env)
    if kind == "LaunchRequest":
        return launch(t)
    if kind != "IntentRequest":
        return empty()
    if (env.get("session") or {}).get("new"):
        # A one-shot ("Alexa, ask my octopus to ...") still continues a recent conversation.
        conv = state.get().get("conversation") or {}
        if conv and time.time() - float(conv.get("at") or 0) < RESUME_WITHIN_SECONDS:
            t.attrs.update({k: conv[k] for k in ("thread_id", "project_id", "chat") if conv.get(k)})
            if conv.get("mode") == "discuss" and t.intent() == "ChatIntent":
                t.attrs["mode"] = "discuss"
    name = t.intent()
    if name not in _NO_INTERJECTION:
        _ready_earlier_answer(t)
        _heads_up(t)
    return INTENTS.get(name, on_fallback)(t)
