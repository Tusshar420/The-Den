"""
Pictures attached to a prompt.

The dashboard uploads each picture once (POST /uploads) and then refers to it
by id when it sends the prompt. They live under workspace/uploads/ as
<random 32 hex>.<png|jpg|gif|webp>. The file type is decided by sniffing the
bytes, never by trusting the name or Content-Type the client sent, and an id is
only ever accepted if it has exactly that shape, so an id can't be used to reach
any other file on disk.

How a task sees its pictures depends on the lane (see worker.py):
Claude Code is given the file paths and told to look at them; API models get
the pictures as image parts of the message (providers.py).
"""

import base64
import os
import re
import uuid
from pathlib import Path
from typing import Optional

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or Path(__file__).parent / "workspace" / "uploads")
MAX_BYTES = 10 * 1024 * 1024
MAX_PER_PROMPT = 6

MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
IMAGE_EXTS = frozenset(MIME)
# Documents the Den takes besides pictures. Text-like ones are recognised by their content (no NUL bytes);
# the rest by their signature. html/svg/scripts-that-run-in-a-browser are deliberately not here.
TEXT_EXTS = frozenset("txt md csv tsv json jsonl log yaml yml toml ini xml py js ts tsx jsx css sql sh rb go rs java c cpp h swift kt php tex rtf".split())
ZIP_EXTS = frozenset("zip docx xlsx pptx".split())
OLE_EXTS = frozenset("doc xls ppt".split())
MIME.update({"pdf": "application/pdf", "zip": "application/zip", "doc": "application/msword", "xls": "application/vnd.ms-excel",
             "ppt": "application/vnd.ms-powerpoint",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"})
MIME.update({e: "text/plain" for e in TEXT_EXTS})
_ID_RE = re.compile(r"[0-9a-f]{32}\.(?:" + "|".join(sorted(MIME)) + ")")


def sniff(data: bytes, filename: str = "") -> Optional[str]:
    """The extension for what these bytes really are, or None if it isn't a file we take.
    The filename is only a hint for choosing among types the bytes allow, never trusted on its own."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    hint = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return hint if hint in ZIP_EXTS else "zip"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return hint if hint in OLE_EXTS else None
    if b"\x00" not in data[:8192]:
        try:
            data[:8192].decode("utf-8")
        except UnicodeDecodeError:
            return None
        return hint if hint in TEXT_EXTS else "txt"
    return None


def save(data: bytes, filename: str = "") -> dict:
    """Store one attached file. Raises ValueError with a message fit to show the user."""
    if not data:
        raise ValueError("That file is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"That file is over {MAX_BYTES // (1024 * 1024)} MB")
    ext = sniff(data, filename)
    if not ext:
        raise ValueError("That file type can't be attached - pictures, PDFs, Office files, zips and text files work")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    return {"id": name, "mime": MIME[ext], "size": len(data)}


def path_of(image_id: str) -> Optional[Path]:
    """Where a picture lives, or None if the id is malformed or the file is gone."""
    if not isinstance(image_id, str) or not _ID_RE.fullmatch(image_id):
        return None
    p = UPLOAD_DIR / image_id
    return p if p.is_file() else None


def mime_of(image_id: str) -> str:
    return MIME[image_id.rsplit(".", 1)[-1]]


def is_image(image_id: str) -> bool:
    return image_id.rsplit(".", 1)[-1] in IMAGE_EXTS


def validate(ids: Optional[list]) -> list:
    """The ids of a prompt's pictures, checked. Raises ValueError naming the problem."""
    ids = list(ids or [])
    if len(ids) > MAX_PER_PROMPT:
        raise ValueError(f"Attach at most {MAX_PER_PROMPT} files")
    for i in ids:
        if path_of(i) is None:
            raise ValueError("An attached file wasn't found - attach it again")
    return list(dict.fromkeys(ids))


def inline_text(ids: Optional[list], limit: int = 60_000) -> str:
    """For API models, which can only be sent pictures as image parts: the readable text of any attached
    text files, and a note naming the others (they can't be read in a plain answer)."""
    out = []
    for i in ids or []:
        p = path_of(i)
        if p is None or is_image(i):
            continue
        ext = i.rsplit(".", 1)[-1]
        if ext in TEXT_EXTS:
            out.append(f"[Attached file {p.name}]\n" + p.read_text("utf-8", errors="replace")[:limit])
        else:
            out.append(f"[A .{ext} file was attached but can't be read in a plain answer - send it as a coding task]")
    return "\n\n".join(out)


def read_base64(image_id: str) -> tuple:
    """(mime, base64 text) for sending to an API model. Raises FileNotFoundError if it's gone."""
    p = path_of(image_id)
    if p is None:
        raise FileNotFoundError(image_id)
    return mime_of(image_id), base64.b64encode(p.read_bytes()).decode("ascii")


def prompt_for_agent(prompt: str, ids: Optional[list]) -> str:
    """The prompt as Claude Code should see it: the user's words, then where the pictures are."""
    paths = [str(p) for p in (path_of(i) for i in ids or []) if p]
    if not paths:
        return prompt
    return (prompt + "\n\nThe user attached " + ("a file" if len(paths) == 1 else f"{len(paths)} files")
            + " to this message. Look at " + ("it" if len(paths) == 1 else "each one")
            + " with the Read tool (pictures, PDFs and text read directly; for Office files or zips use a shell command) before answering:\n" + "\n".join(paths))
