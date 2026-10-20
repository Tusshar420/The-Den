"""Notes pad: free-form notes written from any device (the iPad, mostly).

A note can sit under a project (project_id) or stay unfiled, and can be tied to a
meeting / calendar item (event_id) - the note stays when that item is deleted, just untied. Separately, a
note can be saved out as a real Markdown file - into a project's folder, the
shared notes folder, or any folder under the home directory.
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

import db

NOTES_DIR = Path(__file__).parent / "workspace" / "notes"
_TABLE = """
CREATE TABLE IF NOT EXISTS pad_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    project_id INTEGER REFERENCES projects(id),
    event_id INTEGER REFERENCES project_events(id),
    saved_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_ready = False


def _conn():
    global _ready
    conn = db.get_conn()
    if not _ready:
        with db._lock:
            conn.execute(_TABLE)
            # Notes made before they could be tied to a meeting: add the column, keep every note as it was.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(pad_notes)").fetchall()}
            if "event_id" not in cols:
                conn.execute("ALTER TABLE pad_notes ADD COLUMN event_id INTEGER REFERENCES project_events(id)")
            # Den 2.0: notes saved from a chat (the Message center's side panel) keep which chat and
            # what kind (note | follow-up | decision); any note can be pinned to the top.
            if "chat_id" not in cols:
                conn.execute("ALTER TABLE pad_notes ADD COLUMN chat_id INTEGER")
            if "kind" not in cols:
                conn.execute("ALTER TABLE pad_notes ADD COLUMN kind TEXT NOT NULL DEFAULT 'note'")
            if "pinned" not in cols:
                conn.execute("ALTER TABLE pad_notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            # Den 2.1: pictures in a note - upload ids (uploads.py), in order, as a JSON list.
            if "images" not in cols:
                conn.execute("ALTER TABLE pad_notes ADD COLUMN images TEXT NOT NULL DEFAULT '[]'")
            # Rich notes: content is the editor's document (Quill delta ops, JSON) - bullets, checkboxes,
            # formatting, Important sections, pictures and files in place. body stays its Markdown copy,
            # which search, previews, Octopus and Save as file read. NULL = build the document from body.
            # meta holds the side panel's saved links and comments ({links: [...], comments: [...]}).
            if "content" not in cols:
                conn.execute("ALTER TABLE pad_notes ADD COLUMN content TEXT")
            if "meta" not in cols:
                conn.execute("ALTER TABLE pad_notes ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
            conn.commit()
        _ready = True
    return conn


MAX_IMAGES = 40


def _row(r) -> dict[str, Any]:
    d = dict(r)
    try:
        d["images"] = json.loads(d.get("images") or "[]")
    except ValueError:
        d["images"] = []
    try:
        d["content"] = json.loads(d["content"]) if d.get("content") else None
    except ValueError:
        d["content"] = None
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except ValueError:
        d["meta"] = {}
    return d


def list_notes() -> list[dict[str, Any]]:
    with db._lock:
        rows = _conn().execute("SELECT * FROM pad_notes ORDER BY updated_at DESC, id DESC").fetchall()
    return [_row(r) for r in rows]


def get_note(note_id: int) -> Optional[dict[str, Any]]:
    with db._lock:
        r = _conn().execute("SELECT * FROM pad_notes WHERE id = ?", (note_id,)).fetchone()
    return _row(r) if r else None


KINDS = ("note", "follow-up", "decision")


def create_note(title: str = "", body: str = "", project_id: Optional[int] = None,
                event_id: Optional[int] = None, chat_id: Optional[int] = None, kind: str = "note",
                pinned: bool = False, images: Optional[list[str]] = None, content: Optional[list] = None,
                meta: Optional[dict] = None) -> dict[str, Any]:
    now = db._now()
    with db._lock:
        conn = _conn()
        cur = conn.execute("INSERT INTO pad_notes (title, body, project_id, event_id, chat_id, kind, pinned, images, content, meta, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (title, body, project_id, event_id, chat_id,
                                                                     kind if kind in KINDS else "note", 1 if pinned else 0,
                                                                     json.dumps(list(images or [])),
                                                                     json.dumps(content) if content is not None else None,
                                                                     json.dumps(meta or {}), now, now))
        conn.commit()
        nid = cur.lastrowid
    return get_note(nid)


def update_note(note_id: int, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    if "pinned" in fields:
        fields = {**fields, "pinned": 1 if fields["pinned"] else 0}
    if "kind" in fields and fields["kind"] not in KINDS:
        fields = {k: v for k, v in fields.items() if k != "kind"}
    if "images" in fields:
        fields = {**fields, "images": json.dumps(list(fields["images"] or []))}
    if "content" in fields:
        fields = {**fields, "content": json.dumps(fields["content"]) if fields["content"] is not None else None}
    elif "body" in fields:
        # The text changed without the editor's document (Octopus, an older screen): the document is
        # rebuilt from the new text next time the note opens, so it never shows stale words.
        fields = {**fields, "content": None}
    if "meta" in fields:
        fields = {**fields, "meta": json.dumps(fields["meta"] or {})}
    cols = [k for k in ("title", "body", "project_id", "event_id", "kind", "pinned", "images", "content", "meta") if k in fields]
    if cols:
        with db._lock:
            conn = _conn()
            conn.execute(f"UPDATE pad_notes SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ? WHERE id = ?",
                         [fields[c] for c in cols] + [db._now(), note_id])
            conn.commit()
    return get_note(note_id)


def delete_note(note_id: int) -> None:
    with db._lock:
        conn = _conn()
        conn.execute("DELETE FROM pad_notes WHERE id = ?", (note_id,))
        conn.commit()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "", text).strip().replace(" ", "-")[:60].strip("-.") or "note"


def save_file(note: dict[str, Any], folder: Path, filename: Optional[str] = None) -> Path:
    """Write the note as Markdown into folder (created if missing). Never overwrites a
    different file: a name already taken gets -2, -3, ... unless it is this note's own file."""
    folder.mkdir(parents=True, exist_ok=True)
    name = _slug(Path(filename).stem if filename else (note["title"] or "note"))
    own = Path(note["saved_path"]) if note.get("saved_path") else None
    path = folder / f"{name}.md"
    n = 2
    while path.exists() and path != own:
        path = folder / f"{name}-{n}.md"
        n += 1
    title = (note["title"] or "").strip()
    text = (f"# {title}\n\n" if title else "") + note["body"].rstrip() + "\n"
    # Links saved in the side panel but not placed in the text go at the end, so the file has them all.
    loose = [x for x in ((note.get("meta") or {}).get("links") or []) if isinstance(x, dict) and x.get("url") and x["url"] not in note["body"]]
    if loose:
        text += "\n## Links\n\n" + "".join(f"- [{(x.get('title') or x['url']).replace(']', '')}]({x['url']})\n" for x in loose)
    path.write_text(text, encoding="utf-8")
    with db._lock:
        conn = _conn()
        conn.execute("UPDATE pad_notes SET saved_path = ? WHERE id = ?", (str(path), note["id"]))
        conn.commit()
    return path


def resolve_folder(project_path: Optional[str], sub: str) -> Path:
    """Pick where a file goes. With a project: its folder (+ optional subfolder inside it).
    Without: the shared notes folder, or a folder under the home directory when sub is absolute / ~."""
    sub = (sub or "").strip()
    if project_path:
        base = Path(project_path).expanduser().resolve()
        target = (base / sub).resolve() if sub else base
        if target != base and base not in target.parents:
            raise ValueError("That folder is outside the project")
        return target
    if not sub:
        return NOTES_DIR
    target = Path(sub).expanduser()
    if not target.is_absolute():
        target = NOTES_DIR / sub
    target = target.resolve()
    home = Path.home().resolve()
    if target != home and home not in target.parents:
        raise ValueError("Pick a folder inside your home folder")
    return target
