"""
What a module is given: a ModuleContext, and nothing else of the manager. Every
registration made through it is recorded against the module, so stopping the
module can remove all of it again - including when initialize() fails halfway.
"""

import logging
import os
import re
from typing import Any, Callable, Optional

import db
from assistant import scope
from assistant import tools as tool_registry
from assistant.tools import Tool
from den_modules import events

COMMAND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ModuleError(Exception):
    """A module (or the registry on its behalf) did something not allowed; the message says what."""


class ModuleContext:
    def __init__(self, record: Any, manager: Any):
        self._rec = record
        self._mgr = manager
        self.alive = True
        self.name: str = record.manifest.name
        self.config: dict[str, Any] = dict(record.config)
        self.log = _Log(logging.getLogger(f"the-den.module.{self.name}"), self.name)

    # ---------------------------------------------------------- registration

    def register_tool(self, tool: Tool) -> None:
        """Add a tool to the assistant's tool registry. It still has to be granted in tools.json /
        agents.json before the Personal Assistant may use it - registering is not permission."""
        self._check_alive()
        tid = getattr(tool, "id", None)
        if not isinstance(tool, Tool) or not isinstance(tid, str) or not TOOL_ID_RE.match(tid):
            raise ModuleError("a tool must be a Tool subclass instance with an id like 'hello' (lowercase, digits, _)")
        if not tool.actions or not all(callable(getattr(tool, f"prepare_{a}", None)) and callable(getattr(tool, f"run_{a}", None))
                                       for a in tool.actions):
            raise ModuleError(f"tool '{tid}' needs at least one action, each with prepare_<action> and run_<action>")
        # scope.py maps a tool id to its tools.json grant; a module must not be able to ride on a built-in's grant.
        if tid in scope.TOOL_IDS or tid in scope.TOOL_IDS.values():
            raise ModuleError(f"tool id '{tid}' belongs to a built-in tool")
        owner = self._mgr.tool_owner(tid)
        if tid in tool_registry.REGISTRY:
            raise ModuleError(f"tool id '{tid}' is already registered" + (f" by module '{owner}'" if owner else " (built-in)"))
        self._guard_tool(tool)
        tool_registry.register(tool)
        self._rec.tools.append(tool)

    def register_command(self, name: str, handler: Callable[[str], Any], description: str = "") -> None:
        """Add a command (like /hello). handler(arg_text) -> text. Commands are run through
        POST /modules/commands/{name}; the dashboard has no command line to type them into yet."""
        self._check_alive()
        if not COMMAND_RE.match(name or "") or not callable(handler):
            raise ModuleError("a command needs a name like 'hello' (lowercase, digits, - _) and a callable handler")
        owner = self._mgr.command_owner(name)
        if owner:
            raise ModuleError(f"command '/{name}' is already registered by module '{owner}'")
        self._mgr.add_command(self.name, name, self._guard(handler, f"command /{name}"), description)
        self._rec.commands.append(name)

    def subscribe(self, event: str, handler: Callable[[Any], None]) -> None:
        """Listen for a Den event (den_modules.events.EVENTS). Removed automatically when the module stops."""
        self._check_alive()
        if not callable(handler):
            raise ModuleError("an event handler must be callable")
        try:
            remove = events.subscribe(event, self.name, self._guard(handler, f"event {event}", quiet_return=True),
                                      on_error=None)
        except ValueError as e:
            raise ModuleError(str(e))
        self._rec.unsubs.append(remove)

    def add_cleanup(self, fn: Callable[[], None]) -> None:
        """Run fn when the module stops (or when initialize/start fails). Register anything you open
        here straight after opening it - cleanups run last-in first-out."""
        self._check_alive()
        self._rec.cleanups.append(fn)

    # --------------------------------------------------------------- services

    def secret(self, env_name: str) -> Optional[str]:
        """An environment variable the manifest lists under "secrets" (the launchd plist is where The
        Den keeps its keys). Anything not listed is refused; never put a secret in module.json."""
        if env_name not in self._rec.manifest.secrets:
            raise ModuleError(f"'{env_name}' is not listed under \"secrets\" in module.json")
        return os.environ.get(env_name) or None

    def get_state(self, key: str, default: Any = None) -> Any:
        """Small private state that survives restarts (stored in The Den's settings table, namespaced)."""
        return db.get_setting(f"module_state:{self.name}:{key}", default)

    def set_state(self, key: str, value: Any) -> None:
        db.set_setting(f"module_state:{self.name}:{key}", value)

    def dependency(self, name: str) -> Any:
        """The running instance of a module this one declared in requires/optional, else None."""
        m = self._rec.manifest
        if name not in m.requires and name not in m.optional:
            raise ModuleError(f"'{name}' is not declared in requires/optional")
        return self._mgr.instance_of(name)

    # --------------------------------------------------------------- internals

    def _check_alive(self) -> None:
        if not self.alive:
            raise ModuleError("this module has been stopped; it can't register anything now")

    def _guard(self, fn: Callable, what: str, quiet_return: bool = False) -> Callable:
        rec = self._rec

        def guarded(*a: Any, **k: Any) -> Any:
            if not self.alive:
                if quiet_return:
                    return None
                raise ModuleError(f"module '{self.name}' is no longer running")
            try:
                return fn(*a, **k)
            except Exception as e:
                rec.record("runtime", e, f"{what} failed")
                if quiet_return:
                    return None
                raise ModuleError(f"{what} failed in module '{self.name}': {e.__class__.__name__}")
        return guarded

    def _guard_tool(self, tool: Tool) -> None:
        """Wrap this tool object so a crash is logged against the module and surfaces as a plain ToolError,
        and so it can never claim a capability the manifest did not declare."""
        from assistant.tools import ToolError
        orig_prepare, orig_execute = tool.prepare, tool.execute
        rec, ctx = self._rec, self
        allowed = set(rec.manifest.capabilities)

        def prepare(action: str, args: dict, tctx: Any):
            if not ctx.alive:
                raise ToolError(f"The {tool.label or tool.id} module isn't running.")
            try:
                prepared = orig_prepare(action, args, tctx)
            except ToolError:
                raise
            except Exception as e:
                rec.record("runtime", e, f"tool {tool.id}.{action} prepare failed")
                raise ToolError(f"The {tool.label or tool.id} module hit an error. It has been logged.")
            extra = [c for c in prepared.capabilities if c not in allowed]
            if extra:
                rec.record("runtime", ModuleError(f"asked for undeclared capabilities {extra}"), f"tool {tool.id}.{action}")
                raise ToolError(f"The {tool.label or tool.id} module asked for permissions it didn't declare ({', '.join(extra)}).")
            return prepared

        def execute(action: str, args: dict, tctx: Any):
            if not ctx.alive:
                raise ToolError(f"The {tool.label or tool.id} module isn't running.")
            try:
                return orig_execute(action, args, tctx)
            except ToolError:
                raise
            except Exception as e:
                rec.record("runtime", e, f"tool {tool.id}.{action} failed")
                raise ToolError(f"The {tool.label or tool.id} module hit an error. It has been logged.")
        tool.prepare, tool.execute = prepare, execute   # instance attributes: only this module's object is touched


class _Log(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, name: str):
        super().__init__(logger, {})
        self._name = name

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        return f"[module:{self._name}] {msg}", kwargs
