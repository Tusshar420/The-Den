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
_ID_RE = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|gif|webp)")


def sniff(data: bytes) -> Optional[str]:
    """The extension for what these bytes really are, or None if it isn't a picture we take."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save(data: bytes) -> dict:
    """Store one picture. Raises ValueError with a message fit to show the user."""
    if not data:
        raise ValueError("That file is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"That picture is over {MAX_BYTES // (1024 * 1024)} MB")
    ext = sniff(data)
    if not ext:
        raise ValueError("Only PNG, JPEG, GIF and WebP pictures can be attached")
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


def validate(ids: Optional[list]) -> list:
    """The ids of a prompt's pictures, checked. Raises ValueError naming the problem."""
    ids = list(ids or [])
    if len(ids) > MAX_PER_PROMPT:
        raise ValueError(f"Attach at most {MAX_PER_PROMPT} pictures")
    for i in ids:
        if path_of(i) is None:
            raise ValueError("An attached picture wasn't found - attach it again")
    return list(dict.fromkeys(ids))


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
    return (prompt + "\n\nThe user attached " + ("a picture" if len(paths) == 1 else f"{len(paths)} pictures")
            + " to this message. Look at " + ("it" if len(paths) == 1 else "each one")
            + " with the Read tool before answering:\n" + "\n".join(paths))
