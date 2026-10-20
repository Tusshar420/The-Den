"""Sites Octopus has learned: the `site-*` skills the learn-website skill writes to ~/.claude/skills,
and the task that asks Claude to learn a new one."""

import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SKILLS_DIR = Path.home() / ".claude" / "skills"


class SiteError(ValueError):
    pass


def normalize_url(raw: str) -> str:
    """A clean http(s) URL for a public site, or SiteError. A bare 'example.com' becomes https."""
    url = (raw or "").strip()
    if not url:
        raise SiteError("Paste a website link first.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or "." not in host and host != "localhost":
        raise SiteError("That doesn't look like a website link.")
    if parts.username or parts.password:
        raise SiteError("Links with a username or password aren't allowed.")
    try:
        ip = ipaddress.ip_address(host)
        private = ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        private = host == "localhost" or host.endswith((".local", ".internal"))
    if private:
        raise SiteError("Only public websites can be learned, not ones on this Mac or your network.")
    return url


def learn_prompt(url: str) -> str:
    return (f"Use the learn-website skill to learn this website: {url}\n\n"
            "Inspect its UI and how it works, then save what you learn as a reusable site-<name> skill "
            "in ~/.claude/skills and add it to learn-website/INDEX.md. Only public pages; never store credentials.")


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    out: dict[str, str] = {}
    for line in (m.group(1) if m else "").splitlines():
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip().strip("\"'")
    return out


def list_sites() -> list[dict[str, Any]]:
    """Every learned site, newest first."""
    sites = []
    for f in SKILLS_DIR.glob("site-*/SKILL.md"):
        try:
            text = f.read_text(errors="replace")
            st = f.stat().st_mtime
        except OSError:
            continue
        fm = _frontmatter(text)
        url = re.search(r"^-\s*URL:\s*(\S+)", text, re.M)
        when = re.search(r"^-\s*Analyzed:\s*(\S+)", text, re.M)
        sites.append({"id": f.parent.name, "name": fm.get("name", f.parent.name),
                      "description": fm.get("description", ""), "url": url.group(1) if url else None,
                      "analyzed": when.group(1) if when else None, "path": str(f), "_m": st})
    sites.sort(key=lambda s: s["_m"], reverse=True)
    for s in sites:
        s.pop("_m")
    return sites
