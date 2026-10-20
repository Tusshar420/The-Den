"""
Export and backup for Settings: tasks / history / settings as JSON or CSV, and a whole-database
backup file that can be restored later.

Exports are for reading and moving data elsewhere, so they never include anything secret: Claude
account tokens live in accounts.json (not the database) and any setting whose key looks like a
credential is left out. A database backup IS the whole file, so treat it as private.

Restore copies a backup over the live database through SQLite's own backup API, so the running
service never has its file swapped out from under it - and the current data is saved to
queue.db.bak-pre-restore first, so a wrong restore can be undone by restoring that file.
"""

import csv
import io
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db

SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "credential", "oauth")
KINDS = ("tasks", "history", "settings")
FORMATS = ("json", "csv")
MAX_RESTORE_BYTES = 512 * 1024 * 1024
# A backup is only accepted if it has at least these - anything else isn't one of ours.
REQUIRED_TABLES = ("projects", "tasks", "settings")


class BackupError(ValueError):
    """A problem with the request itself (bad kind, not a database) - becomes a 400."""


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with db._lock:
        return [dict(r) for r in db.get_conn().execute(sql, params).fetchall()]


def _is_secret(key: str) -> bool:
    k = key.lower()
    return any(h in k for h in SECRET_HINTS)


def collect(kind: str) -> dict[str, list[dict[str, Any]]]:
    """The tables for one kind of export, as {table name: rows}."""
    if kind == "tasks":
        return {"tasks": _rows("SELECT t.*, p.name AS project FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
                               "WHERE t.status != 'deleted' ORDER BY t.id")}
    if kind == "history":
        return {"task_events": _rows("SELECT e.* FROM task_events e JOIN tasks t ON t.id = e.task_id "
                                     "WHERE t.status != 'deleted' ORDER BY e.id"),
                "usage_log": _rows("SELECT * FROM usage_log ORDER BY id")}
    if kind == "settings":
        settings = []
        for r in _rows("SELECT key, value FROM settings ORDER BY key"):
            if _is_secret(r["key"]):
                continue
            try:
                settings.append({"key": r["key"], "value": json.loads(r["value"])})
            except ValueError:
                settings.append({"key": r["key"], "value": r["value"]})
        return {"settings": settings,
                "projects": _rows("SELECT * FROM projects ORDER BY id"),
                "project_settings": _rows("SELECT * FROM project_settings"),
                "recipes": _rows("SELECT * FROM recipes ORDER BY id"),
                "schedules": _rows("SELECT * FROM schedules ORDER BY id")}
    raise BackupError(f"Unknown export '{kind}' - choose one of: {', '.join(KINDS)}")


def _cell(v: Any) -> Any:
    return json.dumps(v) if isinstance(v, (dict, list)) else v


def _csv_text(rows: list[dict[str, Any]]) -> str:
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return out.getvalue()


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def export(kind: str, fmt: str) -> tuple[bytes, str, str]:
    """(file bytes, media type, file name). CSV is one table, so history and settings use the main
    one (events / settings) - the JSON export carries everything."""
    if fmt not in FORMATS:
        raise BackupError("Format must be json or csv")
    data = collect(kind)
    name = f"den-{kind}-{_stamp()}.{fmt}"
    if fmt == "json":
        body = {"exported_at": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
        return json.dumps(body, indent=2, default=str).encode(), "application/json", name
    main = next(iter(data.values()))
    if kind == "settings":
        main = [{"key": r["key"], "value": _cell(r["value"])} for r in main]
    return _csv_text(main).encode("utf-8-sig"), "text/csv", name


# ---------------------------------------------------------------- database backup / restore

def backup_file() -> tuple[bytes, str]:
    """A consistent copy of the live database (safe while the service is running)."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "backup.db"
        out = sqlite3.connect(dest)
        try:
            with db._lock:
                db.get_conn().backup(out)
        finally:
            out.close()
        return dest.read_bytes(), f"den-backup-{datetime.now().strftime('%Y-%m-%d-%H%M')}.db"


def inspect(data: bytes) -> dict[str, Any]:
    """Check an uploaded file really is one of our databases and say what is in it. Raises BackupError."""
    if not data.startswith(b"SQLite format 3\x00"):
        raise BackupError("That isn't a database backup file.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upload.db"
        path.write_bytes(data)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("That backup is damaged (it failed the integrity check).")
                have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not all(t in have for t in REQUIRED_TABLES):
                    raise BackupError("That database isn't from The Den (its tables are missing).")
                return {"tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                        "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]}
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise BackupError(f"That file can't be read as a database ({e}).") from e


def restore(data: bytes) -> dict[str, Any]:
    """Replace everything in the live database with the uploaded backup."""
    summary = inspect(data)
    safety = db.DB_PATH.with_name(db.DB_PATH.name + ".bak-pre-restore")
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "upload.db"
        src_path.write_bytes(data)
        with db._lock:
            conn = db.get_conn()
            conn.commit()
            keep = sqlite3.connect(safety)          # overwritten each time: only the latest pre-restore state is kept
            try:
                conn.backup(keep)
            finally:
                keep.close()
            src = sqlite3.connect(src_path)
            try:
                src.backup(conn)
            finally:
                src.close()
            conn.row_factory = sqlite3.Row
            conn.executescript(db.SCHEMA)            # a backup from an older version gets the newer tables and columns
            conn.commit()
            db._migrate(conn)
    return {"restored": True, **summary, "previous_saved_as": safety.name}
