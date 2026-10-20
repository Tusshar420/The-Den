"""
The Module Registry: discovers modules in modules/<name>/ (module.json + an entry
file), works out which should run, and takes each through

    discover -> validate -> load -> initialize -> start  ...  stop -> unload

Everything funnels through reconcile(): "given the manifests on disk, the owner's
enable/config settings and the dependency graph, which modules should be running?"
It stops what shouldn't (dependents first), then starts what should (dependencies
first). Enable, disable, reload, config changes and rescans are all just a change
to the inputs followed by reconcile().

Isolation: each stage of each module runs in its own try/except. A failure is
recorded on that module (stage, time, stack), its registrations are rolled back,
and the module - plus anything that requires it - is marked failed/blocked. The
Den and every other module carry on.
"""

import importlib
import importlib.util
import logging
import shutil
import sys
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import db
from assistant import tools as tool_registry
from den_modules import events, manifest as mf
from den_modules.context import ModuleContext, ModuleError

log = logging.getLogger("the-den.modules")

ROOT = Path(__file__).resolve().parent.parent / "modules"
SETTING = "modules"          # {name: {"enabled": bool, "config": {...}}} - the owner's switches, in the settings table
MAX_ERRORS = 20

INVALID, DISABLED, BLOCKED, FAILED, RUNNING = "invalid", "disabled", "blocked", "failed", "running"


class Record:
    """Everything known about one module folder, loaded or not."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.manifest: Optional[mf.Manifest] = None
        self.status = INVALID
        self.stage = "discover"
        self.manifest_error = ""               # set while module.json / its config can't be used
        self.detail = ""                       # why it isn't running, in words for the owner
        self.warnings: list[str] = []
        self.errors: deque = deque(maxlen=MAX_ERRORS)
        self.config: dict[str, Any] = {}
        self.restart_required = ""             # non-empty: why only a restart can apply the owner's last change
        self.fingerprint: tuple = ()
        self.loaded_fingerprint: tuple = ()
        self.instance: Any = None
        self.ctx: Optional[ModuleContext] = None
        self.pymod = ""
        self.tools: list[Any] = []
        self.commands: list[str] = []
        self.unsubs: list[Callable[[], None]] = []
        self.cleanups: list[Callable[[], None]] = []
        self.used_deps: list[str] = []         # modules this run was started after (for stopping dependents)

    @property
    def name(self) -> str:
        return self.manifest.name if self.manifest else self.folder.name

    def record(self, stage: str, exc: BaseException, what: str = "") -> None:
        """Log a failure against this module: stage, time, message and full stack."""
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.errors.append({"stage": stage, "time": datetime.now(timezone.utc).isoformat(), "message": what or str(exc),
                            "error": f"{exc.__class__.__name__}: {exc}", "traceback": text})
        logging.getLogger(f"the-den.module.{self.name}").error("[module:%s] stage=%s %s", self.name, stage,
                                                              what or str(exc), exc_info=exc)


def _fingerprint(folder: Path) -> tuple:
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json"):
            st = p.stat()
            out.append((str(p.relative_to(folder)), st.st_mtime_ns, st.st_size))
    return tuple(out)


class ModuleManager:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else ROOT
        self.records: dict[str, Record] = {}
        self._lock = threading.RLock()
        self._commands: dict[str, tuple[str, Callable, str]] = {}

    # ================================================================ queries

    def tool_owner(self, tool_id: str) -> Optional[str]:
        for r in self.records.values():
            if any(t.id == tool_id for t in r.tools):
                return r.name
        return None

    def command_owner(self, name: str) -> Optional[str]:
        hit = self._commands.get(name)
        return hit[0] if hit else None

    def add_command(self, module: str, name: str, fn: Callable, description: str) -> None:
        self._commands[name] = (module, fn, description)

    def instance_of(self, name: str) -> Any:
        r = self.records.get(name)
        return r.instance if r and r.status == RUNNING else None

    def commands(self) -> list[dict[str, str]]:
        return [{"name": n, "module": m, "description": d} for n, (m, _, d) in sorted(self._commands.items())]

    def run_command(self, name: str, arg: str = "") -> Any:
        hit = self._commands.get(name)
        if not hit:
            raise KeyError(name)
        return hit[1](arg)

    def status(self, name: Optional[str] = None, detail: bool = False) -> Any:
        with self._lock:
            if name is not None:
                return self._view(self.records[name], detail)
            return [self._view(r, detail) for r in sorted(self.records.values(), key=lambda r: r.name)]

    def _view(self, r: Record, detail: bool) -> dict[str, Any]:
        m = r.manifest
        errors = list(r.errors)
        view = {"name": r.name, "version": m.version if m else None, "description": m.description if m else "",
                "enabled": self._enabled(r) if m else False, "status": r.status, "stage": r.stage, "detail": r.detail,
                "restart_required": r.restart_required, "warnings": list(r.warnings),
                "tools": [t.id for t in r.tools], "tool_count": len(r.tools),
                "commands": list(r.commands), "command_count": len(r.commands),
                "event_listeners": len(r.unsubs), "hot_reload": m.hot_reload if m else True,
                "requires": dict(m.requires) if m else {}, "optional": dict(m.optional) if m else {},
                "error_count": len(errors), "last_error": ({k: errors[-1][k] for k in ("stage", "time", "message", "error")}
                                                          if errors else None)}
        if detail:
            view["config"] = dict(r.config)
            view["config_schema"] = dict(m.config) if m else {}
            view["errors"] = errors
        return view

    # ============================================================== settings

    def _settings(self) -> dict[str, Any]:
        s = db.get_setting(SETTING, {})
        return s if isinstance(s, dict) else {}

    def _enabled(self, r: Record) -> bool:
        mine = self._settings().get(r.name, {})
        return bool(mine["enabled"]) if "enabled" in mine else r.manifest.enabled

    def _owner_config(self, name: str) -> dict[str, Any]:
        cfg = self._settings().get(name, {}).get("config", {})
        return cfg if isinstance(cfg, dict) else {}

    def _save(self, name: str, **fields: Any) -> None:
        s = self._settings()
        s[name] = {**s.get(name, {}), **fields}
        db.set_setting(SETTING, s)

    # ============================================================== discovery

    def discover(self) -> None:
        """Read every manifest on disk. Never imports module code. Vanished folders are stopped and forgotten."""
        with self._lock:
            found: dict[str, Path] = {}
            if self.root.is_dir():
                for f in sorted(self.root.iterdir()):
                    if f.is_dir() and not f.name.startswith((".", "_")) and (f / "module.json").exists():
                        found[f.name] = f
            for name in [n for n in self.records if n not in found]:
                self._teardown(self.records[name], force=True)
                del self.records[name]
            for name, folder in found.items():
                r = self.records.get(name) or self.records.setdefault(name, Record(folder))
                r.folder = folder
                r.fingerprint = _fingerprint(folder)
                try:
                    r.manifest = mf.load(folder)
                    r.config = mf.resolve_config(r.manifest, self._owner_config(name))
                    r.manifest_error = ""
                    if r.status == INVALID:
                        r.detail = ""
                except mf.ManifestError as e:
                    # On a running module this is picked up by reconcile as "stop it".
                    r.manifest_error = str(e)
                    if r.status != RUNNING:
                        r.status, r.stage, r.detail = INVALID, "validate", r.manifest_error

    # ================================================================ reconcile

    def reconcile(self, reload: tuple[str, ...] = ()) -> None:
        with self._lock:
            try:
                self._reconcile(set(reload))
            except Exception:        # a bug here must never take The Den down
                log.exception("module reconcile failed")

    def _static_plan(self) -> tuple[list[str], dict[str, str]]:
        """-> (names that statically may run, in dependency order; name -> why not, for enabled-but-blocked ones)."""
        for r in self.records.values():
            r.warnings = []
        recs = {n: r for n, r in self.records.items() if r.manifest and not r.manifest_error}
        enabled = {n for n, r in recs.items() if self._enabled(r)}
        why: dict[str, str] = {}
        edges: dict[str, set[str]] = {}
        for n in sorted(enabled):
            m = recs[n].manifest
            deps: set[str] = set()
            for dep, spec in m.requires.items():
                d = recs.get(dep)
                if d is None:
                    why[n] = f"requires module '{dep}', which is not installed"
                elif dep not in enabled:
                    why[n] = f"requires module '{dep}', which is disabled"
                elif not mf.satisfies(d.manifest.version, spec):
                    why[n] = f"requires '{dep}' {spec}, but {d.manifest.version} is installed"
                deps.add(dep)
            for dep, spec in m.optional.items():
                d = recs.get(dep)
                if d is not None and dep in enabled and mf.satisfies(d.manifest.version, spec):
                    deps.add(dep)
                elif d is not None and dep in enabled:
                    recs[n].warnings.append(f"optional '{dep}' {spec} is installed as {d.manifest.version}; running without it")
                else:
                    recs[n].warnings.append(f"optional module '{dep}' isn't available; running without it")
            edges[n] = {d for d in deps if d in enabled}
        # Kahn's algorithm; what's left over is a cycle or sits downstream of one.
        order: list[str] = []
        left = {n: set(e) for n, e in edges.items()}
        ready = sorted(n for n, e in left.items() if not e)
        while ready:
            n = ready.pop(0)
            order.append(n)
            for k, e in left.items():
                if n in e:
                    e.discard(n)
                    if not e and k not in order and k not in ready:
                        ready.append(k)
            ready.sort()
        stuck = [n for n in left if n not in order]
        for n in stuck:
            cyc = self._find_cycle(n, edges)
            why.setdefault(n, ("circular dependency: " + " -> ".join(cyc)) if cyc else
                           "depends on a module that is part of a dependency cycle")
        # Required dependency blocked => dependent blocked. (order is topological, so one pass is enough.)
        runnable: list[str] = []
        for n in order:
            if n not in why:
                bad = [d for d in recs[n].manifest.requires if d in why or d not in order]
                if bad:
                    why[n] = f"requires '{bad[0]}', which can't run ({why.get(bad[0], 'blocked')})"
            if n not in why:
                runnable.append(n)
        for n in stuck:
            if n in why and n not in order:
                order.append(n)
        return runnable, why

    @staticmethod
    def _find_cycle(start: str, edges: dict[str, set[str]]) -> list[str]:
        path: list[str] = []

        def dfs(n: str) -> Optional[list[str]]:
            if n in path:
                return path[path.index(n):] + [n]
            path.append(n)
            for d in sorted(edges.get(n, ())):
                got = dfs(d)
                if got:
                    return got
            path.pop()
            return None
        return dfs(start) or []

    def _reconcile(self, reload: set[str]) -> None:
        runnable, why = self._static_plan()
        runnable_set = set(runnable)
        # ---- 1. what must stop: no longer wanted, asked to reload, or changed on disk
        stop: set[str] = set()
        for n, r in self.records.items():
            if r.status != RUNNING:
                continue
            if n not in runnable_set or r.manifest_error or n in reload:
                stop.add(n)
        # A module that can't be unloaded stays up; its dependents don't need to stop on its account.
        stuck = {n for n in stop if not self.records[n].manifest.hot_reload}
        for n in stuck:
            r = self.records[n]
            r.restart_required = ("disabled" if n not in runnable_set else "reload requested") + \
                                 " - this module can't be unloaded while The Den runs; restart The Den to apply it"
            stop.discard(n)
        grew = True
        while grew:       # dependents of anything stopping must stop first
            grew = False
            for n, r in self.records.items():
                if r.status == RUNNING and n not in stop and any(d in stop for d in r.used_deps):
                    stop.add(n)
                    grew = True
        for n in self._reverse_order(stop):
            self._teardown(self.records[n])
        # ---- 2. what must start, dependencies first
        for n in runnable:
            r = self.records[n]
            if r.status == RUNNING:
                continue
            down = [d for d in r.manifest.requires if self.records[d].status != RUNNING]
            if down:
                r.status, r.stage, r.detail = BLOCKED, "dependencies", f"requires '{down[0]}', which isn't running"
                continue
            self._start(r)
        for n, r in self.records.items():
            if r.status == RUNNING or n in runnable_set:
                continue
            if r.manifest is None or r.manifest_error:
                r.status, r.stage, r.detail = INVALID, "validate", r.manifest_error or r.detail
            elif n in why:
                r.status, r.stage, r.detail = BLOCKED, "dependencies", why[n]
            else:
                r.status, r.stage, r.detail = DISABLED, "discover", "switched off"

    def _reverse_order(self, names: set[str]) -> list[str]:
        """Dependents before dependencies, using the dependencies each was actually started with."""
        out: list[str] = []
        todo = set(names)
        while todo:
            leaf = sorted(n for n in todo if not any(n in self.records[o].used_deps for o in todo if o != n))
            if not leaf:        # can't happen without a cycle; don't loop forever regardless
                leaf = sorted(todo)
            out.extend(leaf)
            todo -= set(leaf)
        return out

    # ================================================================ lifecycle

    def _start(self, r: Record) -> None:
        m = r.manifest
        r.status, r.detail, r.restart_required = FAILED, "", ""
        r.used_deps = [d for d in list(m.requires) + list(m.optional) if d in self.records and self.records[d].status == RUNNING]
        r.tools, r.commands, r.unsubs, r.cleanups = [], [], [], []
        stage = "load"
        try:
            r.stage = stage
            ctx = ModuleContext(r, self)
            r.ctx = ctx
            r.instance = self._import(r)
            stage = r.stage = "initialize"
            init = getattr(r.instance, "initialize", None)
            if init:
                init(ctx)
            stage = r.stage = "start"
            start = getattr(r.instance, "start", None)
            if start:
                start()
        except BaseException as e:      # noqa: BLE001 - SystemExit from module code must not end The Den either
            if isinstance(e, KeyboardInterrupt):
                raise
            r.record(stage, e)
            self._release(r)
            r.status, r.stage, r.detail = FAILED, stage, f"{stage} failed: {e.__class__.__name__}: {e}"
            return
        r.status, r.stage, r.detail = RUNNING, "running", ""
        r.loaded_fingerprint = r.fingerprint
        log.info("module %s %s started (%d tools, %d commands, %d listeners)", r.name, m.version,
                 len(r.tools), len(r.commands), len(r.unsubs))

    def _import(self, r: Record) -> Any:
        m = r.manifest
        pkg = "den_module_" + m.name.replace("-", "_x_")
        shutil.rmtree(r.folder / "__pycache__", ignore_errors=True)    # never run stale bytecode after an edit
        importlib.invalidate_caches()
        self._purge(pkg)
        r.pymod = pkg
        spec = importlib.util.spec_from_loader(pkg, loader=None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(r.folder)]
        sys.modules[pkg] = package
        mod = importlib.import_module(f"{pkg}.{Path(m.entry).stem}")
        cls = getattr(mod, "Module", None)
        if cls is None:
            raise ModuleError(f"{m.entry} must define a class named Module")
        return cls()

    @staticmethod
    def _purge(pkg: str) -> None:
        for k in [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]:
            del sys.modules[k]

    def _release(self, r: Record) -> None:
        """Undo everything this module registered. Safe to call twice; never raises."""
        ctx = r.ctx
        if ctx:
            ctx.alive = False
        for fn in reversed(r.cleanups):
            try:
                fn()
            except Exception as e:
                r.record("unload", e, "cleanup callback failed")
        for off in r.unsubs:
            try:
                off()
            except Exception as e:
                r.record("unload", e, "removing an event listener failed")
        events.unsubscribe_owner(r.name)
        for t in r.tools:
            tool_registry.unregister(t.id, t)
        for c in r.commands:
            if self._commands.get(c, (None,))[0] == r.name:
                del self._commands[c]
        r.tools, r.commands, r.unsubs, r.cleanups = [], [], [], []
        r.instance = None
        r.ctx = None
        if r.pymod:
            self._purge(r.pymod)
            r.pymod = ""

    def _teardown(self, r: Record, force: bool = False) -> None:
        """stop() then unload(), then remove every registration - even if either hook raised."""
        if r.status != RUNNING:
            self._release(r)
            return
        if not r.manifest.hot_reload and not force:
            r.restart_required = "can't be unloaded while The Den runs; restart The Den to apply the change"
            return
        inst = r.instance
        for stage in ("stop", "unload"):
            r.stage = stage
            fn = getattr(inst, stage, None)
            if fn:
                try:
                    fn()
                except Exception as e:
                    r.record(stage, e)
        self._release(r)
        r.status, r.stage = DISABLED, "unloaded"
        r.used_deps = []

    # ============================================================ public actions

    def start_all(self) -> None:
        with self._lock:
            try:
                self.discover()
            except Exception:
                log.exception("module discovery failed")
                return
            self.reconcile()

    def stop_all(self) -> None:
        with self._lock:
            for n in self._reverse_order({n for n, r in self.records.items() if r.status == RUNNING}):
                self._teardown(self.records[n], force=True)

    def rescan(self) -> list[str]:
        """Re-read the folder; reload only modules whose files changed. -> names reloaded."""
        with self._lock:
            self.discover()
            changed = tuple(n for n, r in self.records.items()
                            if r.status == RUNNING and r.fingerprint != r.loaded_fingerprint)
            self.reconcile(reload=changed)
            return list(changed)

    def enable(self, name: str) -> None:
        with self._lock:
            self._get(name)
            self._save(name, enabled=True)
            self.discover()
            self.reconcile()

    def disable(self, name: str) -> None:
        with self._lock:
            self._get(name)
            self._save(name, enabled=False)
            self.reconcile()

    def reload(self, name: str) -> None:
        with self._lock:
            self._get(name)
            self.discover()
            self.reconcile(reload=(name,))

    def set_config(self, name: str, values: dict[str, Any]) -> None:
        """Validate and save the owner's config, then reload if that is safe. Keys marked "restart": true
        in the manifest are saved but only take effect after a restart."""
        with self._lock:
            r = self._get(name)
            if r.manifest is None:
                raise mf.ManifestError("this module's manifest is invalid, fix that first")
            new = {**self._owner_config(name), **values}
            mf.resolve_config(r.manifest, new)               # raises ManifestError, saving nothing
            self._save(name, config=new)
            restart_keys = [k for k in values if r.manifest.config.get(k, {}).get("restart")]
            self.discover()
            if r.status == RUNNING and restart_keys:
                r.restart_required = f"config {', '.join(restart_keys)} only takes effect after The Den restarts"
                return
            self.reconcile(reload=(name,) if r.status == RUNNING else ())

    def _get(self, name: str) -> Record:
        if name not in self.records:
            raise KeyError(name)
        return self.records[name]


manager = ModuleManager()
