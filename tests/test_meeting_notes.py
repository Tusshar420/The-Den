"""Meeting notes: pad notes tied to a meeting (pad_notes.event_id), kept when the meeting goes,
and the migration that adds the column without touching notes made before it existed."""

import sqlite3

from fastapi import HTTPException

import api
import db
import pad
import timeline
from tests.helpers import NOW, TestBase


class MeetingNotes(TestBase):
    def setUp(self):
        super().setUp()
        pad._ready = False
        self.pid = db.add_project("shop", self._tmp.name + "/shop")["id"]
        self.meeting = timeline.create(self.pid, "meeting", "Kickoff", "2026-10-01", "10:00", notes="agenda", now=NOW)

    def test_note_can_be_tied_to_a_meeting_and_untied(self):
        n = api.pad_create(api.PadNoteIn(title="Kickoff", body="decided X", project_id=self.pid, event_id=self.meeting["id"]))
        self.assertEqual(n["event_id"], self.meeting["id"])
        self.assertEqual(api.pad_update(n["id"], api.PadNoteUpdateIn(event_id=None))["event_id"], None)
        self.assertEqual(api.pad_update(n["id"], api.PadNoteUpdateIn(event_id=self.meeting["id"]))["event_id"], self.meeting["id"])

    def test_unknown_meeting_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            api.pad_create(api.PadNoteIn(body="x", event_id=9999))
        self.assertEqual(cm.exception.status_code, 404)

    def test_deleting_the_meeting_keeps_the_note(self):
        n = pad.create_note("t", "keep me", self.pid, self.meeting["id"])
        db.delete_project_event(self.meeting["id"])
        kept = pad.get_note(n["id"])
        self.assertEqual((kept["body"], kept["event_id"]), ("keep me", None))

    def test_deleting_the_project_keeps_the_note(self):
        n = pad.create_note("t", "keep me", self.pid, self.meeting["id"])
        db.delete_project(self.pid)
        kept = pad.get_note(n["id"])
        self.assertEqual((kept["body"], kept["event_id"], kept["project_id"]), ("keep me", None, None))

    def test_meetings_own_notes_are_untouched(self):
        pad.create_note("t", "b", self.pid, self.meeting["id"])
        self.assertEqual(db.get_project_event(self.meeting["id"])["notes"], "agenda")
        timeline.update(self.meeting["id"], {"notes": "agenda v2"})     # what the full-page editor saves
        self.assertEqual(db.get_project_event(self.meeting["id"])["notes"], "agenda v2")

    def test_old_pad_table_is_migrated_with_notes_kept(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE IF EXISTS pad_notes")
        conn.execute("CREATE TABLE pad_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',"
                     " project_id INTEGER, saved_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO pad_notes (title, body, created_at, updated_at) VALUES ('old', 'old body', 'a', 'a')")
        conn.commit()
        pad._ready = False
        notes = pad.list_notes()
        self.assertEqual([(x["title"], x["body"], x["event_id"]) for x in notes], [("old", "old body", None)])
        pad._ready = False
        self.assertEqual(len(pad.list_notes()), 1)      # a second start doesn't fail on the existing column
