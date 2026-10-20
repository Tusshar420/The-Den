"""CRM 1b: projects created and kept in the CRM, with or without a folder.

A project with no folder is a full record (overview, phases, notes, documents, calendar) that can
never run anything: the API refuses to queue work for it, the worker refuses to start any (and never
falls back to another folder), and linking a folder later makes it workable. Rename, archive / hide,
and a hard delete behind the typed name."""

from tests.helpers import TestBase  # noqa: I001 - first, so no test sees real API keys

import asyncio
import os
from pathlib import Path

from fastapi import HTTPException

import api
import db
import documents
import pad
import phases
import worker
from octopus import service


class CrmBase(TestBase):
    def setUp(self):
        super().setUp()
        self._old_docs, documents.DOCUMENT_DIR = documents.DOCUMENT_DIR, Path(self._tmp.name) / "docs"
        self.addCleanup(lambda: setattr(documents, "DOCUMENT_DIR", self._old_docs))
        self._old_key, worker.ACTIVITYSMITH_API_KEY = worker.ACTIVITYSMITH_API_KEY, None   # no real pushes
        self.addCleanup(lambda: setattr(worker, "ACTIVITYSMITH_API_KEY", self._old_key))
        self.folder = Path(self._tmp.name) / "shop"
        self.folder.mkdir()
        pad._ready = False                     # pad makes its table on first use - in this test's database too

    def create(self, **body):
        return api.create_project(api.ProjectIn(**body))

    def crm(self, name="Acme rebrand", **body):
        return self.create(name=name, **body)

    def refused(self, fn, *a, status=400):
        with self.assertRaises(HTTPException) as cm:
            fn(*a)
        self.assertEqual(cm.exception.status_code, status, cm.exception.detail)
        return cm.exception.detail


class Create(CrmBase):
    def test_a_project_without_a_folder_is_a_full_crm_record(self):
        p = self.crm(category="personal", status="in_progress", description="New **brand**", priority="high",
                     health="at_risk", start_date="2026-10-01", due_date="2026-12-01", contract_value=4500,
                     currency="inr", billing_type="fixed", owner="Tushar", tags=["design", "Design", "q4"],
                     links=[{"label": "Brief", "url": "docs.example.com/brief"}])
        self.assertEqual(p["path"], "")
        self.assertFalse(db.has_folder(p))
        self.assertEqual((p["category"], p["status"], p["priority"], p["health"]), ("personal", "in_progress", "high", "at_risk"))
        self.assertEqual((p["contract_value"], p["currency"], p["billing_type"], p["owner"]), (4500, "INR", "fixed", "Tushar"))
        self.assertEqual(p["tags"], ["design", "q4"])
        self.assertEqual(p["links"], [{"label": "Brief", "url": "https://docs.example.com/brief"}])
        self.assertEqual(api.get_project(p["id"])["due_date"], "2026-12-01")

    def test_an_empty_or_blank_folder_means_no_folder(self):
        self.assertEqual(self.crm("a", path="")["path"], "")
        self.assertEqual(self.crm("b", path="   ")["path"], "")

    def test_with_a_folder_it_is_registered_as_before(self):
        p = self.create(name="shop", path=str(self.folder))
        self.assertEqual(p["path"], str(self.folder))
        self.assertTrue(db.has_folder(p))
        self.assertEqual(p["category"], "work")

    def test_a_missing_folder_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            self.create(name="x", path=str(self.folder / "missing"))
        self.assertIn("No folder at", cm.exception.detail)
        self.assertIsNone(db.get_project_by_name("x"))

    def test_a_bad_record_value_creates_nothing(self):
        for bad in ({"priority": "urgent"}, {"due_date": "31/12/2026"}, {"start_date": "2026-10-05", "due_date": "2026-10-01"},
                    {"status": "done"}, {"category": "office"}, {"contract_value": -5}):
            with self.assertRaises(HTTPException, msg=bad):
                self.crm("bad", **bad)
            self.assertIsNone(db.get_project_by_name("bad"), bad)

    def test_names_must_be_new_and_not_general(self):
        self.crm("Acme")
        with self.assertRaises(HTTPException) as cm:
            self.crm("Acme")
        self.assertEqual(cm.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            self.crm("General")
        with self.assertRaises(HTTPException):
            self.crm("  ")


class NeverRuns(CrmBase):
    def setUp(self):
        super().setUp()
        self.p = self.crm()

    def test_no_task_can_be_queued(self):
        detail = self.refused(api.create_task, self.p["id"], api.TaskIn(prompt="build it"))
        self.assertEqual(detail, db.NO_FOLDER_HINT)
        self.refused(api.create_task, self.p["id"], api.TaskIn(prompt="what's left?", kind="message"))
        self.refused(api.suggest_improvements, self.p["id"])
        self.assertEqual(db.list_tasks(project_id=self.p["id"]), [])

    def test_backlog_run_is_refused_but_the_backlog_is_kept(self):
        t = db.add_todo(self.p["id"], "write copy")
        db.update_todo(t["id"], checked=True)
        self.refused(api.run_checked_todos, self.p["id"])
        self.assertEqual(db.list_tasks(project_id=self.p["id"]), [])
        self.assertEqual([x["text"] for x in db.list_todos(self.p["id"])], ["write copy"])

    def test_phases_are_for_planning_only(self):
        ph = db.add_phase(self.p["id"], "Discovery")
        db.add_todo(self.p["id"], "interview client")
        phases.assign_todo(db.list_todos(self.p["id"])[0]["id"], ph["id"])
        with self.assertRaises(phases.PhaseError) as cm:
            phases.start(ph["id"])
        self.assertEqual(str(cm.exception), db.NO_FOLDER_HINT)
        self.refused(api.start_phase, ph["id"])
        with self.assertRaises(phases.PhaseError):
            phases.set_mode(self.p["id"], "capacity")
        self.refused(api.update_project_settings, self.p["id"], api.ProjectSettingsIn(autopilot="timeline"))
        phases.set_mode(self.p["id"], "manual")                    # "on my command" is fine - nothing runs
        # Autopilot doesn't even look at it, and an active phase queues nothing.
        self.assertNotIn(self.p["id"], [x["id"] for x in db.projects_with_open_phases()])
        db.update_phase(ph["id"], status="active")
        self.assertEqual(phases.advance(db.get_phase(ph["id"]), force=True), [])
        self.assertEqual(db.list_tasks(project_id=self.p["id"]), [])
        # Planning still works: Mark done.
        phases.complete(ph["id"])
        self.assertEqual(db.get_phase(ph["id"])["status"], "done")

    def test_recipes_skip_it(self):
        shop = db.add_project("shop", str(self.folder))
        r = db.add_recipe("Update deps", "update the dependencies", target="all")
        out = api.run_recipe(r["id"], api.RecipeRunIn())
        self.assertEqual({t["project_id"] for t in out["tasks"]}, {shop["id"]})
        one = db.add_recipe("Audit", "audit", target=str(self.p["id"]))
        self.refused(api.run_recipe, one["id"], api.RecipeRunIn())

    def test_retry_is_refused(self):
        t = db.add_task(self.p["id"], "sneaked in", lane="agent", model_id="claude-code")
        db.mark_error(t["id"], "x", needs_review=False)
        self.refused(api.requeue_task, t["id"], api.RequeueIn())
        self.assertEqual(db.get_task(t["id"])["status"], "error")

    def test_octopus_requests_for_it_are_refused(self):
        with self.assertRaises(service.ServiceError) as cm:
            service.submit("fix the header", project_id=self.p["id"])
        self.assertEqual((cm.exception.status, cm.exception.detail), (400, db.NO_FOLDER_HINT))

    def test_schedules_and_scheduled_prompts_skip_it(self):
        self.assertIsNone(worker.queue_prompt(self.p, "daily check"))
        self.assertEqual(db.list_tasks(project_id=self.p["id"]), [])

    def test_pad_save_never_falls_back_to_another_folder(self):
        note = pad.create_note("n", "body")
        self.refused(api.pad_save_file, note["id"], api.PadSaveIn(project_id=self.p["id"], filename="n.md"))


class WorkerRefuses(CrmBase):
    """Even a task that got in some other way is never run - and never anywhere else."""

    def setUp(self):
        super().setUp()
        self.p = self.crm()
        self.started = []

        async def no_claude(*a, **k):
            self.started.append(a)
            raise AssertionError("Claude Code was started for a project without a folder")
        self._old_run, worker.run_claude_task = worker.run_claude_task, no_claude
        self.addCleanup(lambda: setattr(worker, "run_claude_task", self._old_run))
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def test_agent_lane_marks_it_as_an_error(self):
        t = db.add_task(self.p["id"], "build it", lane="agent", model_id="claude-code")
        self.loop.run_until_complete(worker.run_agent_task(db.get_task(t["id"])))
        got = db.get_task(t["id"])
        self.assertEqual(got["status"], "error")
        self.assertEqual(got["error_message"], worker.NO_FOLDER_ERROR)
        self.assertIn(db.NO_FOLDER_HINT, got["error_message"])
        self.assertIsNone(got["started_at"])
        self.assertEqual(self.started, [])

    def test_api_lane_marks_it_as_an_error(self):
        import router
        model = next(m for m in router.registry()["models"] if router.lane_for(m) == "api")
        t = db.add_task(self.p["id"], "summarise", lane="api", model_id=model["id"])
        self.loop.run_until_complete(asyncio.sleep(0))

        async def tick():
            return worker.start_ready_api_tasks()
        self.assertEqual(self.loop.run_until_complete(tick()), [])
        self.assertEqual(db.get_task(t["id"])["status"], "error")
        self.assertEqual(db.get_task(t["id"])["error_message"], worker.NO_FOLDER_ERROR)

    def test_claude_is_never_started_with_an_empty_folder(self):
        worker.run_claude_task = self._old_run

        async def no_process(*a, **k):
            raise AssertionError("a process was started")
        old, asyncio.create_subprocess_exec = asyncio.create_subprocess_exec, no_process
        self.addCleanup(lambda: setattr(asyncio, "create_subprocess_exec", old))
        t = db.add_task(self.p["id"], "x", lane="agent", model_id="claude-code")
        for path in ("", "  "):
            result = self.loop.run_until_complete(worker.run_claude_task("x", path, t["id"]))
            self.assertFalse(result.success)
            self.assertEqual(result.error, worker.NO_FOLDER_ERROR)

    def test_a_project_with_a_folder_still_runs(self):
        shop = db.add_project("shop", str(self.folder))
        ran = []

        async def fake(prompt, path, task_id, *a, **k):
            ran.append(path)
            return worker.TaskResult(True, summary="done")
        worker.run_claude_task = fake
        t = db.add_task(shop["id"], "build it", lane="agent", model_id="claude-code")
        self.loop.run_until_complete(worker.run_agent_task(db.get_task(t["id"])))
        self.assertEqual(ran, [str(self.folder)])
        self.assertEqual(db.get_task(t["id"])["status"], "done")


class LinkFolder(CrmBase):
    def setUp(self):
        super().setUp()
        self.p = self.crm()

    def link(self, path):
        return api.link_project_folder(self.p["id"], api.ProjectFolderIn(path=path))

    def test_linking_a_folder_makes_it_workable(self):
        self.refused(self.link, str(self.folder / "missing"))
        self.assertEqual(db.get_project(self.p["id"])["path"], "")
        r = self.link(str(self.folder))
        self.assertEqual(r["path"], str(self.folder))
        self.assertEqual(r["description"], "")                 # the record comes back whole
        t = api.create_task(self.p["id"], api.TaskIn(prompt="build it"))
        self.assertEqual(t["project_id"], self.p["id"])

    def test_tilde_paths_expand_like_registering(self):
        home = Path.home()
        r = self.link("~")
        self.assertEqual(r["path"], str(home))

    def test_a_linked_folder_can_be_changed_when_idle(self):
        self.link(str(self.folder))
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        self.assertEqual(self.link(str(other))["path"], str(other))
        api.create_task(self.p["id"], api.TaskIn(prompt="busy"))
        self.refused(self.link, str(self.folder), status=409)       # a queued task blocks the move
        self.assertEqual(db.get_project(self.p["id"])["path"], str(other))

    def test_unknown_project(self):
        with self.assertRaises(HTTPException) as cm:
            api.link_project_folder(999, api.ProjectFolderIn(path=str(self.folder)))
        self.assertEqual(cm.exception.status_code, 404)


class EditRecord(CrmBase):
    def setUp(self):
        super().setUp()
        self.p = self.crm()

    def patch(self, pid=None, **fields):
        return api.update_project(pid or self.p["id"], api.ProjectActiveIn(**fields))

    def test_every_field_is_editable(self):
        r = self.patch(name="Acme 2027", category="personal", status="on_hold", priority="critical", owner="Asha")
        self.assertEqual((r["name"], r["category"], r["status"], r["priority"], r["owner"]),
                         ("Acme 2027", "personal", "on_hold", "critical", "Asha"))

    def test_rename_checks(self):
        db.add_project("Taken", "")
        for bad, status in (("Taken", 409), ("General", 409), ("   ", 400)):
            with self.assertRaises(HTTPException) as cm:
                self.patch(name=bad)
            self.assertEqual(cm.exception.status_code, status, bad)
        self.assertEqual(db.get_project(self.p["id"])["name"], "Acme rebrand")
        self.assertEqual(self.patch(name="Acme rebrand")["name"], "Acme rebrand")   # unchanged is fine
        with self.assertRaises(HTTPException):
            self.patch(pid=self.project["id"], name="Renamed")                      # General keeps its name

    def test_archive_and_hide_keep_everything(self):
        db.add_project_note(self.p["id"], "kept")
        self.patch(status="archived")
        self.patch(hidden=True)
        p = db.get_project(self.p["id"])
        self.assertEqual((p["status"], p["hidden"]), ("archived", 1))
        self.assertEqual(len(db.list_project_notes(self.p["id"])), 1)


class HardDelete(CrmBase):
    def setUp(self):
        super().setUp()
        self.p = self.crm(description="to go")

    def delete(self, confirm, pid=None):
        return api.delete_project(pid or self.p["id"], api.ProjectDeleteIn(confirm=confirm))

    def test_needs_the_name_typed_exactly(self):
        for wrong in ("", "acme rebrand", "Acme rebrand ", "yes"):
            self.refused(self.delete, wrong)
        self.assertIsNotNone(db.get_project(self.p["id"]))

    def test_removes_the_project_and_what_is_only_about_it(self):
        pid = self.p["id"]
        db.add_project_note(pid, "a note")
        db.add_todo(pid, "a to-do")
        db.add_phase(pid, "Phase 1")
        db.add_project_event(pid, "deadline", "Launch", "2026-11-01")
        db.update_project_settings(pid, default_priority=1)
        doc = documents.save(pid, "brief.txt", "text/plain", b"hello")
        stored = documents.path_of({**doc, "stored": db.list_project_documents(pid, with_stored=True)[0]["stored"]})
        self.assertTrue(stored.exists())
        note = pad.create_note("scratch", "keep me", project_id=pid)
        refused = db.add_task(pid, "never ran", lane="agent", model_id="claude-code")
        db.mark_error(refused["id"], worker.NO_FOLDER_ERROR, needs_review=False)

        self.assertEqual(self.delete("Acme rebrand"), {"ok": True, "id": pid})
        self.assertIsNone(db.get_project(pid))
        self.assertEqual((db.list_project_notes(pid), db.list_todos(pid), db.list_phases(pid),
                          db.list_project_documents(pid)), ([], [], [], []))
        self.assertIsNone(db.get_task(refused["id"]))
        self.assertFalse(stored.exists())
        self.assertIsNone(pad.get_note(note["id"])["project_id"])      # the scratch pad note stays
        with db._lock:
            self.assertEqual(db.get_conn().execute("SELECT COUNT(*) FROM project_events WHERE project_id = ?", (pid,)).fetchone()[0], 0)
        self.assertIsNotNone(db.get_project(self.project["id"]))       # nothing else touched

    def test_a_project_octopus_worked_on_cant_be_deleted(self):
        shop = db.add_project("shop", str(self.folder))
        t = db.add_task(shop["id"], "did work", lane="agent", model_id="claude-code")
        db.claim_task(t["id"])
        db.mark_done(t["id"], "ok")
        detail = self.refused(self.delete, "shop", shop["id"], status=409)
        self.assertIn("archive or hide", detail)
        self.assertIsNotNone(db.get_project(shop["id"]))
        self.assertTrue(self.folder.is_dir())

    def test_queued_work_blocks_it_too(self):
        shop = db.add_project("shop", str(self.folder))
        db.add_task(shop["id"], "waiting", lane="agent", model_id="claude-code")
        self.refused(self.delete, "shop", shop["id"], status=409)

    def test_general_cant_be_deleted(self):
        self.refused(self.delete, "General", self.project["id"])

    def test_deleting_a_folder_project_never_touches_the_folder(self):
        (self.folder / "keep.txt").write_text("x")
        shop = db.add_project("shop", str(self.folder))
        self.delete("shop", shop["id"])
        self.assertTrue((self.folder / "keep.txt").exists())


class ExistingProjects(CrmBase):
    def test_lists_and_records_still_work(self):
        shop = db.add_project("shop", str(self.folder))
        crm_p = self.crm()
        listed = {p["name"]: p for p in api.list_projects()}
        self.assertEqual(listed["shop"]["path"], str(self.folder))
        self.assertEqual(listed["Acme rebrand"]["path"], "")
        self.assertTrue(db.has_folder(db.get_project(shop["id"])))
        self.assertFalse(db.has_folder(db.get_project(crm_p["id"])))
        self.assertFalse(db.has_folder(None))
        self.assertEqual(db.task_workdir({"env_id": None}, db.get_project(shop["id"])), str(self.folder))
        self.assertTrue(os.path.isdir(db.get_project(self.project["id"])["path"]))   # General keeps working
