"""
A project's picture (shown in place of its coloured letter tile everywhere).

The dashboard shrinks the picture to a small square before sending it; the bytes land under
static/project-images/<project id>-<random hex>.<ext>, so the dashboard can show them with a plain
<img> (no API key) and a new picture gets a new URL (no stale cache). The file's type comes from
its first bytes, never from what the client claims, and the stored name from nothing the client sent.
CRM client logos are kept the same way, named c<client id>-<random hex>.<ext>.
"""

import os
import re
import uuid
from pathlib import Path
from typing import Optional

IMAGE_DIR = Path(os.environ.get("PROJECT_IMAGE_DIR") or Path(__file__).parent / "static" / "project-images")
MAX_BYTES = 2 * 1024 * 1024
_NAME_RE = re.compile(r"c?[0-9]+-[0-9a-f]{32}\.(png|jpg|webp|gif)")


def _kind(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def save(project_id: int, data: bytes, prefix: str = "") -> str:
    """Store a picture and return its stored name. Raises ValueError with a message fit to show.
    prefix "c": a client's logo (project_id is then the client's id)."""
    if not data:
        raise ValueError("That picture is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"That picture is over {MAX_BYTES // (1024 * 1024)} MB")
    ext = _kind(data)
    if not ext:
        raise ValueError("Use a PNG, JPEG, WebP or GIF picture")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{'c' if prefix == 'c' else ''}{int(project_id)}-{uuid.uuid4().hex}.{ext}"
    (IMAGE_DIR / name).write_bytes(data)
    return name


def remove(name: Optional[str]) -> None:
    if name and _NAME_RE.fullmatch(name):
        (IMAGE_DIR / name).unlink(missing_ok=True)
