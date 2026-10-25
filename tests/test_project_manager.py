"""modules/project_manager: the interview, brief, plan, approve and review steps, with a fake model."""

import importlib.util
import json
import logging
from unittest import mock

import db
import phases
import providers
from tests.helpers import ROOT, TestBase

_spec = importlib.util.spec_from_file_location("project_manager_module", ROOT / "modules/project_manager/module.py")
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)

PLAN = {"phases": [
    {"name": "Design", "goal": "Decide the look", "tasks": [{"role": "ui_designer", "text": "Write docs/design.md"}]},
    {"name": "Build", "goal": "Make the pages", "tasks": [{"role": "frontend", "text": "Build the home page"},
                                                          {"role": "qa_tester", "text": "Test the home page"}]},
]}


class FakeCtx:
    def __init__(self):
        self.config, self.state, self.commands = {"model": "", "max_questions": 3}, {}, {}
        self.log = logging.getLogger("test.project_manager")

    def get_state(self, key, default=None):
        value = self.state.get(key)
        return default if value is None else json.loads(json.dumps(value))

    def set_state(self, key, value):
        self.state[key] = json.loads(json.dumps(value))

    def register_command(self, name, fn, desc=""):
        self.commands[name] = fn


class ProjectManager(TestBase):
    def setUp(self):
        super().setUp()
        self.ctx = FakeCtx()
        self.mod = pm.Module()
        self.mod.initialize(self.ctx)
        self.pid = self.project["id"]
        self.replies = []
        fake = {"id": "fake", "provider": "openai"}
        patches = [mock.patch.object(self.mod, "model", return_value=fake),
                   mock.patch.object(providers, "complete", side_effect=self._complete)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _complete(self, model, messages, system=None, timeout=120):
        return providers.Completion(self.replies.pop(0), 10, 10)

    def run_cmd(self, name, **data):
        return self.ctx.commands[name](json.dumps({"project_id": self.pid, **data}))

    def to_plan(self):
        self.replies = ['{"ready": true}', "# Bakery\n## Goal\nSell cakes"]
        self.run_cmd("pm-chat", text="A website for my bakery")
        self.replies = [json.dumps(PLAN)]
        return self.run_cmd("pm-confirm-brief", brief="# Bakery\n## Goal\nSell cakes online")

    def test_interview_asks_then_writes_the_brief(self):
        self.replies = ['Sure! {"question": "Who will use it?", "suggestion": "Local customers"}']
        out = self.run_cmd("pm-chat", text="A website for my bakery")
        self.assertEqual(out["stage"], "interview")
        self.assertEqual(out["chat"][-1], {"role": "assistant", "text": "Who will use it?", "suggestion": "Local customers"})
        self.replies = ['{"ready": true}', "# Bakery brief"]
        out = self.run_cmd("pm-chat", text="Local customers")
        self.assertEqual((out["stage"], out["brief"]), ("brief", "# Bakery brief"))

    def test_question_limit_skips_straight_to_the_brief(self):
        self.ctx.config["max_questions"] = 1
        self.replies = ['{"question": "Pages?"}']
        self.run_cmd("pm-chat", text="A website")
        self.replies = ["# Brief"]               # no interview call: the limit is reached
        self.assertEqual(self.run_cmd("pm-chat", text="Home and menu")["stage"], "brief")

    def test_problems_come_back_as_errors(self):
        self.assertIn("error", self.run_cmd("pm-approve"))
        self.replies = ["no json here"]
        self.assertIn("expected shape", self.run_cmd("pm-chat", text="A website")["error"])
        self.assertEqual(self.mod.load(self.pid)["chat"], [])       # the answer wasn't lost half-way
        self.assertEqual(self.ctx.commands["pm-status"]("{}")["error"], 'Which project? Send {"project_id": n}')

    def test_confirm_saves_the_brief_and_drafts_the_plan(self):
        out = self.to_plan()
        self.assertEqual(out["stage"], "plan")
        self.assertEqual([p["name"] for p in out["plan"]["phases"]], ["Design", "Build"])
        notes = db.list_project_notes(self.pid)
        self.assertEqual(len(notes), 1)
        self.assertIn("Sell cakes online", notes[0]["text"])
        self.replies = [json.dumps(PLAN)]
        self.run_cmd("pm-confirm-brief", brief="# Bakery v2")        # confirming again updates the same note
        self.assertEqual([n["text"].endswith("# Bakery v2") for n in db.list_project_notes(self.pid)], [True])

    def test_plan_with_unknown_role_is_rejected(self):
        self.to_plan()
        bad = {"phases": [{"name": "X", "tasks": [{"role": "wizard", "text": "Magic"}]}]}
        self.assertIn("wizard", self.run_cmd("pm-save-plan", plan=bad)["error"])
        self.assertIn("no phases", self.run_cmd("pm-save-plan", plan={"phases": []})["error"])

    def test_approve_makes_phases_and_starts_the_first(self):
        self.to_plan()
        out = self.run_cmd("pm-approve")
        self.assertEqual(out["stage"], "building")
        made = db.list_phases(self.pid)
        self.assertEqual([p["name"] for p in made], ["Design", "Build"])
        self.assertEqual([p["status"] for p in made], ["active", "planned"])
        self.assertEqual(phases.mode_of(self.pid), "manual")
        todos = [t for t in db.list_todos(self.pid) if t["phase_id"] == made[0]["id"]]
        self.assertTrue(todos[0]["text"].startswith("[UI/UX designer] Write docs/design.md\n\nYou are the UI/UX designer."))
        self.assertEqual(todos[0]["status"], "queued")
        shown = out["phases"][0]["tasks"][0]                         # what the tab lists under the phase
        self.assertEqual((shown["role"], shown["text"], shown["state"]), ("UI/UX designer", "Write docs/design.md", "queued"))
        self.assertEqual(shown["task_id"], todos[0]["task_id"])

    def test_finished_phase_is_reviewed_once_then_next_starts(self):
        self.to_plan()
        self.run_cmd("pm-approve")
        first, second = (p["id"] for p in db.list_phases(self.pid))
        self.assertEqual(self.mod.check(), 0)                       # still running
        phases.complete(first)
        self.replies = ["The design is done. Next we build the pages."]
        self.assertEqual(self.mod.check(), 1)
        self.assertEqual(self.mod.check(), 0)                       # only once
        out = self.run_cmd("pm-status")
        self.assertEqual((out["stage"], out["review"]["text"]), ("review", "The design is done. Next we build the pages."))
        out = self.run_cmd("pm-next")
        self.assertEqual((out["stage"], out["current"]), ("building", second))
        self.assertEqual(db.get_phase(second)["status"], "active")

    def test_changes_reopen_the_phase_with_a_fix_up_task(self):
        self.to_plan()
        self.run_cmd("pm-approve")
        first = db.list_phases(self.pid)[0]["id"]
        phases.complete(first)
        self.replies = ["Done."]
        self.mod.check()
        out = self.run_cmd("pm-changes", note="Use bigger fonts")
        self.assertEqual(out["stage"], "building")
        self.assertEqual(db.get_phase(first)["status"], "active")
        fix = [t for t in db.list_todos(self.pid) if t["phase_id"] == first][-1]
        self.assertIn("[UI/UX designer] Changes the owner asked for after reviewing this phase: Use bigger fonts", fix["text"])

    def test_last_phase_review_finishes(self):
        self.to_plan()
        self.run_cmd("pm-approve")
        for i, p in enumerate(db.list_phases(self.pid)):
            phases.complete(p["id"])
            self.replies = ["Review."]
            self.mod.check()
            if i == 0:
                self.run_cmd("pm-next")
        self.assertEqual(self.run_cmd("pm-status")["stage"], "finished")
        self.assertEqual(self.run_cmd("pm-reset")["stage"], "interview")
