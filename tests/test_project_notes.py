"""Project notes: what the user tells Octopus about a project, and that every run for it gets them."""

from pathlib import Path

from fastapi import HTTPException

import api
import db
import uploads
from octopus import execution
from tests.helpers import TestBase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class ProjectNotes(TestBase):
    def setUp(self):
        super().setUp()
        self.app = db.add_project("shop", self._tmp.name)
        self.pid = self.app["id"]

    def test_notes_are_kept_per_project_in_the_order_added(self):
        other = db.add_project("blog", self._tmp.name + "/blog")
        db.add_project_note(self.pid, "Use TypeScript", "guideline")
        db.add_project_note(self.pid, "Must work offline", "requirement")
        db.add_project_note(other["id"], "Not for shop")
        self.assertEqual([n["text"] for n in db.list_project_notes(self.pid)], ["Use TypeScript", "Must work offline"])

    def test_unknown_kind_is_a_plain_note(self):
        self.assertEqual(db.add_project_note(self.pid, "x", "bogus")["kind"], "note")

    def test_edit_and_delete(self):
        n = db.add_project_note(self.pid, "old", "note")
        db.update_project_note(n["id"], text="new", kind="requirement")
        self.assertEqual((db.get_project_note(n["id"])["text"], db.get_project_note(n["id"])["kind"]), ("new", "requirement"))
        db.delete_project_note(n["id"])
        self.assertIsNone(db.get_project_note(n["id"]))

    def test_prompt_groups_by_kind_and_is_empty_without_notes(self):
        self.assertEqual(db.project_notes_prompt(self.pid), "")
        db.add_project_note(self.pid, "Keep it simple", "note")
        db.add_project_note(self.pid, "Tabs, not spaces", "guideline")
        db.add_project_note(self.pid, "Ship by Friday\nno new deps", "requirement")
        text = db.project_notes_prompt(self.pid)
        self.assertLess(text.index("Guidelines:"), text.index("Requirements:"))
        self.assertIn("- Tabs, not spaces", text)
        self.assertIn("- Ship by Friday\n  no new deps", text)
        # Plain notes are the user's own (Den 2.0 Guidelines tab): runs never see them.
        self.assertNotIn("Keep it simple", text)
        self.assertNotIn("Notes:", text)

    def test_switched_off_notes_are_left_out(self):
        n = db.add_project_note(self.pid, "Tabs, not spaces", "guideline")
        db.add_project_note(self.pid, "Ship by Friday", "requirement")
        db.update_project_note(n["id"], active=False)
        text = db.project_notes_prompt(self.pid)
        self.assertNotIn("Tabs, not spaces", text)
        self.assertIn("Ship by Friday", text)
        db.update_project_note(n["id"], active=True)
        self.assertIn("Tabs, not spaces", db.project_notes_prompt(self.pid))

    def test_project_rules_from_settings(self):
        db.update_project_settings(self.pid, run_tests=True)
        text = db.project_notes_prompt(self.pid)
        self.assertIn("Project rules:", text)
        self.assertIn("Run the project's tests", text)
        self.assertNotIn("formatter", text)

    def test_claude_code_gets_the_notes_with_the_agent_instructions_in_one_flag(self):
        db.add_project_note(self.pid, "Tabs, not spaces", "guideline")
        args = execution.claude_args({"project_id": self.pid})
        self.assertEqual(args.count("--append-system-prompt"), 1)
        self.assertIn("Tabs, not spaces", args[args.index("--append-system-prompt") + 1])

    def test_no_notes_means_only_the_house_rules(self):
        self.assertEqual(execution.claude_args({"project_id": self.pid}), ["--append-system-prompt", execution.HOUSE_RULES])


class NotesEndpoints(TestBase):
    def setUp(self):
        super().setUp()
        self.pid = db.add_project("shop", self._tmp.name)["id"]

    def test_add_list_edit_delete(self):
        n = api.create_note(self.pid, api.NoteIn(text="  Use Postgres  ", kind="requirement"))
        self.assertEqual((n["text"], n["kind"]), ("Use Postgres", "requirement"))
        self.assertEqual(len(api.list_notes(self.pid)), 1)
        self.assertEqual(api.update_note(n["id"], api.NoteUpdateIn(text="Use SQLite"))["text"], "Use SQLite")
        api.delete_note(n["id"])
        self.assertEqual(api.list_notes(self.pid), [])

    def test_blank_notes_and_missing_things_are_refused(self):
        n = api.create_note(self.pid, api.NoteIn(text="keep"))
        for call, code in ((lambda: api.create_note(self.pid, api.NoteIn(text="   ")), 400),
                           (lambda: api.update_note(n["id"], api.NoteUpdateIn(text=" ")), 400),
                           (lambda: api.create_note(9999, api.NoteIn(text="x")), 404),
                           (lambda: api.list_notes(9999), 404),
                           (lambda: api.update_note(9999, api.NoteUpdateIn(text="x")), 404),
                           (lambda: api.delete_note(9999), 404)):
            with self.assertRaises(HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, code)


class NotePictures(TestBase):
    def setUp(self):
        super().setUp()
        old = uploads.UPLOAD_DIR
        uploads.UPLOAD_DIR = Path(self._tmp.name) / "uploads"
        self.addCleanup(lambda: setattr(uploads, "UPLOAD_DIR", old))
        self.pid = db.add_project("shop", self._tmp.name)["id"]
        self.img = uploads.save(PNG)["id"]

    def test_a_note_keeps_its_pictures_and_can_be_just_a_picture(self):
        n = api.create_note(self.pid, api.NoteIn(text="Match this layout", images=[self.img]))
        self.assertEqual(n["images"], [self.img])
        only = api.create_note(self.pid, api.NoteIn(images=[self.img]))
        self.assertEqual((only["text"], only["images"]), ("", [self.img]))

    def test_edit_replaces_or_clears_the_pictures(self):
        n = api.create_note(self.pid, api.NoteIn(text="x", images=[self.img]))
        self.assertEqual(api.update_note(n["id"], api.NoteUpdateIn(text="y"))["images"], [self.img])
        self.assertIsNone(api.update_note(n["id"], api.NoteUpdateIn(images=[]))["images"])

    def test_a_note_cant_lose_both_its_text_and_pictures(self):
        n = api.create_note(self.pid, api.NoteIn(images=[self.img]))
        with self.assertRaises(HTTPException) as cm:
            api.update_note(n["id"], api.NoteUpdateIn(images=[]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_unknown_pictures_are_refused(self):
        with self.assertRaises(HTTPException) as cm:
            api.create_note(self.pid, api.NoteIn(text="x", images=["0" * 32 + ".png"]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_runs_are_told_where_the_pictures_are(self):
        db.add_project_note(self.pid, "Match this layout", "guideline", images=[self.img])
        db.add_project_note(self.pid, "", "requirement", images=[self.img])
        text = db.project_notes_prompt(self.pid)
        self.assertIn("- Match this layout\n  Pictures in this note", text)
        self.assertIn("- Pictures in this note", text)
        self.assertEqual(text.count(str(uploads.path_of(self.img))), 2)
