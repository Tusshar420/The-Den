"""Export and backup: files are well-formed and secret-free; restore is checked, confirmed and reversible."""

import asyncio
import csv
import io
import json

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

from fastapi import HTTPException

import api
import backup
import db


class FakeRequest:
    def __init__(self, body: bytes):
        self._body, self.headers = body, {"content-length": str(len(body))}

    async def body(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


class Export(TestBase):
    def test_tasks_json_and_csv(self):
        db.add_task(self.project["id"], "write, the \"docs\"", 0, None, lane="agent")
        body, media, name = backup.export("tasks", "json")
        self.assertEqual(media, "application/json")
        self.assertTrue(name.startswith("den-tasks-") and name.endswith(".json"))
        self.assertEqual(json.loads(body)["tasks"][0]["prompt"], 'write, the "docs"')
        body, media, _ = backup.export("tasks", "csv")
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual((media, rows[0]["prompt"], rows[0]["project"]), ("text/csv", 'write, the "docs"', "General"))

    def test_deleted_tasks_are_not_exported(self):
        t = db.add_task(self.project["id"], "gone", 0, None, lane="agent")
        api.delete_task(t["id"])
        self.assertEqual(json.loads(backup.export("tasks", "json")[0])["tasks"], [])

    def test_settings_export_leaves_out_secrets(self):
        db.set_setting("autonomy", "assist")
        db.set_setting("google_oauth_token", "sekrit")
        db.set_setting("some_api_key", "sekrit")
        body = backup.export("settings", "json")[0]
        self.assertNotIn(b"sekrit", body)
        keys = [s["key"] for s in json.loads(body)["settings"]]
        self.assertIn("autonomy", keys)
        self.assertNotIn(b"sekrit", backup.export("settings", "csv")[0])

    def test_history_has_events(self):
        t = db.add_task(self.project["id"], "x", 0, None, lane="agent")
        db.add_task_event(t["id"], "note", "did a thing")
        self.assertEqual(json.loads(backup.export("history", "json")[0])["task_events"][0]["text"], "did a thing")

    def test_unknown_kind_or_format_is_a_400(self):
        for kind, fmt in (("nope", "json"), ("tasks", "xml")):
            with self.assertRaises(HTTPException) as cm:
                api.export_data(kind, fmt)
            self.assertEqual(cm.exception.status_code, 400)

    def test_endpoint_sets_download_headers(self):
        r = api.export_data("tasks", "csv")
        self.assertIn("attachment; filename=", r.headers["content-disposition"])


class Restore(TestBase):
    def test_round_trip_replaces_current_data_and_keeps_a_safety_copy(self):
        db.add_task(self.project["id"], "before backup", 0, None, lane="agent")
        data, name = backup.backup_file()
        self.assertTrue(name.startswith("den-backup-") and name.endswith(".db"))
        db.add_task(self.project["id"], "after backup", 0, None, lane="agent")
        db.set_setting("autonomy", "assist")
        self.assertEqual(run(api.restore_check(FakeRequest(data))), {"tasks": 1, "projects": 1})
        out = run(api.restore_backup(FakeRequest(data), confirm="replace-everything"))
        self.assertEqual((out["restored"], out["tasks"]), (True, 1))
        self.assertEqual([t["prompt"] for t in db.list_all_tasks()], ["before backup"])
        self.assertEqual(db.get_setting("autonomy"), "execute")
        self.assertTrue((db.DB_PATH.parent / out["previous_saved_as"]).exists())
        db.add_task(self.project["id"], "still works", 0, None, lane="agent")      # the live connection is healthy

    def test_restore_needs_explicit_confirmation(self):
        data, _ = backup.backup_file()
        db.add_task(self.project["id"], "keep me", 0, None, lane="agent")
        with self.assertRaises(HTTPException) as cm:
            run(api.restore_backup(FakeRequest(data)))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(len(db.list_all_tasks()), 1)

    def test_garbage_is_rejected_and_nothing_changes(self):
        db.add_task(self.project["id"], "keep me", 0, None, lane="agent")
        for junk in (b"hello", b"SQLite format 3\x00" + b"\x00" * 200):
            with self.assertRaises(HTTPException) as cm:
                run(api.restore_backup(FakeRequest(junk), confirm="replace-everything"))
            self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(len(db.list_all_tasks()), 1)

    def test_a_foreign_sqlite_file_is_rejected(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE other (a)")
            c.commit()
            c.close()
            with self.assertRaises(backup.BackupError):
                backup.inspect(p.read_bytes())
