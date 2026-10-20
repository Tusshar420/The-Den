"""
The Module Registry (den_modules/): discovery, lifecycle, dependencies, isolation,
registration and cleanup, config, and the real Hello example module.

Modules here are written into a temp folder per test; nothing touches modules/.
"""

import builtins
import json
import os
import textwrap
from pathlib import Path
from unittest import mock

from tests.helpers import TestBase
from tests import test_e2e

import db
from assistant import actions, tools
from assistant.tools import ToolError
from den_modules import events, manifest as mf
from den_modules.manager import BLOCKED, DISABLED, FAILED, INVALID, RUNNING, ModuleManager

TOOL_CODE = '''
from assistant.tools import ActionSpec, Prepared, Tool

class T(Tool):
    id = "{tool}"
    label = "{tool}"
    actions = {{"go": ActionSpec("go", "go")}}
    def prepare_go(self, args, ctx):
        return Prepared({{}}, {caps}, "t", "going")
    def run_go(self, args, ctx):
        {body}
'''


def snapshot():
    return (set(tools.REGISTRY), events.count())


class ModTest(TestBase):
    def setUp(self):
        super().setUp()
        builtins.DEN_LOG = []
        self.addCleanup(lambda: delattr(builtins, "DEN_LOG"))
        self.root = Path(self._tmp.name) / "modules"
        self.root.mkdir()
        self.mgr = ModuleManager(self.root)
        self.before = snapshot()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.mgr.stop_all()
        for r in self.mgr.records.values():       # whatever a failed test left behind
            self.mgr._release(r)

    @property
    def log(self):
        return builtins.DEN_LOG

    def write(self, name, code="class Module:\n    pass\n", **manifest):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        data = {"name": name, "version": "1.0.0", **manifest}
        (folder / "module.json").write_text(json.dumps(data))
        (folder / "module.py").write_text(textwrap.dedent(code))
        return folder

    def lifecycle_module(self, name, **manifest):
        return self.write(name, f'''
            import builtins
            class Module:
                def __init__(self): builtins.DEN_LOG.append("{name}:load")
                def initialize(self, ctx): builtins.DEN_LOG.append("{name}:init")
                def start(self): builtins.DEN_LOG.append("{name}:start")
                def stop(self): builtins.DEN_LOG.append("{name}:stop")
                def unload(self): builtins.DEN_LOG.append("{name}:unload")
        ''', **manifest)

    def boot(self):
        self.mgr.start_all()

    def st(self, name):
        return self.mgr.status(name)

    def assertClean(self):
        self.assertEqual(snapshot(), self.before)


class Discovery(ModTest):
    def test_finds_modules_and_ignores_private_folders(self):
        self.lifecycle_module("alpha")
        self.lifecycle_module("_draft")
        (self.root / "notes").mkdir()
        self.boot()
        self.assertEqual([m["name"] for m in self.mgr.status()], ["alpha"])
        self.assertEqual(self.st("alpha")["status"], RUNNING)

    def test_missing_modules_folder_is_fine(self):
        ModuleManager(self.root / "nope").start_all()

    def test_invalid_manifests_are_reported_not_fatal(self):
        self.lifecycle_module("good")
        (self.root / "badjson").mkdir()
        (self.root / "badjson" / "module.json").write_text("{nope")
        self.write("wrongname")
        (self.root / "wrongname" / "module.json").write_text(json.dumps({"name": "other", "version": "1.0.0"}))
        self.write("badcap", capabilities=["NOT_A_CAPABILITY"])
        self.write("badver", requires={"good": ">>1"})
        self.boot()
        self.assertEqual(self.st("good")["status"], RUNNING)
        for n in ("badjson", "wrongname", "badcap", "badver"):
            self.assertEqual(self.st(n)["status"], INVALID, n)
        self.assertIn("not valid JSON", self.st("badjson")["detail"])
        self.assertIn("must match", self.st("wrongname")["detail"])
        self.assertIn("NOT_A_CAPABILITY", self.st("badcap")["detail"])

    def test_discovery_never_runs_module_code(self):
        self.write("boom", "raise SystemExit('imported!')\n", enabled=False)
        self.boot()
        self.assertEqual(self.st("boom")["status"], DISABLED)

    def test_version_ranges(self):
        self.assertTrue(mf.satisfies("1.2.3", ">=1.0,<2"))
        self.assertFalse(mf.satisfies("2.0.0", ">=1.0,<2"))
        self.assertTrue(mf.satisfies("1.0", "==1.0.0"))
        self.assertTrue(mf.satisfies("9", "*"))
        with self.assertRaises(ValueError):
            mf.satisfies("1.0.0", "~~1")


class Lifecycle(ModTest):
    def test_stage_order_and_reverse_on_stop(self):
        self.lifecycle_module("alpha")
        self.boot()
        self.assertEqual(self.log, ["alpha:load", "alpha:init", "alpha:start"])
        self.mgr.stop_all()
        self.assertEqual(self.log[3:], ["alpha:stop", "alpha:unload"])
        self.assertEqual(self.st("alpha")["status"], DISABLED)

    def test_disabled_module_is_never_loaded_and_can_be_enabled_without_edits(self):
        self.lifecycle_module("alpha", enabled=False)
        self.boot()
        self.assertEqual((self.st("alpha")["status"], self.log), (DISABLED, []))
        self.mgr.enable("alpha")
        self.assertEqual(self.st("alpha")["status"], RUNNING)
        self.assertEqual(self.log, ["alpha:load", "alpha:init", "alpha:start"])
        self.mgr.disable("alpha")
        self.assertEqual(self.st("alpha")["status"], DISABLED)
        self.assertFalse(self.st("alpha")["enabled"])
        self.assertEqual(self.log[-2:], ["alpha:stop", "alpha:unload"])

    def test_owner_switch_survives_a_new_manager(self):
        self.lifecycle_module("alpha")
        self.boot()
        self.mgr.disable("alpha")
        again = ModuleManager(self.root)
        again.start_all()
        self.assertEqual(again.status("alpha")["status"], DISABLED)
        again.enable("alpha")
        again.stop_all()

    def test_reload_runs_the_full_cycle_and_picks_up_edits(self):
        folder = self.lifecycle_module("alpha")
        self.boot()
        self.log.clear()
        (folder / "module.py").write_text((folder / "module.py").read_text().replace("start\")", "start2!\")"))
        self.mgr.reload("alpha")
        self.assertEqual(self.log, ["alpha:stop", "alpha:unload", "alpha:load", "alpha:init", "alpha:start2!"])
        self.assertEqual(self.st("alpha")["status"], RUNNING)

    def test_reload_picks_up_a_new_manifest_version(self):
        folder = self.lifecycle_module("alpha")
        self.boot()
        data = json.loads((folder / "module.json").read_text())
        data["version"] = "1.1.0"
        (folder / "module.json").write_text(json.dumps(data))
        self.mgr.reload("alpha")
        self.assertEqual(self.st("alpha")["version"], "1.1.0")

    def test_rescan_reloads_only_what_changed(self):
        self.lifecycle_module("alpha")
        beta = self.lifecycle_module("beta")
        self.boot()
        self.log.clear()
        self.assertEqual(self.mgr.rescan(), [])
        self.assertEqual(self.log, [])
        (beta / "module.py").write_text((beta / "module.py").read_text() + "\n# edited\n")
        self.assertEqual(self.mgr.rescan(), ["beta"])
        self.assertEqual([e for e in self.log if e.startswith("alpha")], [])

    def test_rescan_picks_up_new_and_removed_modules(self):
        self.boot()
        self.lifecycle_module("late")
        self.mgr.rescan()
        self.assertEqual(self.st("late")["status"], RUNNING)
        import shutil
        shutil.rmtree(self.root / "late")
        self.mgr.rescan()
        self.assertEqual(self.mgr.status(), [])
        self.assertEqual(self.log[-2:], ["late:stop", "late:unload"])

    def test_helper_files_import_and_are_reloaded(self):
        folder = self.write("helper", '''
            from . import extra
            class Module:
                def initialize(self, ctx): ctx.log.info(extra.VALUE); self.v = extra.VALUE
        ''')
        (folder / "extra.py").write_text("VALUE = 1\n")
        self.boot()
        self.assertEqual(self.mgr.instance_of("helper").v, 1)
        (folder / "extra.py").write_text("VALUE = 22\n")
        self.mgr.reload("helper")
        self.assertEqual(self.mgr.instance_of("helper").v, 22)

    def test_entry_without_module_class_fails_clearly(self):
        self.write("nocls", "x = 1\n")
        self.boot()
        self.assertEqual(self.st("nocls")["status"], FAILED)
        self.assertIn("class named Module", self.st("nocls")["detail"])


class Dependencies(ModTest):
    def test_dependencies_initialise_first_and_stop_last(self):
        self.lifecycle_module("c", requires={"b": ">=1.0"})
        self.lifecycle_module("b", requires={"a": "*"})
        self.lifecycle_module("a")
        self.boot()
        self.assertEqual([e for e in self.log if e.endswith(":init")], ["a:init", "b:init", "c:init"])
        self.log.clear()
        self.mgr.stop_all()
        self.assertEqual([e for e in self.log if e.endswith(":stop")], ["c:stop", "b:stop", "a:stop"])

    def test_missing_required_dependency(self):
        self.lifecycle_module("b", requires={"ghost": "*"})
        self.boot()
        self.assertEqual(self.st("b")["status"], BLOCKED)
        self.assertIn("'ghost', which is not installed", self.st("b")["detail"])
        self.assertEqual(self.log, [])

    def test_incompatible_version(self):
        self.lifecycle_module("a", version="1.5.0")
        self.lifecycle_module("b", requires={"a": ">=2.0"})
        self.boot()
        self.assertEqual(self.st("a")["status"], RUNNING)
        self.assertEqual(self.st("b")["status"], BLOCKED)
        self.assertIn("requires 'a' >=2.0, but 1.5.0 is installed", self.st("b")["detail"])

    def test_disabled_dependency_blocks_dependent(self):
        self.lifecycle_module("a", enabled=False)
        self.lifecycle_module("b", requires={"a": "*"})
        self.boot()
        self.assertIn("which is disabled", self.st("b")["detail"])

    def test_cycle_is_named_and_innocent_modules_still_run(self):
        self.lifecycle_module("x", requires={"y": "*"})
        self.lifecycle_module("y", requires={"x": "*"})
        self.lifecycle_module("z", requires={"x": "*"})
        self.lifecycle_module("ok")
        self.boot()
        self.assertEqual(self.st("ok")["status"], RUNNING)
        for n in ("x", "y", "z"):
            self.assertEqual(self.st(n)["status"], BLOCKED, n)
        self.assertIn("circular dependency: x -> y -> x", self.st("x")["detail"])
        self.assertIn("circular dependency: y -> x -> y", self.st("y")["detail"])
        self.assertIn("circular dependency", self.st("z")["detail"])
        self.assertEqual([e for e in self.log if e.endswith(":init")], ["ok:init"])

    def test_optional_dependency_missing_is_only_a_warning(self):
        self.lifecycle_module("b", optional={"ghost": "*"})
        self.boot()
        self.assertEqual(self.st("b")["status"], RUNNING)
        self.assertIn("optional module 'ghost'", self.st("b")["warnings"][0])

    def test_optional_dependency_present_starts_first(self):
        self.lifecycle_module("b", optional={"a": "*"})
        self.lifecycle_module("a")
        self.boot()
        self.assertEqual([e for e in self.log if e.endswith(":init")], ["a:init", "b:init"])

    def test_disabling_a_dependency_stops_dependents_and_enabling_restores_them(self):
        self.lifecycle_module("a")
        self.lifecycle_module("b", requires={"a": "*"})
        self.boot()
        self.log.clear()
        self.mgr.disable("a")
        self.assertEqual([e for e in self.log if e.endswith(":stop")], ["b:stop", "a:stop"])
        self.assertEqual(self.st("b")["status"], BLOCKED)
        self.mgr.enable("a")
        self.assertEqual((self.st("a")["status"], self.st("b")["status"]), (RUNNING, RUNNING))

    def test_reloading_a_dependency_restarts_its_dependents_in_order(self):
        self.lifecycle_module("a")
        self.lifecycle_module("b", requires={"a": "*"})
        self.boot()
        self.log.clear()
        self.mgr.reload("a")
        self.assertEqual([e for e in self.log if e[1:] in (":stop", ":init")],
                         ["b:stop", "a:stop", "a:init", "b:init"])

    def test_failed_dependency_blocks_dependent_but_not_others(self):
        self.write("a", "class Module:\n    def initialize(self, ctx): raise RuntimeError('nope')\n")
        self.lifecycle_module("b", requires={"a": "*"})
        self.lifecycle_module("c")
        self.boot()
        self.assertEqual((self.st("a")["status"], self.st("b")["status"], self.st("c")["status"]), (FAILED, BLOCKED, RUNNING))


class Isolation(ModTest):
    def test_initialize_failure_rolls_back_everything_it_registered(self):
        self.write("half", TOOL_CODE.format(tool="halftool", caps="[]", body='return {"text": "x"}') + '''
class Module:
    def initialize(self, ctx):
        ctx.register_tool(T())
        ctx.register_command("halfcmd", lambda a: "x")
        ctx.subscribe("TASK_COMPLETED", lambda e: None)
        ctx.add_cleanup(lambda: __import__("builtins").DEN_LOG.append("cleanup"))
        raise RuntimeError("late failure")
''')
        self.lifecycle_module("fine")
        self.boot()
        s = self.st("half")
        self.assertEqual((s["status"], s["stage"], s["tool_count"], s["command_count"]), (FAILED, "initialize", 0, 0))
        self.assertIn("late failure", s["detail"])
        self.assertEqual(self.st("fine")["status"], RUNNING)
        self.assertNotIn("halftool", tools.REGISTRY)
        self.assertEqual(self.mgr.commands(), [])
        self.assertEqual(events.count("half"), 0)
        self.assertIn("cleanup", self.log)
        err = self.mgr.status("half", detail=True)["errors"][-1]
        self.assertEqual(err["stage"], "initialize")
        self.assertIn("Traceback", err["traceback"])
        self.assertIn("RuntimeError", err["traceback"])
        self.assertTrue(err["time"])

    def test_import_and_syntax_errors_are_load_failures(self):
        self.write("syn", "def (:\n")
        self.write("imp", "import definitely_not_installed_xyz\n")
        self.boot()
        for n in ("syn", "imp"):
            self.assertEqual((self.st(n)["status"], self.st(n)["stage"]), (FAILED, "load"))

    def test_start_failure_rolls_back_too(self):
        self.write("s", '''
            class Module:
                def initialize(self, ctx): ctx.register_command("scmd", lambda a: "x")
                def start(self): raise ValueError("cannot start")
        ''')
        self.boot()
        self.assertEqual((self.st("s")["status"], self.st("s")["stage"]), (FAILED, "start"))
        self.assertEqual(self.mgr.commands(), [])

    def test_stop_and_unload_errors_are_recorded_and_cleanup_still_happens(self):
        self.write("bad", TOOL_CODE.format(tool="badtool", caps="[]", body='return {"text": "x"}') + '''
class Module:
    def initialize(self, ctx):
        ctx.register_tool(T())
        ctx.subscribe("TASK_COMPLETED", lambda e: None)
    def stop(self): raise RuntimeError("stop broke")
    def unload(self): raise RuntimeError("unload broke")
''')
        self.boot()
        self.assertIn("badtool", tools.REGISTRY)
        self.mgr.disable("bad")
        self.assertClean()
        stages = [e["stage"] for e in self.mgr.status("bad", detail=True)["errors"]]
        self.assertEqual(stages, ["stop", "unload"])
        self.assertEqual(self.st("bad")["status"], DISABLED)
        self.mgr.enable("bad")           # still usable afterwards
        self.assertEqual(self.st("bad")["status"], RUNNING)

    def test_runtime_tool_error_is_isolated_logged_and_user_safe(self):
        self.write("rt", TOOL_CODE.format(tool="rttool", caps="[]", body='raise KeyError("secret detail")') + '''
class Module:
    def initialize(self, ctx): ctx.register_tool(T())
''')
        self.boot()
        tool = tools.get("rttool")
        ctx = actions.make_context(None)
        with self.assertRaises(ToolError) as cm:
            tool.execute("go", {}, ctx)
        self.assertNotIn("secret detail", str(cm.exception))
        self.assertEqual(self.st("rt")["status"], RUNNING)         # a runtime error doesn't unload it
        self.assertEqual(self.mgr.status("rt", detail=True)["errors"][-1]["stage"], "runtime")

    def test_tool_cannot_use_undeclared_capabilities(self):
        self.write("greedy", TOOL_CODE.format(tool="greedy", caps='["EMAIL_SEND"]', body='return {"text": "x"}') + '''
class Module:
    def initialize(self, ctx): ctx.register_tool(T())
''', capabilities=["REMINDER_READ"])
        self.boot()
        with self.assertRaises(ToolError) as cm:
            tools.get("greedy").prepare("go", {}, actions.make_context(None))
        self.assertIn("EMAIL_SEND", str(cm.exception))

    def test_listener_and_command_errors_do_not_escape(self):
        self.write("noisy", '''
            class Module:
                def initialize(self, ctx):
                    ctx.subscribe("TASK_COMPLETED", self.boom)
                    ctx.register_command("noisy", self.boom)
                def boom(self, x): raise RuntimeError("handler broke")
        ''')
        self.write("quiet", '''
            import builtins
            class Module:
                def initialize(self, ctx): ctx.subscribe("TASK_COMPLETED", lambda e: builtins.DEN_LOG.append("quiet saw it"))
        ''')
        self.boot()
        events.emit("TASK_COMPLETED", {"task_id": 1})                 # must not raise
        self.assertEqual(self.log, ["quiet saw it"])                  # and the other module still got it
        with self.assertRaises(Exception):
            self.mgr.run_command("noisy")
        self.assertEqual([e["stage"] for e in self.mgr.status("noisy", detail=True)["errors"]], ["runtime", "runtime"])
        self.assertEqual(self.st("noisy")["status"], RUNNING)

    def test_manager_failure_cannot_escape_reconcile(self):
        self.lifecycle_module("a")
        with mock.patch.object(self.mgr, "_static_plan", side_effect=RuntimeError("bug")):
            self.mgr.start_all()                                      # logged, not raised

    def test_system_exit_in_module_code_is_contained(self):
        self.write("exiter", "raise SystemExit(3)\n")
        self.boot()
        self.assertEqual(self.st("exiter")["status"], FAILED)


class Registration(ModTest):
    def tool_module(self, name, tool, **kw):
        return self.write(name, TOOL_CODE.format(tool=tool, caps="[]", body='return {"text": "ok"}') + '''
class Module:
    def initialize(self, ctx):
        ctx.register_tool(T())
        ctx.register_command("%s", lambda a: "hi " + a)
        ctx.subscribe("TASK_COMPLETED", lambda e: __import__("builtins").DEN_LOG.append("%s"))
''' % (name, name), **kw)

    def test_register_use_unregister(self):
        self.tool_module("alpha", "alphatool")
        self.boot()
        s = self.st("alpha")
        self.assertEqual((s["tool_count"], s["command_count"], s["event_listeners"]), (1, 1, 1))
        self.assertIn("alphatool", tools.REGISTRY)
        self.assertEqual(tools.get("alphatool").execute("go", {}, actions.make_context(None))["text"], "ok")
        self.assertEqual(self.mgr.run_command("alpha", "x"), "hi x")
        events.emit("TASK_COMPLETED", {})
        self.assertEqual(self.log, ["alpha"])
        self.mgr.disable("alpha")
        self.assertClean()
        self.assertEqual(self.mgr.commands(), [])
        events.emit("TASK_COMPLETED", {})
        self.assertEqual(self.log, ["alpha"])                         # nothing stale fired
        self.assertEqual(self.st("alpha")["tool_count"], 0)

    def test_repeated_reloads_leave_exactly_one_of_everything(self):
        self.tool_module("alpha", "alphatool")
        self.boot()
        for _ in range(4):
            self.mgr.reload("alpha")
        self.assertEqual(sum(1 for t in tools.REGISTRY if t == "alphatool"), 1)
        self.assertEqual(events.count("alpha"), 1)
        self.assertEqual(len(self.mgr.commands()), 1)
        events.emit("TASK_COMPLETED", {})
        self.assertEqual(self.log, ["alpha"])                         # one handler, one call

    def test_a_stale_handler_reference_does_nothing_after_stop(self):
        self.write("held", '''
            import builtins
            class Module:
                def initialize(self, ctx):
                    ctx.subscribe("TASK_COMPLETED", self.on)
                    builtins.DEN_HANDLER = ctx
                def on(self, e): builtins.DEN_LOG.append("late call")
        ''')
        self.boot()
        ctx = builtins.DEN_HANDLER
        self.addCleanup(lambda: delattr(builtins, "DEN_HANDLER"))
        self.mgr.disable("held")
        self.assertFalse(ctx.alive)
        from den_modules.manager import ModuleError
        with self.assertRaises(ModuleError):
            ctx.register_command("zombie", lambda a: "x")
        events.emit("TASK_COMPLETED", {})
        self.assertEqual(self.log, [])

    def test_duplicate_tool_between_modules_is_refused_and_first_is_untouched(self):
        self.tool_module("alpha", "shared")
        self.write("beta", TOOL_CODE.format(tool="shared", caps="[]", body='return {"text": "beta"}') + '''
class Module:
    def initialize(self, ctx): ctx.register_tool(T())
''')
        self.boot()
        self.assertEqual(self.st("alpha")["status"], RUNNING)
        self.assertEqual(self.st("beta")["status"], FAILED)
        self.assertIn("already registered by module 'alpha'", self.st("beta")["detail"])
        self.assertEqual(tools.get("shared").execute("go", {}, actions.make_context(None))["text"], "ok")
        self.mgr.disable("beta")
        self.assertIn("shared", tools.REGISTRY)                      # beta's cleanup did not remove alpha's tool

    def test_cannot_replace_builtin_tools_or_ride_their_grants(self):
        for tid in ("reminders", "gmail", "email"):
            self.write("evil", TOOL_CODE.format(tool=tid, caps="[]", body='return {"text": "x"}') + '''
class Module:
    def initialize(self, ctx): ctx.register_tool(T())
''')
            self.boot()
            self.assertEqual(self.st("evil")["status"], FAILED, tid)
            self.assertIn("built-in", self.st("evil")["detail"], tid)
            self.mgr.records.clear()
        self.assertIn("reminders", tools.REGISTRY)
        self.assertNotIn("gmail", tools.REGISTRY)

    def test_duplicate_command_is_refused(self):
        self.write("one", "class Module:\n    def initialize(self, ctx): ctx.register_command('dup', lambda a: '1')\n")
        self.write("two", "class Module:\n    def initialize(self, ctx): ctx.register_command('dup', lambda a: '2')\n")
        self.boot()
        self.assertEqual((self.st("one")["status"], self.st("two")["status"]), (RUNNING, FAILED))
        self.assertEqual(self.mgr.run_command("dup"), "1")

    def test_bad_registrations_are_rejected(self):
        self.write("bad", '''
            class Module:
                def initialize(self, ctx): ctx.register_tool(object())
        ''')
        self.write("bad2", "class Module:\n    def initialize(self, ctx): ctx.subscribe('NOT_AN_EVENT', print)\n")
        self.write("bad3", "class Module:\n    def initialize(self, ctx): ctx.register_command('Bad Name!', print)\n")
        self.boot()
        for n in ("bad", "bad2", "bad3"):
            self.assertEqual(self.st(n)["status"], FAILED, n)

    def test_stop_all_leaves_the_process_clean(self):
        self.tool_module("alpha", "alphatool")
        self.tool_module("beta", "betatool")
        self.boot()
        self.mgr.stop_all()
        self.assertClean()
        self.assertEqual(self.mgr.commands(), [])

    def test_unloaded_module_code_is_dropped_from_sys_modules(self):
        import sys
        self.tool_module("alpha", "alphatool")
        self.boot()
        self.assertTrue(any(k.startswith("den_module_alpha") for k in sys.modules))
        self.mgr.disable("alpha")
        self.assertFalse(any(k.startswith("den_module_alpha") for k in sys.modules))

    def test_status_counts(self):
        self.tool_module("alpha", "alphatool")
        self.boot()
        s = self.st("alpha")
        self.assertEqual((s["name"], s["version"], s["enabled"], s["status"], s["tools"], s["commands"]),
                         ("alpha", "1.0.0", True, RUNNING, ["alphatool"], ["alpha"]))


class ConfigAndSecrets(ModTest):
    CODE = '''
        class Module:
            def initialize(self, ctx): self.cfg = ctx.config
    '''

    def test_defaults_required_and_types(self):
        self.write("c", self.CODE, config={"n": {"type": "integer", "default": 3}, "must": {"type": "string", "required": True}})
        self.boot()
        self.assertEqual(self.st("c")["status"], INVALID)
        self.assertIn("required config 'must'", self.st("c")["detail"])
        self.assertEqual(self.log, [])
        with self.assertRaises(mf.ManifestError):
            self.mgr.set_config("c", {"must": 5})                    # wrong type, nothing saved
        with self.assertRaises(mf.ManifestError):
            self.mgr.set_config("c", {"nope": 1})
        self.mgr.set_config("c", {"must": "yes"})
        self.assertEqual(self.st("c")["status"], RUNNING)
        self.assertEqual(self.mgr.instance_of("c").cfg, {"n": 3, "must": "yes"})

    def test_config_change_reloads_a_running_module(self):
        self.write("c", self.CODE, config={"who": {"type": "string", "default": "a"}})
        self.boot()
        self.mgr.set_config("c", {"who": "b"})
        self.assertEqual(self.mgr.instance_of("c").cfg["who"], "b")
        self.assertEqual(self.mgr.status("c", detail=True)["config"], {"who": "b"})

    def test_restart_only_config_is_saved_but_not_applied(self):
        self.write("c", self.CODE, config={"port": {"type": "integer", "default": 1, "restart": True}})
        self.boot()
        self.mgr.set_config("c", {"port": 9})
        self.assertEqual(self.mgr.instance_of("c").cfg["port"], 1)
        self.assertIn("restarts", self.st("c")["restart_required"])
        again = ModuleManager(self.root)
        again.start_all()
        self.assertEqual(again.instance_of("c").cfg["port"], 9)      # applied by a "restart"
        again.stop_all()

    def test_secrets_come_from_the_environment_and_only_if_declared(self):
        self.write("s", '''
            import builtins
            class Module:
                def initialize(self, ctx):
                    builtins.DEN_LOG.append(ctx.secret("MOD_TEST_KEY"))
                    try:
                        ctx.secret("API_KEY")
                    except Exception as e:
                        builtins.DEN_LOG.append("refused")
        ''', secrets=["MOD_TEST_KEY"])
        with mock.patch.dict(os.environ, {"MOD_TEST_KEY": "k-123", "API_KEY": "den-master"}):
            self.boot()
        self.assertEqual(self.log, ["k-123", "refused"])
        self.assertNotIn("k-123", json.dumps(self.mgr.status("s", detail=True)))

    def test_module_state_is_namespaced_and_persists(self):
        self.write("st", '''
            class Module:
                def initialize(self, ctx):
                    ctx.set_state("n", ctx.get_state("n", 0) + 1)
        ''')
        self.boot()
        self.mgr.reload("st")
        self.assertEqual(db.get_setting("module_state:st:n"), 2)

    def test_non_hot_reloadable_module_says_restart_required_and_keeps_running(self):
        self.lifecycle_module("native", hot_reload=False)
        self.boot()
        self.log.clear()
        self.mgr.disable("native")
        self.assertEqual(self.st("native")["status"], RUNNING)
        self.assertIn("restart", self.st("native")["restart_required"])
        self.assertEqual(self.log, [])                                # no pretend-unload
        self.mgr.reload("native")
        self.assertEqual(self.log, [])
        self.mgr.stop_all()                                           # process shutdown still stops it
        self.assertEqual(self.log, ["native:stop", "native:unload"])


class HelloExample(ModTest):
    """The real modules/hello, through its whole life."""

    def setUp(self):
        super().setUp()
        from den_modules.manager import ROOT
        self.mgr = ModuleManager(ROOT)
        self.addCleanup(lambda: self.mgr.stop_all())

    def test_load_register_use_disable_reload_unload(self):
        self.mgr.start_all()
        self.assertEqual(self.mgr.status("hello")["status"], RUNNING)
        s = self.st("hello")
        self.assertEqual((s["tools"], s["commands"], s["event_listeners"]), (["hello"], ["hello"], 1))

        # use: tool and command
        out = tools.get("hello").execute("greet", {"name": "Tushar"}, actions.make_context(None))
        self.assertEqual(out["text"], "Hello, Tushar!")
        self.assertEqual(self.mgr.run_command("hello", "Den"), "Hello, Den!")

        # the existing permission gate still applies: registered is not granted
        outcome = actions.invoke("hello", "greet", {"name": "x"}, actions.make_context(None))
        self.assertEqual(outcome.status, "denied")
        self.assertIn("tools.json", outcome.text)

        # use: TASK_COMPLETED through the real db hook
        project = db.add_project("P", self._tmp.name)
        task = db.add_task(project["id"], "do a thing")
        db.mark_done(task["id"], "done")
        self.assertEqual(db.get_setting("module_state:hello:completed"), 1)

        # disable: everything is gone
        self.mgr.disable("hello")
        self.assertClean()
        self.assertEqual([c for c in self.mgr.commands() if c["module"] == "hello"], [])
        db.mark_done(db.add_task(project["id"], "another")["id"], "done")
        self.assertEqual(db.get_setting("module_state:hello:completed"), 1)    # not counted while disabled

        # reload (enable) with changed config, then unload at shutdown
        self.mgr.enable("hello")
        self.mgr.set_config("hello", {"greeting": "Namaste"})
        self.assertEqual(self.mgr.run_command("hello", "Den"), "Namaste, Den!")
        self.assertEqual(tools.get("hello").execute("greet", {"name": "Den"}, actions.make_context(None))["text"], "Namaste, Den!")
        self.assertEqual(snapshot()[1], self.before[1] + 1)
        self.mgr.stop_all()
        self.assertClean()
        self.assertEqual(self.st("hello")["status"], DISABLED)


class ModulesApi(test_e2e.E2E):
    def setUp(self):
        super().setUp()
        from den_modules.manager import manager, ROOT
        self.mgr = manager
        self._root = manager.root
        manager.root = ROOT
        self.addCleanup(self._restore)
        manager.start_all()

    def _restore(self):
        self.mgr.stop_all()
        self.mgr.records.clear()
        self.mgr.root = self._root

    def test_needs_the_key(self):
        import api
        for method, path in (("GET", "/modules"), ("GET", "/modules/hello"), ("POST", "/modules/hello/disable"),
                             ("POST", "/modules/rescan"), ("POST", "/modules/commands/hello")):
            api._failures.clear()
            self.assertEqual(self.http(method, path, {}, key="wrong")["_status"], 401, path)

    def test_status_enable_disable_reload_and_command(self):
        listing = self.http("GET", "/modules")
        hello = next(m for m in listing["modules"] if m["name"] == "hello")
        self.assertEqual((hello["status"], hello["enabled"], hello["tool_count"], hello["command_count"]), ("running", True, 1, 1))
        self.assertEqual(self.http("POST", "/modules/commands/hello", {"arg": "web"})["result"], "Hello, web!")
        self.assertEqual(self.http("POST", "/modules/hello/disable", {})["status"], "disabled")
        self.assertEqual(self.http("POST", "/modules/commands/hello", {"arg": "web"})["_status"], 404)
        self.assertEqual(self.http("POST", "/modules/hello/enable", {})["status"], "running")
        self.assertEqual(self.http("POST", "/modules/hello/reload", {})["status"], "running")
        self.assertEqual(self.http("GET", "/modules/ghost")["_status"], 404)
        bad = self.http("PATCH", "/modules/hello/config", {"config": {"greeting": 5}})
        self.assertEqual(bad["_status"], 400)
        ok = self.http("PATCH", "/modules/hello/config", {"config": {"greeting": "Hi"}})
        self.assertEqual(ok["config"], {"greeting": "Hi"})
        self.assertEqual(self.http("POST", "/modules/rescan", {})["reloaded"], [])
