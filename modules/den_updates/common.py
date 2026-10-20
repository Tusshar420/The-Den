"""
What both sides of Den updates share: the version number, the changelog, and the signature.

The version is the VERSION file at the top of the Den (like 2.1.0). CHANGELOG.md says what each
version brought, in plain words, newest first:

    ## 2.1.0 — 2026-10-21 — The Den 2.1
    - One line per change.

A release is described by a small manifest (version, date, title, items, commit, sha256 and size of
the bundle) signed with the owner's Ed25519 key. A Den that downloads it checks the signature with
the owner's public key before it trusts anything else in the manifest, so a release can't be
changed or made up on the way.
"""

import base64
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parents[2]
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
SIGNED = ("version", "date", "title", "items", "commit", "sha256", "size")
_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s*(?:[—–-]+\s*(\d{4}-\d{2}-\d{2}))?\s*(?:[—–-]+\s*(.*))?$")


class UpdateError(Exception):
    """Something the person can act on - the message is shown in the Den as is."""


def read_version(root: Path = ROOT) -> str:
    try:
        v = (root / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"
    return v if VERSION_RE.match(v) else "0.0.0"


def vtuple(v: str) -> tuple:
    if not VERSION_RE.match(v or ""):
        raise UpdateError(f"'{v}' isn't a version number - use three numbers like 2.1.0")
    return tuple(int(p) for p in v.split("."))


def next_versions(v: str) -> dict[str, str]:
    a, b, c = vtuple(v)
    return {"patch": f"{a}.{b}.{c + 1}", "minor": f"{a}.{b + 1}.0", "major": f"{a + 1}.0.0"}


def parse_changelog(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _HEADING.match(line.strip())
        if m:
            entries.append({"version": m.group(1), "date": m.group(2) or "", "title": (m.group(3) or "").strip(), "items": []})
        elif entries and line.strip().startswith(("- ", "* ")):
            entries[-1]["items"].append(line.strip()[2:].strip())
    return entries


def read_changelog(root: Path = ROOT) -> list[dict[str, Any]]:
    try:
        return parse_changelog((root / "CHANGELOG.md").read_text())
    except OSError:
        return []


def kind(root: Path = ROOT) -> str:
    """'source' is the owner's own copy (a git checkout, where versions are published);
    'install' is a copy given to someone, which updates itself from the owner's releases."""
    return "source" if (root / ".git").exists() else "install"


def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise UpdateError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


# ---------------------------------------------------------------- signatures

def canonical(manifest: dict[str, Any]) -> bytes:
    return json.dumps({k: manifest.get(k) for k in SIGNED}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sign(private: Ed25519PrivateKey, manifest: dict[str, Any]) -> str:
    return b64(private.sign(canonical(manifest)))


def public_from_b64(text: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode((text or "").strip(), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        raise UpdateError("The owner's public key doesn't look right - copy it again from the owner's Den")


def verify(public_b64: str, manifest: dict[str, Any]) -> None:
    key = public_from_b64(public_b64)
    try:
        key.verify(base64.b64decode(manifest.get("signature") or "", validate=True), canonical(manifest))
    except (InvalidSignature, ValueError):
        raise UpdateError("This update isn't signed by the Den's owner, so it was not used")


def clean_manifest(m: Any) -> Optional[dict[str, Any]]:
    """The fields a release manifest may carry, typed; None if it isn't one."""
    if not isinstance(m, dict) or not VERSION_RE.match(str(m.get("version", ""))):
        return None
    items = m.get("items") if isinstance(m.get("items"), list) else []
    try:
        size = int(m.get("size") or 0)
    except (TypeError, ValueError):
        return None
    return {"version": m["version"], "date": str(m.get("date") or ""), "title": str(m.get("title") or ""),
            "items": [str(i) for i in items][:100], "commit": str(m.get("commit") or ""),
            "sha256": str(m.get("sha256") or ""), "size": size, "signature": str(m.get("signature") or "")}
