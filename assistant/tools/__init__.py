"""
Tools: provider-independent capabilities the assistant can use (email,
calendar, reminders, the coding agent, Octopus's own queue and projects, and
the app's AI models). A tool is not itself an AI model - it is code that does
one kind of thing, deterministically (ai.py calls a model, but only with a
prompt the planner wrote, through the same gate as everything else).

Every action runs in two steps so the permission check can see what is
really about to happen:

    prepare(args)  read-only. Validates and resolves the arguments (fetches
                   the event being moved, the message being replied to...),
                   says which capabilities the call needs, and builds the
                   preview shown on an approval card.
    execute(args)  does it. Receives the resolved args prepare() returned,
                   which are exactly what an approval stores and later runs.

To add a tool: subclass Tool, describe each action in `actions`, implement
prepare_<action> and run_<action>, call register(). See email.py.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Optional


class ToolError(Exception):
    """A failure with a message fit to show the user."""


@dataclass
class ActionSpec:
    name: str
    description: str
    # arg name -> description. A trailing "?" on the name marks it optional.
    args: dict[str, str] = field(default_factory=dict)
    # True when the output contains text written by other people (email
    # bodies, invite descriptions) - see capabilities.effective.
    reads_untrusted: bool = False

    def arg_names(self) -> tuple[list[str], list[str]]:
        required = [a for a in self.args if not a.endswith("?")]
        optional = [a[:-1] for a in self.args if a.endswith("?")]
        return required, optional


@dataclass
class Prepared:
    args: dict[str, Any]
    capabilities: list[str]
    # Short label for the activity log: a recipient, an event title, a query.
    target: str
    # One line for step logs and push notifications.
    describe: str
    # Approval card content; see preview().
    preview: Optional[dict[str, Any]] = None


def preview(kind: str, title: str, action: str, fields: list[tuple[str, str]], approve: str,
            warnings: Optional[list[str]] = None, blocks: tuple = (), deny: str = "Cancel") -> dict[str, Any]:
    """The shape every approval card is rendered from. `blocks` names the
    fields that are long text (a message body) rather than one line."""
    return {"kind": kind, "title": title, "action": action, "approve": approve, "deny": deny,
            "fields": [{"label": k, "value": v, "block": k in blocks} for k, v in fields if v not in (None, "")],
            "warnings": warnings or []}


@dataclass
class ToolContext:
    """What a tool may touch. Providers are factories so a tool that never
    needs the mailbox never requires an email account to be connected."""
    email: Callable[[], Any]
    calendar: Callable[[], Any]
    now: Callable[[], datetime]
    task: Optional[dict[str, Any]] = None
    memory: dict[str, Any] = field(default_factory=dict)
    # Model calls a tool made on the run's behalf (tools/ai.py), so the task
    # can report what they cost next to the planner's own tokens.
    usage: dict[str, Any] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cost_usd": None,
                                                           "models": []})

    def add_usage(self, model_id: str, input_tokens: Optional[int], output_tokens: Optional[int],
                  cost_usd: Optional[float]) -> None:
        u = self.usage
        u["input_tokens"] += input_tokens or 0
        u["output_tokens"] += output_tokens or 0
        if cost_usd is not None:
            u["cost_usd"] = (u["cost_usd"] or 0.0) + cost_usd
        if model_id not in u["models"]:
            u["models"].append(model_id)


class Tool:
    id = ""
    label = ""
    actions: dict[str, ActionSpec] = {}
    # "email" / "calendar" for a tool that works through connected accounts: every action
    # then also takes an optional `account` (which one to use - see _scope).
    account_service: Optional[str] = None

    def check_args(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = self.actions.get(action)
        if not spec:
            raise ToolError(f"{self.label} has no action '{action}'. Available: {', '.join(self.actions)}")
        required, optional = spec.arg_names()
        if self.account_service:
            optional = optional + ["account"]
        clean = {}
        for k in required + optional:
            v = args.get(k)
            if isinstance(v, str):
                v = v.strip()
            if v not in (None, "", []):
                clean[k] = v
        missing = [k for k in required if k not in clean]
        if missing:
            raise ToolError(f"{self.id}.{action} needs: {', '.join(missing)}")
        return clean

    def prepare(self, action: str, args: dict[str, Any], ctx: ToolContext) -> Prepared:
        clean = self.check_args(action, args)
        hint = clean.pop("account", None)
        if not self.account_service:
            return getattr(self, f"prepare_{action}")(clean, ctx)
        from assistant import connections
        account = connections.resolve(self.account_service, str(hint)) if hint else None
        scoped, made = self._scope(ctx, account)
        prepared = getattr(self, f"prepare_{action}")(clean, scoped)
        ids = connections.connected_with(self.account_service)
        if not account and len(ids) > 1:
            # Several accounts and none named: an approval must run on the account it showed - the
            # one holding the message / event it's about, or the default for anything new.
            owner = getattr(made.get("provider"), "owner", None)
            reads_only = all(c.endswith("_READ") for c in prepared.capabilities)
            account = owner or (None if reads_only else ids[0])
        if account:
            prepared.args["account"] = account
            if prepared.preview is not None and len(ids) > 1:
                prepared.preview["fields"].append({"label": "Account", "value": connections.label(account), "block": False})
        return prepared

    def execute(self, action: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """-> {"text": what to tell the user, "data": structured result for a
        model to reason over}."""
        if self.account_service:
            ctx = self._scope(ctx, args.get("account"))[0]
        return getattr(self, f"run_{action}")(args, ctx)

    def _scope(self, ctx: ToolContext, account: Optional[str]) -> tuple[ToolContext, dict[str, Any]]:
        """ctx with this tool's provider limited to one account (if given), made once
        and kept in the returned dict, so prepare() can see which account it used."""
        made: dict[str, Any] = {}
        base = getattr(ctx, self.account_service)

        def provider():
            if "provider" not in made:
                made["provider"] = base(account) if account else base()
            return made["provider"]
        return replace(ctx, **{self.account_service: provider}), made

    def remember(self, action: str, result: dict[str, Any], memory: dict[str, Any]) -> None:
        """Note what this result mentioned so later turns can refer to it."""


REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    REGISTRY[tool.id] = tool
    return tool


def unregister(tool_id: str, tool: Optional[Tool] = None) -> bool:
    """Remove a tool (the Module Registry does this when a module stops). If `tool` is given
    the entry is only removed while it is still that object, so a stale caller can't remove a newer one."""
    current = REGISTRY.get(tool_id)
    if current is None or (tool is not None and current is not tool):
        return False
    del REGISTRY[tool_id]
    return True


def get(tool_id: str) -> Tool:
    tool = REGISTRY.get(tool_id)
    if not tool:
        raise ToolError(f"Unknown tool '{tool_id}'. Available: {', '.join(REGISTRY)}")
    return tool


def catalog() -> str:
    """The tool list a planning model is shown: only what the Personal
    Assistant is allowed to use (assistant/scope.py)."""
    from assistant import scope
    from assistant import connections
    lines = []
    for tool in REGISTRY.values():
        if scope.tool_refusal(tool.id):
            continue
        # With more than one account connected the model may say which one to use.
        ids = connections.connected_with(tool.account_service) if tool.account_service else []
        accounts = ""
        if len(ids) > 1:
            names = [connections.label(cid) + (" (default)" if i == 0 else "") for i, cid in enumerate(ids)]
            accounts = (f"which connected account: {', '.join(names)}. Leave it out to use them all for reading, "
                        "the one that has the item for replies and changes, and the default for anything new")
        for spec in tool.actions.values():
            args = ", ".join(list(spec.args) + (["account?"] if accounts else []))
            lines.append(f"- {tool.id}.{spec.name}({args}): {spec.description}")
            for name, desc in spec.args.items():
                lines.append(f"    {name.rstrip('?')}: {desc}")
            if accounts:
                lines.append(f"    account: {accounts}")
    return "\n".join(lines)


def load_all() -> None:
    """Import the built-in tools so they register themselves."""
    from assistant.tools import agent, ai, calendar, email, reminders, workspace  # noqa: F401
