"""
The personal-assistant intent router.

classify() answers, for a piece of text and without calling any model:

    is this a personal-assistant request at all,
    which intent, which tool,
    which capabilities it needs and what the current policy says about them,
    whether approval will be required,
    whether an AI model is needed (or plain tool calls will do),
    whether it can run automatically (no human OK needed).

Rules, not a model, so it is instant and free. It errs on the side of *not*
claiming a request: a coding request that merely mentions email ("add email
validation to the signup form") must reach the coding path, so a mail or
calendar request has to look like an action on the mailbox or calendar
(verb + object), not just contain the word. router.py runs this first and
falls back to its usual classifier for anything not matched here.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from assistant import capabilities as cap
from assistant import fastpath

# name -> (tool, capabilities the intent can need in the worst case, plain-English summary)
INTENTS: dict[str, tuple[str, list[str], str]] = {
    "calendar.read": ("calendar", ["CALENDAR_READ"], "Read your calendar"),
    "calendar.availability": ("calendar", ["CALENDAR_READ"], "Check whether you're free"),
    "calendar.find_slot": ("calendar", ["CALENDAR_READ"], "Find a free slot"),
    "calendar.create_event": ("calendar", ["CALENDAR_CREATE"], "Add an event to your calendar"),
    "calendar.create_meeting": ("calendar", ["CALENDAR_CREATE", "CALENDAR_INVITE"], "Schedule a meeting and invite people"),
    "calendar.modify": ("calendar", ["CALENDAR_UPDATE", "CALENDAR_INVITE"], "Change an event"),
    "calendar.cancel": ("calendar", ["CALENDAR_DELETE", "CALENDAR_INVITE"], "Cancel an event"),
    "email.search": ("email", ["EMAIL_READ"], "Search your email"),
    "email.read": ("email", ["EMAIL_READ"], "Read an email"),
    "email.analyze": ("email", ["EMAIL_READ"], "Read an email and work out what it needs"),
    "email.draft": ("email", ["EMAIL_READ", "EMAIL_DRAFT"], "Draft an email"),
    "email.send": ("email", ["EMAIL_READ", "EMAIL_SEND"], "Send an email"),
    "email.reply": ("email", ["EMAIL_READ", "EMAIL_SEND"], "Reply to an email"),
    "email.forward": ("email", ["EMAIL_READ", "EMAIL_SEND"], "Forward an email"),
    "reminder.create": ("reminders", ["REMINDER_WRITE"], "Set a reminder"),
    "reminder.list": ("reminders", ["REMINDER_READ"], "List your reminders"),
    "workspace.query": ("workspace", ["PROJECT_READ"], "Look at Octopus's queue and tasks"),
    "workspace.cancel": ("workspace", ["TASK_CONTROL"], "Cancel or stop a task"),
    "multi": ("multi", [], "Use several tools together"),
}

# Intents that always need language understanding, whatever the wording.
ALWAYS_AI = {"calendar.find_slot", "calendar.create_meeting", "calendar.modify", "calendar.cancel", "email.analyze",
             "email.draft", "email.send", "email.reply", "email.forward", "workspace.query", "workspace.cancel", "multi"}


@dataclass
class Intent:
    matched: bool = False
    intent: str = ""
    tool: str = ""
    summary: str = ""
    capabilities: list[str] = field(default_factory=list)
    policy: str = cap.ALLOW
    requires_approval: bool = False
    needs_ai: bool = False
    auto_executable: bool = False
    confidence: str = "none"
    reason: str = ""
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def route_note(self) -> str:
        how = "needs a model to interpret" if self.needs_ai else "plain tool calls, no model"
        gate = {"ask": "approval before anything leaves your accounts", "deny": "blocked by your permission settings"}.get(
            self.policy, "runs automatically")
        return f"assistant: {self.intent} → {' + '.join(self.tools or [self.tool])} · {how} · {gate}"


# ------------------------------------------------------------------ patterns

_NOT_CODE = (r"(?!\s+(?:validation|validator|validate|regex|addresses|address|template|service|field|verification|module|"
             r"function|sender|parser|library|client|server|config|column|handler|queue|worker|endpoint|api|bounce|deliverability))")
MAILBOX = rf"(?:e-?mails?{_NOT_CODE}|mails?\b|inbox|gmail)"
CAL_NOUN = r"(?:meetings?|events?|appointments?|calls?|stand-?ups?|syncs?|1:1s?|catch-?ups?|reviews?|interviews?|demos?)"
DAYS = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?"
TIME_CUE = re.compile(rf"\b(?:today|tomorrow|tonight|next|this (?:morning|afternoon|evening|week)|{DAYS}|\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)|\d{{1,2}}:\d{{2}}|noon)\b")
POSSESSIVE = re.compile(r"\bmy\b")
CODING_VERB = re.compile(r"\b(?:fix|debug|implement|refactor|deploy|patch|resolve|investigate|reproduce|diagnose)\b")
MAIL_REF = re.compile(rf"\b(?:this|that|the|his|her|their|[a-z]+'s)\s+(?:e-?mail|mail|thread|invite)\b|"
                      rf"\b(?:mentioned|described|reported|listed)\s+in\b|\bfrom\s+(?:the|this|that)\s+e-?mail\b|"
                      rf"\bin\s+(?:the|this|that)\s+e-?mail\b")


# Octopus's own queue. "the queue worker" or "fix the task queue" is code, not this.
_QUEUE_CODE = (r"(?!\s+(?:worker|consumer|implementation|module|code|library|table|schema|service|class|function|"
               r"data structure|depth|size|limit|config))")
_QUEUE = rf"(?:the|my|octopus'?s?) queue\b{_QUEUE_CODE}"
_TASKS = r"(?:my (?:recent |failed |running |pending |queued |stuck |latest |current )*tasks?\b|(?:failed|stuck|queued|pending|running|cancelled) tasks?\b|tasks? (?:have |has |are |is )?(?:failed|failing|stuck|queued|waiting|running)\b|task\s*#?\d+\b)"
_SPEND = r"(?:(?:ai|model) (?:spend|usage|costs?)\b|spent on (?:ai|models?)\b|(?:pending|waiting) approvals?\b|my approvals?\b)"
WORKSPACE_CUE = rf"\b(?:{_QUEUE}|backlog\b|to-?do list\b|my tasks?\b|task\s*#?\d+\b)"
# A request that names a model for the work ("draft a reply using Gemini").
MODEL_CUE = r"\b(?:using|with|via|through|ask|use)\s+(?:gpt|gemini|claude|chatgpt)"


def _has(pattern: str, t: str) -> bool:
    return re.search(pattern, t) is not None


def _reminder(t: str) -> Optional[tuple[str, str, str]]:
    if _has(r"\bremind me\b|\b(?:set|create|add|make)\s+(?:a |an )?reminder\b", t):
        return "reminder.create", "high", "asks to be reminded"
    if _has(r"\b(?:what|which|show|list|check|see|do i have any|any|my)\b.*\breminders?\b", t):
        return "reminder.list", "high", "asks about reminders"
    return None


def _calendar(t: str, in_thread: bool) -> Optional[tuple[str, str, str]]:
    anchored = bool(POSSESSIVE.search(t) or TIME_CUE.search(t))
    if _has(rf"\b(?:cancel|delete|remove|drop|call off)\b.*\b{CAL_NOUN}\b", t) and anchored:
        return "calendar.cancel", "high", "cancel + a calendar item"
    if _has(rf"\b(?:move|reschedule|push|shift|postpone|delay|change|bring forward)\b.*\b{CAL_NOUN}\b", t) and anchored:
        return "calendar.modify", "high", "move/change + a calendar item"
    if in_thread and _has(r"^\s*(?:please\s+)?(?:move|push|shift|reschedule|postpone)\s+(?:it|that|this|them)\b", t):
        return "calendar.modify", "medium", "follow-up about the event just discussed"
    if _has(r"\b(?:find|suggest|propose|look for|get)\b.*\b(?:free|open|available)\b.*\b(?:slots?|times?|windows?|gaps?)\b|"
            r"\bwhen (?:are|is|can)\b.*\b(?:free|available)\b|\bfind (?:a |an )?(?:\d+[- ]?(?:minute|min|hour|hr)|time|slot)\b", t):
        return "calendar.find_slot", "high", "asks for a free slot"
    if _has(rf"\b(?:schedule|set up|setup|book|arrange|organi[sz]e|create|add|put|plan)\b.*\b{CAL_NOUN}\b", t) \
            and not _has(rf"\b(?:show|check|list|what)\b", t):
        if _has(r"\bwith\s+\w|\binvite\b", t):
            return "calendar.create_meeting", "high", "schedule a meeting with someone"
        return "calendar.create_event", "high", "add an event"
    if _has(r"\bblock\b.*(?:\d|today|tomorrow|morning|afternoon|evening|next|calendar)|"
            r"\b(?:add|put)\b.*\b(?:to|on|in) my calendar\b|\bcreate an? event\b", t):
        return "calendar.create_event", "high", "block time / add to calendar"
    if _has(r"\b(?:am i|are we)\s+(?:free|busy|available|around|booked|double[- ]booked)\b|"
            r"\bdo i have (?:anything|something|any (?:meetings?|events?|plans?|conflicts?))\b|"
            r"\bis there anything (?:on|at|scheduled|in|going)\b|\bany (?:meetings?|events?|conflicts?)\b.*\b(?:at|between|tomorrow|today|on)\b", t):
        return "calendar.availability", "high", "asks whether time is free"
    if _has(r"\b(?:what'?s|what is|whats|what do i have|what have i got|show|check|list|read|see|view|pull up|tell me)\b.*"
            r"\b(?:calendar|schedule|agenda|meetings|events|appointments)\b|"
            r"\b(?:my )?(?:calendar|agenda)\s+(?:for|on|today|tomorrow|this|next)\b|\bwhat'?s on (?:today|tomorrow)\b|"
            r"\bwhat do i have (?:today|tomorrow|on|this|next)\b", t):
        return "calendar.read", "high", "asks what is on the calendar"
    return None


def _email(t: str, in_thread: bool) -> Optional[tuple[str, str, str]]:
    if _has(rf"\b(?:draft|compose|write|prepare|put together)\b.*\b(?:{MAILBOX}|reply|response)\b", t):
        return "email.draft", "high", "draft/write an email"
    if _has(rf"\b(?:summari[sz]e|analy[sz]e|explain|go through|what does|what do i need to do)\b.*\b(?:{MAILBOX}|thread)\b|"
            rf"\b(?:read|open)\b.*\b(?:{MAILBOX})\b.*\b(?:tell me|and tell|what i need|summari[sz]e|explain)\b|"
            rf"\btell me what\b.*\b(?:{MAILBOX})\b", t):
        return "email.analyze", "high", "asks what an email says / needs"
    if _has(r"\b(?:reply|respond|write back|answer back)\s+(?:to|back)\b|\breply\b.*\bsaying\b", t) or \
            (in_thread and _has(r"^\s*(?:please\s+)?reply\b", t)):
        return "email.reply", "high", "reply to someone"
    if _has(rf"\bforward\b.*\b(?:{MAILBOX}|it|this|that|thread|message)\b", t):
        return "email.forward", "high", "forward an email"
    if _has(rf"^\s*(?:please\s+|can you\s+|could you\s+)?(?:send|shoot|fire off)\b.*\b(?:{MAILBOX}|message|note|draft)\b|"
            rf"\bsend\s+(?:him|her|them|\w+)\s+(?:an?|the)\s+(?:{MAILBOX}|message|note|draft)\b|"
            rf"^\s*(?:please\s+|can you\s+|could you\s+)?(?:e-?mail|mail)\s+(?!validation|address)\w", t):
        return "email.send", "high", "send an email"
    if _has(rf"\b(?:read|open|show me|display)\b.*\b(?:{MAILBOX})\b|\b(?:read|open)\b\s+\w+'s\s+(?:latest|last|recent|newest)\b.*\b(?:{MAILBOX})\b", t):
        return "email.read", "high", "read an email"
    if _has(rf"\b(?:find|search|look for|look up|check|get|fetch|any|list|show)\b.*\b(?:{MAILBOX})\b|"
            rf"\b(?:latest|last|newest|most recent|recent|unread|new)\s+(?:{MAILBOX})\b|\b\w+'s\s+(?:latest|last|recent)\s+(?:{MAILBOX})\b", t):
        return "email.search", "high", "search the mailbox"
    return None


def _workspace(t: str) -> Optional[tuple[str, str, str]]:
    if CODING_VERB.search(t):
        return None
    if _has(r"\b(?:cancel|stop|abort|kill)\b.*\btask\s*#?\d+\b", t):
        return "workspace.cancel", "high", "cancel a task by number"
    if _has(rf"\b(?:what|which|show|list|how many|how much|any|check|see|tell me|why|is there)\b.*\b(?:{_QUEUE}|{_TASKS}|{_SPEND})", t):
        return "workspace.query", "high", "asks about the queue, tasks or approvals"
    return None


def _coding_handoff(t: str) -> bool:
    return bool(CODING_VERB.search(t) and MAIL_REF.search(t) and _has(rf"\b{MAILBOX}\b|\bmentioned in\b", t))


def _build(intent: str, confidence: str, reason: str, text: str, tools: Optional[list[str]] = None,
           caps: Optional[list[str]] = None) -> Intent:
    tool, base_caps, summary = INTENTS[intent]
    caps = caps if caps is not None else list(base_caps)
    policy = cap.resolve(caps)
    needs_ai = intent in ALWAYS_AI or fastpath.plan(intent, text) is None
    return Intent(matched=True, intent=intent, tool=tool, summary=summary, capabilities=caps, policy=policy,
                  requires_approval=policy == cap.ASK, needs_ai=needs_ai,
                  auto_executable=policy == cap.ALLOW, confidence=confidence, reason=reason,
                  tools=tools or [tool])


def classify(text: str, in_thread: bool = False) -> Intent:
    """See the module docstring. `in_thread` says the request continues an
    assistant chat, which makes short follow-ups ("move it to 4") claimable."""
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t:
        return Intent()

    found = {"reminders": _reminder(t), "calendar": _calendar(t, in_thread), "email": _email(t, in_thread),
             "workspace": _workspace(t)}
    hits = {k: v for k, v in found.items() if v}

    if _coding_handoff(t) and (hits.get("email") or _has(rf"\b{MAILBOX}\b|\bmentioned in\b", t)):
        caps = ["EMAIL_READ", "AGENT_CODING"]
        return _build("multi", "medium", "email content + a coding task for the agent", text, ["email", "agent"], caps)

    # An email / calendar / reminder request that also involves the queue or a named model
    # is a job for the planner with those tools too. On their own these cues claim nothing.
    extra: dict[str, list[str]] = {}
    if hits:
        if "workspace" not in hits and _has(WORKSPACE_CUE, t):
            extra["workspace"] = ["PROJECT_READ"] + (["TODO_WRITE"] if _has(r"\bbacklog\b|\bto-?do", t) else [])
        if _has(MODEL_CUE, t):
            extra["ai"] = ["AI_MODEL_USE"]

    if len(hits) + len(extra) >= 2:
        caps: list[str] = []
        for name, _, _ in hits.values():
            caps += [c for c in INTENTS[name][1] if c not in caps]
        for extra_caps in extra.values():
            caps += [c for c in extra_caps if c not in caps]
        return _build("multi", "medium", " + ".join([v[0] for v in hits.values()] + list(extra)), text,
                      list(hits) + list(extra), caps)

    if hits:
        (name, confidence, reason), = hits.values()
        return _build(name, confidence, reason, text)
    return Intent()
