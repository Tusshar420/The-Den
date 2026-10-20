"""
Documents kept under a project (specs, contracts, PDFs, anything).

The file's bytes live under workspace/documents/<project id>/<random 32 hex>,
with no extension and no part of the client's name on disk, so a name can never
steer where a file lands. The name the user gave, its type and size are kept in
the project_documents table (see db.py) and are only ever used for display and
for the download's file name.
"""

import os
import re
import uuid
from pathlib import Path
from typing import Optional

import db

DOCUMENT_DIR = Path(os.environ.get("DOCUMENT_DIR") or Path(__file__).parent / "workspace" / "documents")
MAX_BYTES = 100 * 1024 * 1024
_NAME_MAX = 120
_STORED_RE = re.compile(r"[0-9a-f]{32}")


def clean_name(name: Optional[str]) -> str:
    """A display name with no folders, control characters or runaway length."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip().strip(".")
    if len(name) > _NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:_NAME_MAX - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:_NAME_MAX]
    return name or "document"


def save(project_id: int, name: Optional[str], mime: Optional[str], data: bytes) -> dict:
    """Store one document. Raises ValueError with a message fit to show the user."""
    if not data:
        raise ValueError("That file is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"That file is over {MAX_BYTES // (1024 * 1024)} MB")
    folder = DOCUMENT_DIR / str(int(project_id))
    folder.mkdir(parents=True, exist_ok=True)
    stored = uuid.uuid4().hex
    (folder / stored).write_bytes(data)
    try:
        return db.add_project_document(project_id, clean_name(name), (mime or "")[:100], len(data), stored)
    except Exception:
        (folder / stored).unlink(missing_ok=True)
        raise


def path_of(doc: dict) -> Optional[Path]:
    """Where a document's bytes are, or None if the record is malformed or the file is gone."""
    if not _STORED_RE.fullmatch(doc.get("stored") or ""):
        return None
    p = DOCUMENT_DIR / str(int(doc["project_id"])) / doc["stored"]
    return p if p.is_file() else None


def remove(doc: dict) -> None:
    """Forget a document: its record and its file."""
    p = path_of(doc)
    db.delete_project_document(doc["id"])
    if p:
        p.unlink(missing_ok=True)


# ------------------------------------------------------- what a task can see
# Claude Code is told what documents the project has and may open them (it gets
# read access to the project's documents folder - see octopus/execution.py).
# On disk they have no extension, so it's pointed at <project folder>/files/,
# where each one is a link named like the original - a .pdf reads as a PDF.
# API models can't open files, so they're given the text of plain-text ones.

READABLE_DIR = "files"
LIST_MAX = 50                 # documents named to a task; the newest, if there are more
TEXT_MAX_CHARS = 40_000       # all the plain-text documents' text an API model is given, together
_TEXT_EXT = {"txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "xml", "html", "htm", "log", "ini",
             "toml", "rst", "sql", "py", "js", "ts", "css", "sh"}


def folder_of(project_id: int) -> Path:
    return DOCUMENT_DIR / str(int(project_id))


def readable(project_id: Optional[int]) -> list[tuple[dict, Path]]:
    """Each of the project's documents (newest first) with a path named like the
    original. The links are brought up to date here, so ones for deleted
    documents go and renamed duplicates ("spec (2).pdf") stay put."""
    if not project_id:
        return []
    docs = [(d, p) for d, p in ((d, path_of(d)) for d in db.list_project_documents(project_id, with_stored=True)[:LIST_MAX]) if p]
    if not docs:
        return []
    view = folder_of(project_id) / READABLE_DIR
    view.mkdir(parents=True, exist_ok=True)
    # A link keeps the name it already has, so a path a chat was given stays good.
    had = {os.readlink(x): x.name for x in view.iterdir() if x.is_symlink()}
    wanted, taken = {}, set()
    for d, _ in sorted(docs, key=lambda x: x[0]["id"]):     # oldest gets the plain name
        name, n = d["name"], 1
        old = had.get("../" + d["stored"])
        if old and old.lower() not in taken:
            name = old
        while name.lower() in taken:                         # the Mac's disk ignores case
            n += 1
            stem, dot, ext = d["name"].rpartition(".")
            name = f"{stem} ({n}).{ext}" if dot and stem else f"{d['name']} ({n})"
        taken.add(name.lower())
        wanted[d["id"]] = name
    targets = {wanted[d["id"]]: "../" + d["stored"] for d, _ in docs}
    for link in view.iterdir():
        if link.name not in targets or not link.is_symlink() or os.readlink(link) != targets[link.name]:
            link.unlink(missing_ok=True)
    for name, target in targets.items():
        try:
            (view / name).symlink_to(target)
        except FileExistsError:                              # already there, or another run made it just now
            pass
    return [(d, view / wanted[d["id"]]) for d, _ in docs]


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{round(n / 1024)} KB" if n < 1048576 else f"{n / 1048576:.1f} MB"


def prompt_for_agent(project_id: Optional[int]) -> str:
    """For Claude Code: which documents the project has and where. Empty without
    any - or if the links can't be made, which must never stop the task."""
    try:
        docs = readable(project_id)
    except OSError:
        return ""
    if not docs:
        return ""
    return ("The user keeps these documents with this project (newest first). Open one with the Read tool "
            "when it's relevant to the task - you don't need to read them all:\n"
            + "\n".join(f"- {d['name']} ({_size(d['size'])}): {p}" for d, p in docs))


def is_text(doc: dict) -> bool:
    ext = doc["name"].rsplit(".", 1)[-1].lower() if "." in doc["name"] else ""
    return ext in _TEXT_EXT or (doc.get("mime") or "").startswith("text/")


def prompt_for_api(project_id: Optional[int]) -> str:
    """For a model that can't open files: the text of the plain-text documents
    (up to TEXT_MAX_CHARS in all), and the names of the rest. Empty without any."""
    if not project_id:
        return ""
    docs = [(d, path_of(d)) for d in db.list_project_documents(project_id, with_stored=True)[:LIST_MAX]]
    docs = [(d, p) for d, p in docs if p]
    if not docs:
        return ""
    room, texts, others = TEXT_MAX_CHARS, [], []
    for d, p in docs:
        text, cut = None, False
        if is_text(d) and room > 0:
            try:
                raw = p.read_bytes()[:room * 4]              # a character is at most 4 bytes
            except OSError:
                raw = b"\x00"
            if b"\x00" not in raw:
                text = raw.decode("utf-8", errors="replace")
                cut = len(text) > room or len(raw) < d["size"]
                text = text[:room]
        if text is None:
            others.append(d["name"])
            continue
        room -= len(text)
        texts.append(f"=== {d['name']}{' (cut short)' if cut else ''}\n{text}")
    out = []
    if texts:
        out.append("The user keeps these documents with this project. Their text, for reference:\n\n" + "\n\n".join(texts))
    if others:
        out.append("The project also has these documents, which you can't open here: " + ", ".join(others))
    return "\n\n".join(out)
