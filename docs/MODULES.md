# Module Registry

Optional features live in `modules/<name>/` and are switched on, off and reloaded
without editing The Den's source. Code: `den_modules/`. Example: `modules/hello/`.
Tests: `tests/test_modules.py`.

## Architecture

```
modules/<name>/module.json   manifest (data only - never executes)
modules/<name>/module.py     implementation: class Module
settings table, key "modules"   owner's switches: {name: {"enabled": bool, "config": {...}}}

den_modules/manifest.py   parse + validate manifests, version ranges, config validation
den_modules/manager.py    discovery, dependency plan, lifecycle, status   (the singleton `manager`)
den_modules/context.py    ModuleContext: the only thing a module is handed
den_modules/events.py     tiny dispatcher over db.task_added_hooks / db.task_completed_hooks
den_modules/api.py        /modules/... endpoints (X-API-Key like every other router)
```

Everything goes through one function, `reconcile()`: from the manifests on disk,
the owner's settings and the dependency graph it works out which modules should be
running, stops those that shouldn't (dependents first), then starts those that
should (dependencies first). Enable, disable, reload, config change and rescan only
change the inputs and call it.

`main.py` calls `manager.start_all()` at startup and `stop_all()` at shutdown. Both
are wrapped so a module problem can't stop The Den starting.

## Lifecycle

`discover` (read manifests; no code runs) -> `validate` (manifest, config, dependency
versions, cycles) -> `load` (import the entry file, instantiate `Module()`) ->
`initialize(ctx)` -> `start()` ... `stop()` -> `unload()` -> registrations removed.

A module that is disabled, invalid or blocked is never imported and nothing is initialised.

Statuses: `running`, `disabled`, `blocked` (a dependency problem), `failed` (a stage
raised - see `stage`/`detail`/`errors`), `invalid` (bad manifest or config).

If `initialize` or `start` raises, `stop()`/`unload()` are **not** called (nothing
finished starting); every tool, command and listener it registered is removed and its
`ctx.add_cleanup` callbacks run. If `stop()` or `unload()` raise, the errors are
recorded and the removal still happens.

## Manifest (`module.json`)

| key | meaning |
|---|---|
| `name` | required; lowercase letters/digits/`-`/`_`; must equal the folder name |
| `version` | required, `1.2.3` |
| `description` | shown in status |
| `enabled` | default switch (the owner's setting, below, overrides it). Default `true` |
| `entry` | file defining `class Module`. Default `module.py` |
| `requires` / `optional` | `{"other": ">=1.0,<2"}`. Operators `>= <= > < == =`; `""`/`"*"` = any |
| `config` | `{"key": {"type": "string\|integer\|number\|boolean", "default": .., "required": true, "restart": true}}` |
| `secrets` | environment variable names the module may read via `ctx.secret()` |
| `capabilities` | capabilities its tools may ask for (names from `assistant/capabilities.py`) |
| `hot_reload` | `false` if it owns something that can't be unloaded (see below). Default `true` |

## Module interface

```python
class Module:                       # no constructor arguments
    def initialize(self, ctx): ...  # register tools/commands/listeners; open resources
    def start(self): ...            # optional
    def stop(self): ...             # optional
    def unload(self): ...           # optional
```
All four hooks are optional except that the class must exist.

## Context (`ctx`)

| | |
|---|---|
| `ctx.name`, `ctx.config` | the module's name; validated config (manifest defaults + owner values) |
| `ctx.log` | logger `the-den.module.<name>`, lines prefixed `[module:<name>]` |
| `ctx.register_tool(tool)` | add a `assistant.tools.Tool` to the existing tool registry |
| `ctx.register_command(name, fn, description)` | `fn(arg_text) -> text`; see "Commands" |
| `ctx.subscribe(event, fn)` | `TASK_ADDED` (payload: task dict), `TASK_COMPLETED` (`{"task_id", "result_summary"}`) |
| `ctx.add_cleanup(fn)` | run on stop or on a failed start; register anything you open right after opening it |
| `ctx.secret("ENV_NAME")` | env var, only if listed in `secrets`. Where The Den keeps keys is the launchd plist - never put a secret in `module.json` or the config |
| `ctx.get_state(k, default)` / `ctx.set_state(k, v)` | small private JSON state in the settings table (survives restarts) |
| `ctx.dependency(name)` | the running `Module` instance of a declared dependency, else `None` |

Nothing else of the manager is reachable from `ctx`.

## Creating a module

1. `modules/mymod/module.json`: `{"name": "mymod", "version": "1.0.0"}` plus anything above.
2. `modules/mymod/module.py` with `class Module`. Helper files in the same folder import with `from . import helper`.
3. `POST /modules/rescan` (or restart). It appears in `GET /modules`.

Complete example - `modules/hello/`:

```json
{"name": "hello", "version": "1.0.0", "description": "Example module.",
 "config": {"greeting": {"type": "string", "default": "Hello"}}, "capabilities": []}
```
```python
from assistant.tools import ActionSpec, Prepared, Tool
from den_modules import events

class HelloTool(Tool):
    id, label = "hello", "Hello"
    actions = {"greet": ActionSpec("greet", "Greet someone.", {"name?": "who"})}
    def __init__(self, greeting): self.greeting = greeting
    def prepare_greet(self, args, ctx):
        return Prepared({"name": args.get("name", "world")}, [], "greet", "Saying hello")
    def run_greet(self, args, ctx):
        text = f"{self.greeting}, {args['name']}!"
        return {"text": text, "data": {"greeting": text}}

class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        ctx.register_tool(HelloTool(ctx.config["greeting"]))
        ctx.register_command("hello", lambda arg: f"{ctx.config['greeting']}, {arg or 'world'}!", "Say hello")
        ctx.subscribe(events.TASK_COMPLETED, lambda e: ctx.log.info("task %s done", e["task_id"]))
```

## Tools, commands, events

**Tools** go into `assistant.tools.REGISTRY`, so the planner's catalog and `actions.invoke` see them.
Registering is *not* permission: the existing gate is unchanged. The Personal Assistant can only
use a module tool once the owner adds an entry with the same id to `tools.json` and lists it in the
agent's `tools` (`assistant/scope.py`); the capability policies (ALLOW/ASK/DENY) apply as usual.
The registry adds two rules: a tool may only return capabilities listed in the manifest's
`capabilities` (otherwise the call is refused), and a tool id can't be a built-in's id or tools.json
grant name (so a module can't ride on Gmail's permission). A tool's crash is logged against the module and
reaches the caller as a plain `ToolError`.

**Commands.** The dashboard has no command line or slash-command system, and none was invented.
Commands are held by the registry, listed in `GET /modules`, and run with
`POST /modules/commands/<name>` `{"arg": "..."}`. Wiring a `/hello` input to that endpoint is a UI task.

**Events.** The Den has no general event bus; `events.py` is a ~60-line dispatcher over the existing
`db` hook lists. `TASK_COMPLETED` fires from `db.mark_done` (the success path of every lane; not for errors,
and not for `mark_reviewed`). Listeners run synchronously on the thread that raised the event: keep them quick.
A listener that raises affects nobody else.

All three are tracked per module and removed on stop/unload/reload/failed start; duplicates
(tool id, command name) are refused while the owner is alive. A `ctx` is marked dead on stop: late calls to
`register_*` raise and late handler calls are ignored.

## Dependencies

Dependencies are resolved among *enabled* modules. Missing, disabled, version-incompatible and
circular dependencies mark the module `blocked` with a precise message (e.g.
`requires 'a' >=2.0, but 1.5.0 is installed`, `circular dependency: x -> y -> x`); other modules are unaffected.
Dependencies initialise first and stop last. Stopping or reloading a module first stops its dependents and
restarts them afterwards. Optional dependencies are ordered first when present, and noted in `warnings` when not.
(Optional dependencies that appear *later* do not retroactively restart modules that started without them.)

## Configuration, enable/disable

State lives in the settings table (`modules` key), editable through the API, no source edits:

```
POST  /modules/<name>/enable | disable | reload
PATCH /modules/<name>/config   {"config": {"greeting": "Namaste"}}   # validated, then the module is reloaded
POST  /modules/rescan                                                # new/removed/changed modules
GET   /modules, GET /modules/<name>                                  # status; the second adds config + error stacks
```
Status has name, version, description, enabled, status, stage, detail, tool/command/listener counts, warnings,
`restart_required`, the last error, and (detail view) up to 20 errors with stage, UTC time, and full traceback.

## Hot reload vs restart required

Hot (module-level, no restart): module code and its helper files, manifest changes (version, dependencies,
config schema), tools, commands, listeners, config values, enable/disable. Reload re-imports from disk
(stale `__pycache__` is cleared first and the old modules are removed from `sys.modules`).

Restart required - stated, never faked:
* `"hot_reload": false` in the manifest: use it for modules with native extensions, background processes or
  anything that can't be torn down. Disable/reload leaves it **running**, sets `restart_required`, and applies at restart.
* Config keys marked `"restart": true`: saved, applied at the next start.
* Anything in The Den itself: `db.py`, `api.py`, `worker.py`, the registry code. Not module-managed.
* `stop_all()` at shutdown stops even non-hot modules.

## Limitations

* **Isolation is logical, not a sandbox.** Modules are Python in The Den's process: a module can read the
  database, environment or files if its code tries. `secrets` and `capabilities` stop honest mistakes and
  routine overreach; they do not contain hostile code. Only install modules you trust.
* Threads, sockets or subprocesses a module starts are not tracked; stop them in `stop()`/`add_cleanup`.
  A module that leaks them leaks them across reloads. There are no timeouts: a hook that hangs hangs the call.
* Third-party libraries a module imports stay cached in `sys.modules` across reload (only the module's own code reloads).
* A running tool call or event handler is not interrupted by unload.
* No file watcher: reload is explicit (`rescan`). `rescan` is cheap (stat of the module files) if you want to
  schedule it.
* Existing built-in tools stay as they are; none were migrated.
