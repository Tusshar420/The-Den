"""
Capability-based permissions: what a tool call is allowed to do, decided
per capability rather than per tool.

Every tool action declares the capabilities it needs (sending an email
needs EMAIL_SEND; a meeting with attendees needs CALENDAR_CREATE and
CALENDAR_INVITE). Each capability has a policy:

    ALLOW  run it
    ASK    park it as an approval until the human says yes
    DENY   never run it

A call that needs several capabilities gets the strictest of their
policies. Defaults are below; overrides are stored in the settings table
and edited from the dashboard (PATCH /assistant/policies).
"""

from typing import Any, Iterable, Optional

import db

ALLOW, ASK, DENY = "allow", "ask", "deny"
POLICIES = (ALLOW, ASK, DENY)
_STRICTNESS = {ALLOW: 0, ASK: 1, DENY: 2}

# name -> (label, default policy, one-line description shown in Settings)
CAPABILITIES: dict[str, dict[str, Any]] = {
    "EMAIL_READ": {"label": "Read and search email", "default": ALLOW,
                   "help": "Search the mailbox and open messages."},
    "EMAIL_DRAFT": {"label": "Create email drafts", "default": ALLOW,
                    "help": "Save a draft. Nothing is sent."},
    "EMAIL_SEND": {"label": "Send, reply or forward email", "default": ASK,
                   "help": "Anything that leaves your mailbox."},
    "EMAIL_DELETE": {"label": "Delete email", "default": DENY,
                     "help": "Not implemented - Octopus is not even given the permission to do it."},
    "CALENDAR_READ": {"label": "Read calendar and availability", "default": ALLOW,
                      "help": "List events and check free/busy."},
    "CALENDAR_CREATE": {"label": "Create personal events", "default": ALLOW,
                        "help": "Add an event to your own calendar with nobody invited."},
    "CALENDAR_UPDATE": {"label": "Change existing events", "default": ASK,
                        "help": "Move, rename or edit an event."},
    "CALENDAR_DELETE": {"label": "Cancel events", "default": ASK,
                        "help": "Delete an event."},
    "CALENDAR_INVITE": {"label": "Send invitations", "default": ASK,
                        "help": "Any event change that emails other people."},
    "REMINDER_READ": {"label": "List reminders", "default": ALLOW, "help": "See your pending reminders."},
    "REMINDER_WRITE": {"label": "Set and cancel reminders", "default": ALLOW,
                       "help": "Reminders stay on this Mac and ping your phone."},
    "AGENT_CODING": {"label": "Hand work to the coding agent", "default": ASK,
                     "help": "Queue a Claude Code task in one of your projects."},
    "AI_MODEL_USE": {"label": "Ask the app's AI models", "default": ALLOW,
                     "help": "Send a prompt to a writing, research or general model you have enabled (OpenAI, Gemini, "
                             "Claude). The prompt leaves this Mac for that provider and may cost a little."},

    # ---- coding agent: what Claude Code does inside a project (group "coding").
    # The per-command Bash approvals still decide each command (approval_server.py);
    # these say what kind of thing a command is, so the plan card and the approval
    # card can explain it and one setting covers the whole family.
    "FILES_READ": {"label": "Read files and analyze code", "default": ALLOW, "group": "coding",
                   "help": "Look through a project's files. Changes nothing."},
    "FILES_WRITE": {"label": "Edit files", "default": ALLOW, "group": "coding",
                    "help": "Create and change files inside a project."},
    "TESTS_RUN": {"label": "Run tests, linters and builds", "default": ALLOW, "group": "coding",
                  "help": "Run the project's own test, lint and build commands."},
    "GIT_READ": {"label": "Read git history", "default": ALLOW, "group": "coding",
                 "help": "git status, diff, log, show, branch."},
    "GIT_WRITE": {"label": "Commit and branch", "default": ASK, "group": "coding",
                  "help": "Commit, create branches, merge, rebase, stash."},
    "GIT_PUSH": {"label": "Push to a remote", "default": ASK, "group": "coding",
                 "help": "Send commits to GitHub or any other remote."},
    "PACKAGE_INSTALL": {"label": "Install or update packages", "default": ASK, "group": "coding",
                        "help": "npm/pip/brew install and friends."},
    "NETWORK_ACCESS": {"label": "Commands that use the network", "default": ASK, "group": "coding",
                       "help": "curl, wget, ssh, scp and similar."},
    "TERMINAL_RUN": {"label": "Other terminal commands", "default": ASK, "group": "coding",
                     "help": "Any shell command not covered above."},
    "DEPLOY": {"label": "Deploy or publish", "default": ASK, "group": "coding",
               "help": "Deploys, releases and publishes - stays behind approval in every autonomy mode."},
    "DESTRUCTIVE": {"label": "Delete files or rewrite history", "default": ASK, "group": "coding",
                    "help": "rm, git reset --hard, force-push - stays behind approval in every autonomy mode."},
    "PRODUCTION": {"label": "Touch production", "default": ASK, "group": "coding",
                   "help": "Anything that mentions production - stays behind approval in every autonomy mode."},
    "SYSTEM_ADMIN": {"label": "Admin rights and system changes", "default": ASK, "group": "coding",
                     "help": "sudo, launchctl, changing system settings - stays behind approval in every autonomy mode."},

    # ---- web, media, and Octopus's own housekeeping
    "WEB_ACCESS": {"label": "Search the web and read pages", "default": ALLOW, "group": "web",
                   "help": "Look things up online. Nothing is sent to any site but the query."},
    "IMAGE_GENERATE": {"label": "Generate images", "default": ALLOW, "group": "media",
                       "help": "Ask an image model for a picture (a small per-image cost)."},
    "PROJECT_READ": {"label": "Look at projects, tasks and the queue", "default": ALLOW, "group": "workspace",
                     "help": "Status, project lists, to-dos."},
    "TASK_CONTROL": {"label": "Stop, cancel and resume tasks", "default": ALLOW, "group": "workspace",
                     "help": "Stop what is running, cancel something queued, pick a task back up."},
    "TODO_WRITE": {"label": "Add to a project's backlog", "default": ALLOW, "group": "workspace",
                   "help": "Add to-do items. Nothing runs until you queue them."},
}

# Extra facts about each capability, read by the plan builder and the autonomy layer:
#   risk       low | medium | high   (shown on plan and approval cards)
#   protected  stays behind approval in every autonomy mode unless you set it to
#              allow yourself - "dangerous operations remain behind approval"
#   readonly   still fine in Assist mode (which only investigates and proposes)
#   group      which settings list it belongs to
_META: dict[str, dict[str, Any]] = {
    "EMAIL_READ": {"risk": "low", "readonly": True}, "EMAIL_DRAFT": {"risk": "low"},
    "EMAIL_SEND": {"risk": "high", "protected": True}, "EMAIL_DELETE": {"risk": "high", "protected": True},
    "CALENDAR_READ": {"risk": "low", "readonly": True}, "CALENDAR_CREATE": {"risk": "low"},
    "CALENDAR_UPDATE": {"risk": "medium"}, "CALENDAR_DELETE": {"risk": "medium"},
    "CALENDAR_INVITE": {"risk": "medium", "protected": True},
    "REMINDER_READ": {"risk": "low", "readonly": True}, "REMINDER_WRITE": {"risk": "low"},
    "AGENT_CODING": {"risk": "medium"}, "AI_MODEL_USE": {"risk": "low", "readonly": True},
    "FILES_READ": {"risk": "low", "readonly": True}, "FILES_WRITE": {"risk": "low"}, "TESTS_RUN": {"risk": "low"},
    "GIT_READ": {"risk": "low", "readonly": True}, "GIT_WRITE": {"risk": "medium"},
    "GIT_PUSH": {"risk": "high", "protected": True}, "PACKAGE_INSTALL": {"risk": "medium"},
    "NETWORK_ACCESS": {"risk": "medium"}, "TERMINAL_RUN": {"risk": "medium"},
    "DEPLOY": {"risk": "high", "protected": True}, "DESTRUCTIVE": {"risk": "high", "protected": True},
    "PRODUCTION": {"risk": "high", "protected": True}, "SYSTEM_ADMIN": {"risk": "high", "protected": True},
    "WEB_ACCESS": {"risk": "low", "readonly": True}, "IMAGE_GENERATE": {"risk": "low"},
    "PROJECT_READ": {"risk": "low", "readonly": True}, "TASK_CONTROL": {"risk": "medium"},
    "TODO_WRITE": {"risk": "low"},
}
for _name, _c in CAPABILITIES.items():
    _c.setdefault("group", "assistant")
    _c.update({"risk": "low", "protected": False, "readonly": False, **_META.get(_name, {})})

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Capabilities that act on other people or change something that is hard to
# take back. A run that has read untrusted text (an email body, an invite
# description, a task's output, a model's answer) never gets these on ALLOW -
# see actions.invoke. Cancelling a task and adding to a backlog count: the backlog
# is what Claude Code is later told to work through.
OUTBOUND = frozenset({"EMAIL_SEND", "EMAIL_DELETE", "CALENDAR_INVITE", "CALENDAR_UPDATE",
                      "CALENDAR_DELETE", "AGENT_CODING", "TASK_CONTROL", "TODO_WRITE"})

SETTING_KEY = "assistant_policies"


def _overrides() -> dict[str, str]:
    stored = db.get_setting(SETTING_KEY, {}) or {}
    return {k: v for k, v in stored.items() if k in CAPABILITIES and v in POLICIES}


def policy_for(capability: str) -> str:
    if capability not in CAPABILITIES:
        return DENY  # an undeclared capability is never allowed
    return _overrides().get(capability, CAPABILITIES[capability]["default"])


# ------------------------------------------------------------- autonomy
# How much Octopus may do without asking (settings key "autonomy"):
#   assist      investigates and proposes; anything that changes something asks
#   execute     normal actions run, risky ones ask - the policies below, as configured
#   autonomous  also runs what would ask by default, except protected capabilities
# A capability you set to DENY is denied in every mode; one you set to ALLOW yourself
# is honoured in execute and autonomous ("unless I explicitly configure otherwise").

AUTONOMY_LEVELS = ("assist", "execute", "autonomous")
DEFAULT_AUTONOMY = "execute"


def autonomy() -> str:
    level = db.get_setting("autonomy")
    return level if level in AUTONOMY_LEVELS else DEFAULT_AUTONOMY


def effective_policy_for(capability: str, mode: Optional[str] = None) -> str:
    """policy_for(), after the autonomy level has had its say."""
    configured = policy_for(capability)
    if configured == DENY or capability not in CAPABILITIES:
        return DENY
    meta, mode = CAPABILITIES[capability], mode or autonomy()
    if mode == "assist":
        return configured if meta["readonly"] else ASK
    if mode == "autonomous" and configured == ASK and capability not in _overrides() and not meta["protected"]:
        return ALLOW
    return configured


def all_policies(group: Optional[str] = None) -> list[dict[str, Any]]:
    over, mode = _overrides(), autonomy()
    return [{"capability": name, "label": c["label"], "help": c["help"], "default": c["default"],
             "policy": over.get(name, c["default"]), "effective": effective_policy_for(name, mode),
             "group": c["group"], "risk": c["risk"], "protected": c["protected"]}
            for name, c in CAPABILITIES.items() if group is None or c["group"] == group]


def set_policy(capability: str, policy: str) -> None:
    if capability not in CAPABILITIES:
        raise ValueError(f"Unknown capability '{capability}'")
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
    over = _overrides()
    over[capability] = policy
    db.set_setting(SETTING_KEY, over)


def strictest(policies: Iterable[str]) -> str:
    return max(policies, key=lambda p: _STRICTNESS[p], default=ALLOW)


def resolve(capabilities: Iterable[str], mode: Optional[str] = None) -> str:
    """The policy for a call that needs all of these capabilities, at the
    current autonomy level (or `mode`)."""
    return strictest(effective_policy_for(c, mode) for c in capabilities)


def risk_of(capabilities: Iterable[str]) -> str:
    """The highest risk among these capabilities: low | medium | high."""
    return max((CAPABILITIES[c]["risk"] for c in capabilities if c in CAPABILITIES), key=RISK_ORDER.get, default="low")


def is_outbound(capabilities: Iterable[str]) -> bool:
    return bool(OUTBOUND.intersection(capabilities))


def effective(capabilities: Iterable[str], tainted: bool = False) -> str:
    """resolve(), except that after untrusted text has been read in this run,
    an outbound action can no longer run without asking, whatever the policy
    says. Prompt-injected text in an email must not be able to send mail."""
    caps = list(capabilities)
    policy = resolve(caps)
    if tainted and policy == ALLOW and is_outbound(caps):
        return ASK
    return policy


def describe(policy: Optional[str]) -> str:
    return {ALLOW: "runs automatically", ASK: "needs your approval", DENY: "is blocked"}.get(policy or "", "unknown")
