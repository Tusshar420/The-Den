"""
The "system" screens of The Den 2.0 (DenSystem.dc.html): AI accounts, and the
Settings extras - sites, agents & tools, push devices, alert email, backups and
export. Most of it is a thin layer over what the Den already has:

- Claude accounts are accounts.py (the page calls /accounts/... directly), and
  OpenAI / Gemini keys are api_keys.py (/octopus/api-keys/...). What this module
  adds is what neither keeps: a name and colour for an API key, the account
  order per provider, "switch automatically at 80 / 90 / 95%" for Claude (done
  here, in tick(), by moving the default account - accounts.activate - never a
  project's own account), this month's spend per provider, and the GPU memory a
  local Ollama server reports (its own /api/ps).
- Sites are the site-* skills octopus/sites.py lists. "Use in answers" off moves
  the skill folder out of ~/.claude/skills (so Claude Code stops loading it) into
  skills-off/, and Remove moves it to skills-removed/ - nothing is erased.
- Backups are copies of the database (SQLite's own backup API, safe while the
  service runs) kept in backups/ next to it, by hand or on a schedule (tick()),
  restored through backup.restore - which keeps the current data first.
- Export is the datasets the owner picks, as JSON, CSV or a ZIP, never with
  anything hidden (vault projects and tasks) or secret (token-like settings).

All of it is kept in one setting, "den_system" (see _state). Stdlib + db only,
apart from the Den modules it reuses.
"""

import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import accounts
import api_keys
import backup
import db
import den_platform
import mailer
import router as model_router
from assistant import connections, timeutil
from assistant.providers import google as google_drive
from assistant.providers.base import ProviderError
from octopus import models as model_admin
from octopus import sites

log = logging.getLogger("the-den.system")

SETTING = "den_system"
PROVIDERS = ("claude", "openai", "gemini", "other")
THRESHOLDS = (80, 90, 95)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_lock = threading.Lock()


# ---------------------------------------------------------------- stored choices

def _state() -> dict[str, Any]:
    return dict(db.get_setting(SETTING) or {})


def _save(**changes: Any) -> dict[str, Any]:
    with _lock:
        st = _state()
        st.update(changes)
        db.set_setting(SETTING, st)
        return st


# ---------------------------------------------------------------- AI accounts

def _pct(window: Optional[dict[str, Any]]) -> float:
    """A usage window's utilization as a percentage (Claude Code reports 0-1 or 0-100)."""
    u = (window or {}).get("utilization")
    if u is None:
        return 0.0
    u = float(u)
    return u * 100 if u <= 1 else u


def _used(account: dict[str, Any]) -> float:
    """How full the fuller of an account's 5-hour and weekly windows is."""
    w = ((account.get("usage") or {}).get("windows")) or {}
    return max(_pct(w.get("five_hour")), _pct(w.get("seven_day")))


def policy(provider: str) -> dict[str, Any]:
    """Switch automatically?, at what %, and the account order for one provider.
    OpenAI / Gemini's switch is the AI Providers "Retry on the next key" setting
    (failover.retry) - the same thing, so there is only one switch for it."""
    p = dict((_state().get("policy") or {}).get(provider) or {})
    out = {"auto": bool(p.get("auto", False)), "at": p.get("at") if p.get("at") in THRESHOLDS else 90,
           "order": [str(i) for i in p.get("order") or []]}
    if provider in api_keys.PROVIDERS:
        out["auto"] = bool((db.get_setting("failover") or {}).get("retry", True))
    return out


def set_policy(provider: str, auto: Optional[bool] = None, at: Optional[int] = None,
               order: Optional[list[str]] = None) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if at is not None and at not in THRESHOLDS:
        raise HTTPException(status_code=400, detail="Switch at 80, 90 or 95%")
    with _lock:
        st = _state()
        pol = dict(st.get("policy") or {})
        cur = dict(pol.get(provider) or {})
        if auto is not None:
            cur["auto"] = bool(auto)
        if at is not None:
            cur["at"] = at
        if order is not None:
            cur["order"] = [str(i)[:80] for i in order][:50]
        pol[provider] = cur
        st["policy"] = pol
        db.set_setting(SETTING, st)
    if provider in api_keys.PROVIDERS:
        if auto is not None:
            db.set_setting("failover", {**(db.get_setting("failover") or {}), "retry": bool(auto)})
        if order:
            # The first key is the one every call starts with - that's the provider's default key.
            try:
                api_keys.make_default(order[0])
            except ValueError:
                pass
    return policy(provider)


def ordered(provider: str, ids: list[str]) -> list[str]:
    """ids in the owner's order; ones never ordered go last, in the order given."""
    want = [i for i in policy(provider)["order"] if i in ids]
    return want + [i for i in ids if i not in want]


def set_look(provider: str, item_id: str, name: Optional[str] = None, color: Optional[str] = None) -> dict[str, Any]:
    """A name / colour for an API key or an OpenAI-compatible server. api_keys.py has no
    rename, so the name is kept here (the key's own label stays as it was)."""
    if provider not in PROVIDERS or provider == "claude":
        raise HTTPException(status_code=400, detail="Claude accounts are renamed in /accounts")
    if color is not None and color and not COLOR_RE.match(color):
        raise HTTPException(status_code=400, detail="Colour must look like #aabbcc")
    with _lock:
        st = _state()
        looks = dict(st.get("looks") or {})
        key = f"{provider}:{item_id}"
        cur = dict(looks.get(key) or {})
        if name is not None:
            cur["name"] = name.strip()[:60]
        if color is not None:
            cur["color"] = color.lower()
        looks[key] = {k: v for k, v in cur.items() if v}
        st["looks"] = looks
        db.set_setting(SETTING, st)
    return looks[key]


def month_start() -> datetime:
    return timeutil.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend() -> dict[str, Any]:
    """Real API spend this month per provider and per model - api / assistant lane tasks, the
    same definition as db.spend_summary (Claude Code's cost is only an estimate, never spend).
    Which key paid isn't recorded, so a provider's spend is all of its keys together."""
    since = month_start().astimezone(timezone.utc).isoformat()
    with db._lock:
        rows = db.get_conn().execute(
            "SELECT model_id, SUM(cost_usd) AS s FROM tasks WHERE lane IN ('api', 'assistant') AND cost_usd IS NOT NULL "
            "AND COALESCE(finished_at, created_at) >= ? GROUP BY model_id", (since,)).fetchall()
    by_model = {r["model_id"]: float(r["s"] or 0) for r in rows if r["model_id"]}
    by_provider: dict[str, float] = {}
    for mid, cost in by_model.items():
        prov = (model_router.get_model(mid) or {}).get("provider") or "other"
        by_provider[prov] = by_provider.get(prov, 0.0) + cost
    return {"providers": by_provider, "models": by_model}


_gpu_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
GPU_CACHE_SECONDS = 30


def _ollama_root(base_url: str) -> Optional[str]:
    """The Ollama server behind an OpenAI-compatible base URL, if it looks like one (its port, or 'ollama' in it)."""
    parts = urlsplit(base_url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.port != 11434 and "ollama" not in base_url.lower():
        return None
    return f"{parts.scheme}://{parts.netloc}"


def ollama_gpu(base_url: str, refresh: bool = False) -> Optional[dict[str, Any]]:
    """What Ollama's own /api/ps says is loaded: the first model's name, and how much of it is in
    GPU memory (size_vram / size). None when the server isn't Ollama or doesn't answer quickly."""
    root = _ollama_root(base_url)
    if not root:
        return None
    hit = _gpu_cache.get(root)
    if hit and not refresh and time.time() - hit[0] < GPU_CACHE_SECONDS:
        return hit[1]
    info: Optional[dict[str, Any]] = None
    try:
        with urllib.request.urlopen(root + "/api/ps", timeout=1.5) as r:
            data = json.loads(r.read(200_000) or b"{}")
        models = [m for m in data.get("models") or [] if isinstance(m, dict)]
        if models:
            m = models[0]
            size, vram = float(m.get("size") or 0), float(m.get("size_vram") or 0)
            info = {"loaded": m.get("name") or m.get("model"), "pct": round(vram / size * 100) if size else 0,
                    "vram_gb": round(vram / 1e9, 1), "count": len(models)}
        else:
            info = {"loaded": None, "pct": 0, "vram_gb": 0.0, "count": 0}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        info = None
    _gpu_cache[root] = (time.time(), info)
    return info


def ai_accounts(refresh: bool = False) -> dict[str, Any]:
    """What the AI accounts page needs beyond /settings (Claude accounts) and /octopus/providers
    (keys and models): order and auto-switch per provider, names / colours, spend, local GPU."""
    spend = month_spend()
    local = {}
    for m in model_router.registry().get("models", []):
        if m.get("provider") == "openai_compat" and m.get("base_url"):
            g = ollama_gpu(m["base_url"], refresh)
            if g is not None:
                local[m["id"]] = g
    nxt = (month_start() + timedelta(days=32)).replace(day=1)
    st = _state()
    return {"policy": {p: policy(p) for p in PROVIDERS}, "looks": st.get("looks") or {},
            "spend": spend, "budget_usd": db.get_setting("monthly_budget_usd"), "resets_on": nxt.date().isoformat(),
            "local": local, "last_switch": st.get("last_switch"),
            # Who acts on "Switch automatically": the Den itself (tick), the providers' key failover, or nothing yet.
            "auto_by": {"claude": "den", "openai": "failover", "gemini": "failover", "other": "stored"}}


def auto_switch_claude() -> Optional[str]:
    """Switch automatically (Claude): once the default account's 5-hour or weekly window is at the
    chosen %, make the next account in the owner's order that is under it (and logged in, and not
    waiting out a limit) the default. Projects with an account of their own are never moved.
    Returns the account switched to, or None."""
    pol = policy("claude")
    if not pol["auto"]:
        return None
    listed = accounts.public_list()
    active = next((a for a in listed if a.get("active")), None)
    if not active or (_used(active) < pol["at"] and not active.get("limited_until")):
        return None
    ids = ordered("claude", [a["id"] for a in listed])
    start = ids.index(active["id"]) if active["id"] in ids else -1
    by_id = {a["id"]: a for a in listed}
    for cand in ids[start + 1:] + ids[:max(start, 0)]:
        a = by_id[cand]
        if cand == active["id"] or a.get("auth_error") or a.get("limited_until") or a.get("logged_in") is False:
            continue
        if _used(a) >= pol["at"]:
            continue
        accounts.activate(cand)
        db.resume_rate_limited_now(default_account_only=True)
        try:
            import worker      # here, not at the top: worker imports this module for tick()
            worker.wake_agent_lane()
        except Exception:
            log.exception("couldn't wake the agent lane after switching account")
        _save(last_switch={"at": db._now(), "from": active["id"], "to": cand, "used": round(_used(active))})
        log.info("Claude account: %s at %d%% - switched to %s", active["id"], round(_used(active)), cand)
        return cand
    return None


# ---------------------------------------------------------------- sites

SITE_ID = re.compile(r"^site-[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _off_dir() -> Path:
    return sites.SKILLS_DIR.parent / "skills-off"


def _removed_dir() -> Path:
    return sites.SKILLS_DIR.parent / "skills-removed"


def _read_site(f: Path, on: bool) -> Optional[dict[str, Any]]:
    try:
        text = f.read_text(errors="replace")
        mtime = f.stat().st_mtime
    except OSError:
        return None
    fm = sites._frontmatter(text)
    url = re.search(r"^-\s*URL:\s*(\S+)", text, re.M)
    when = re.search(r"^-\s*Analyzed:\s*(\S+)", text, re.M)
    pages = len(re.findall(r"^#{2,3} ", text, re.M))
    return {"id": f.parent.name, "name": fm.get("name", f.parent.name), "description": fm.get("description", "")[:300],
            "url": url.group(1) if url else None, "analyzed": when.group(1) if when else None,
            "updated_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(), "sections": pages, "on": on, "_m": mtime}


def site_list() -> dict[str, Any]:
    """Learned sites (on, then off), and the Learn site tasks still going or that failed this week."""
    found = []
    for base, on in ((sites.SKILLS_DIR, True), (_off_dir(), False)):
        for f in base.glob("site-*/SKILL.md"):
            s = _read_site(f, on)
            if s:
                found.append(s)
    found.sort(key=lambda s: s["_m"], reverse=True)
    for s in found:
        s.pop("_m")
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db._lock:
        rows = db.get_conn().execute(
            "SELECT id, prompt, status, current_step, created_at, finished_at FROM tasks WHERE routed_by = 'sites' "
            "AND status IN ('queued', 'running', 'rate_limited', 'error', 'needs_review') AND created_at >= ? ORDER BY id DESC LIMIT 10",
            (week,)).fetchall()
    learning = []
    for r in rows:
        m = re.search(r"learn this website: (\S+)", r["prompt"] or "")
        learning.append({"task_id": r["id"], "url": m.group(1) if m else None, "status": r["status"],
                         "step": r["current_step"], "created_at": r["created_at"]})
    return {"sites": found, "learning": learning}


def _site_dir(site_id: str) -> tuple[Path, bool]:
    if not SITE_ID.match(site_id or ""):
        raise HTTPException(status_code=400, detail="Not a site")
    for base, on in ((sites.SKILLS_DIR, True), (_off_dir(), False)):
        d = base / site_id
        if (d / "SKILL.md").is_file():
            return d, on
    raise HTTPException(status_code=404, detail="No such site")


def set_site(site_id: str, on: bool) -> dict[str, Any]:
    """Use in answers on / off: the skill folder moves in or out of ~/.claude/skills."""
    d, now_on = _site_dir(site_id)
    if now_on != on:
        dest = (sites.SKILLS_DIR if on else _off_dir()) / site_id
        if dest.exists():
            raise HTTPException(status_code=409, detail="There's already a copy of that site there")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest))
    return {"id": site_id, "on": on}


def remove_site(site_id: str) -> dict[str, Any]:
    """Out of the list - moved to skills-removed/, not erased."""
    d, _ = _site_dir(site_id)
    dest = _removed_dir() / f"{site_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(dest))
    return {"ok": True, "id": site_id, "kept_in": str(dest)}


# ---------------------------------------------------------------- agents & tools (programs on this Mac)

# id: (name, the program, the version flag, how to install it)
TOOLS = {
    "claude": ("Claude Code CLI", lambda: accounts.CLAUDE_BIN, ["--version"], "npm install -g @anthropic-ai/claude-code"),
    "git": ("Git", lambda: "git", ["--version"], "xcode-select --install"),
    "node": ("Node.js", lambda: "node", ["--version"], "brew install node"),
    "python": ("Python", lambda: sys.executable, ["--version"], None),
    "playwright": ("Playwright browser", None, None, "npx playwright install chromium"),
    "weasyprint": ("weasyprint", lambda: "weasyprint", ["--version"], "brew install weasyprint"),
    "gh": ("GitHub CLI", lambda: "gh", ["--version"], "brew install gh"),
    "docker": ("Docker", lambda: "docker", ["--version"], "brew install --cask docker"),
}
TOOL_CACHE_SECONDS = 600
_tool_cache: dict[str, Any] = {"at": 0.0, "rows": None}
VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def _which(prog: str) -> Optional[str]:
    if os.path.isabs(prog):
        return prog if os.access(prog, os.X_OK) else None
    venv = Path(sys.executable).parent / (prog + ".exe" if den_platform.IS_WINDOWS else prog)   # the service's own venv first (weasyprint lives there)
    if venv.is_file() and os.access(venv, os.X_OK):
        return str(venv)
    return shutil.which(prog) or next((p for p in (f"/opt/homebrew/bin/{prog}", f"/usr/local/bin/{prog}",
                                                    str(Path.home() / ".local/bin" / prog)) if os.access(p, os.X_OK)), None)


def _playwright() -> dict[str, Any]:
    for cache in den_platform.browser_cache_dirs():
        found = sorted(cache.glob("chromium-*")) if cache.is_dir() else []
        if found:
            return {"path": str(cache).replace(str(Path.home()), "~"), "version": "chromium " + found[-1].name.split("-")[-1], "status": "ok"}
    return {"path": "not found", "version": "missing", "status": "missing"}


def _detect(tool_id: str) -> dict[str, Any]:
    name, prog, flag, _ = TOOLS[tool_id]
    if tool_id == "playwright":
        return _playwright()
    path = _which(prog())
    if not path:
        return {"path": "not found", "version": "missing", "status": "missing"}
    try:
        r = subprocess.run([path, *flag], capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL)
        m = VERSION_RE.search((r.stdout or "") + " " + (r.stderr or ""))
        ver, status = (m.group(0) if m else "?"), ("ok" if r.returncode == 0 else "warn")
    except (OSError, subprocess.TimeoutExpired):
        ver, status = "?", "warn"
    return {"path": path.replace(str(Path.home()), "~"), "version": ver, "status": status}


def tool_list(refresh: bool = False) -> list[dict[str, Any]]:
    """Each program Octopus uses here: where it is, its version, and whether it's allowed.
    "Allowed" is the owner's choice kept here; nothing enforces it yet (see the report)."""
    if refresh or _tool_cache["rows"] is None or time.time() - _tool_cache["at"] > TOOL_CACHE_SECONDS:
        _tool_cache.update(at=time.time(), rows={t: _detect(t) for t in TOOLS})
    off = set(_state().get("tools_off") or [])
    return [{"id": t, "name": TOOLS[t][0], **_tool_cache["rows"][t], "on": t not in off,
             "installable": bool(TOOLS[t][3])} for t in TOOLS]


def set_tool(tool_id: str, on: bool) -> dict[str, Any]:
    if tool_id not in TOOLS:
        raise HTTPException(status_code=404, detail="Unknown tool")
    off = set(_state().get("tools_off") or [])
    (off.discard if on else off.add)(tool_id)
    _save(tools_off=sorted(off))
    return {"id": tool_id, "on": on}


def install_tool(tool_id: str) -> dict[str, Any]:
    """Install = a task for Octopus in General, so it runs (and asks) like any other command."""
    if tool_id not in TOOLS or not TOOLS[tool_id][3]:
        raise HTTPException(status_code=404, detail="Nothing to install for that")
    from octopus import plan as planner
    name, cmd = TOOLS[tool_id][0], TOOLS[tool_id][3]
    task = db.add_task(planner.general_project()["id"],
                       f"Install {name} on this Mac by running: {cmd}\nThen run its --version to confirm it works, and say which version was installed.",
                       title=f"Install {name}", source="dashboard", route_reason="You asked to install a tool from Settings", routed_by="den-system")
    _tool_cache["at"] = 0.0
    return {"task": task, "command": cmd}


# ---------------------------------------------------------------- push devices and alert email

def _device_name(ua: str) -> tuple[str, str]:
    ua = ua or ""
    dev = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android phone" if "Android" in ua
           else "Mac" if "Macintosh" in ua else "Windows PC" if "Windows" in ua else "Linux" if "Linux" in ua else "Browser")
    br = ("Edge" if "Edg/" in ua else "Chrome" if "Chrome/" in ua or "CriOS" in ua else "Firefox" if "Firefox" in ua
          else "Safari" if "Safari" in ua else "")
    kind = "phone" if dev in ("iPhone", "Android phone") else "tablet" if dev == "iPad" else "desktop"
    return dev + (" · " + br if br else ""), kind


def push_devices() -> list[dict[str, Any]]:
    """Browsers that allowed notifications - never their endpoint or keys."""
    out = []
    for s in db.list_push_subscriptions():
        name, kind = _device_name(s.get("user_agent") or "")
        out.append({"id": s["id"], "name": name, "kind": kind, "added_at": s.get("created_at")})
    return out


def remove_push_device(device_id: int) -> dict[str, Any]:
    sub = next((s for s in db.list_push_subscriptions() if s["id"] == device_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="No such device")
    db.delete_push_subscription(sub["endpoint"])
    return {"ok": True, "devices": push_devices()}


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


def set_alert_email(email: str) -> dict[str, Any]:
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="That isn't an email address")
    db.set_setting("email_to", email)
    return {"to": mailer.recipient()}


# ---------------------------------------------------------------- backups

BACKUP_NAME = re.compile(r"^den-(auto-)?\d{4}-\d{2}-\d{2}-\d{4}(-\d+)?\.db$")
SCHEDULES = ("daily", "weekly", "off")
BACKUP_HOUR = 2
KEEP_AUTO = 8


def backup_dir() -> Path:
    d = db.DB_PATH.parent / "backups"
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    ignore = d / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Database backups (den_system.py) - private, never commit them.\n*\n")
    return d


def backup_settings() -> dict[str, Any]:
    b = dict(_state().get("backup") or {})
    return {"schedule": b.get("schedule") if b.get("schedule") in SCHEDULES else "off",
            "drive": bool(b.get("drive")), "last_auto": b.get("last_auto"),
            # Drive copies need a connected Google account that was allowed to use Drive.
            "drive_available": bool(connections.connected_with("drive")),
            "drive_account": next(iter(connections.connected_with("drive")), None) and connections.label(connections.connected_with("drive")[0]),
            "drive_error": b.get("drive_error"), "last_drive": b.get("last_drive"),
            "hour": BACKUP_HOUR, "keep": KEEP_AUTO}


def set_backup_settings(schedule: Optional[str] = None, drive: Optional[bool] = None) -> dict[str, Any]:
    if schedule is not None and schedule not in SCHEDULES:
        raise HTTPException(status_code=400, detail="Daily, weekly or off")
    with _lock:
        st = _state()
        b = dict(st.get("backup") or {})
        if schedule is not None:
            b["schedule"] = schedule
        if drive is not None:
            b["drive"] = bool(drive)
        st["backup"] = b
        db.set_setting(SETTING, st)
    return backup_settings()


def backup_list() -> list[dict[str, Any]]:
    out = []
    on_drive = set((_state().get("backup") or {}).get("drive_names") or [])
    for f in backup_dir().glob("den-*.db"):
        if not BACKUP_NAME.match(f.name):
            continue
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size, "auto": f.name.startswith("den-auto-"),
                    "at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                    "where": "Local + Drive" if f.name in on_drive else "Local"})
    out.sort(key=lambda b: b["at"], reverse=True)
    return out


def make_backup(auto: bool = False) -> dict[str, Any]:
    """A consistent copy of the live database, written straight to backups/ (owner-only)."""
    d = backup_dir()
    stem = f"den-{'auto-' if auto else ''}{datetime.now().strftime('%Y-%m-%d-%H%M')}"
    dest, n = d / f"{stem}.db", 2
    while dest.exists():
        dest, n = d / f"{stem}-{n}.db", n + 1
    tmp = dest.with_suffix(".db.part")
    out = sqlite3.connect(tmp)
    try:
        with db._lock:
            db.get_conn().backup(out)
    finally:
        out.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    if auto:
        for old in [b for b in backup_list() if b["auto"]][KEEP_AUTO:]:
            try:
                (d / old["name"]).unlink()
            except OSError:
                pass
    _copy_to_drive(dest)
    return next(b for b in backup_list() if b["name"] == dest.name)


DRIVE_FOLDER = "The Den backups"
DRIVE_KEEP = 30


def _copy_to_drive(path: Path) -> None:
    """If 'Also copy to Google Drive' is on and Drive is connected, upload this backup. A failure is
    recorded for the Settings page and logged - it never fails the local backup."""
    b = backup_settings()
    if not b["drive"]:
        return
    ids = connections.connected_with("drive")
    try:
        if not ids:
            raise ProviderError("Google Drive isn't connected - connect it under Connections.")
        conn = ids[0]
        folder = google_drive.drive_folder(DRIVE_FOLDER, conn, _state().get("backup", {}).get("drive_folder"))
        google_drive.drive_upload(path.name, path.read_bytes(), folder, conn)
        change = {"drive_folder": folder, "last_drive": db._now(), "drive_error": None,
                  "drive_names": ([*(_state().get("backup", {}).get("drive_names") or []), path.name])[-DRIVE_KEEP:]}
    except ProviderError as e:
        log.warning("Drive backup copy failed: %s", e)
        change = {"drive_error": str(e)[:200]}
    except Exception:
        log.exception("Drive backup copy failed")
        change = {"drive_error": "Couldn't copy the backup to Google Drive"}
    with _lock:
        st = _state()
        st["backup"] = {**(st.get("backup") or {}), **change}
        db.set_setting(SETTING, st)


def _backup_path(name: str) -> Path:
    if not BACKUP_NAME.match(name or ""):
        raise HTTPException(status_code=400, detail="Not a backup")
    p = backup_dir() / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="No such backup")
    return p


def restore_backup(name: str) -> dict[str, Any]:
    """Replace everything with a saved backup. backup.restore keeps the current data first
    (queue.db.bak-pre-restore); a fresh backup of it lands in the list too."""
    data = _backup_path(name).read_bytes()
    try:
        backup.inspect(data)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    safety = make_backup()
    try:
        out = backup.restore(data)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**out, "safety_backup": safety["name"]}


def backup_due(now: Optional[datetime] = None) -> bool:
    b = backup_settings()
    if b["schedule"] == "off":
        return False
    now = now or timeutil.now()
    if now.hour < BACKUP_HOUR:
        return False
    if not b["last_auto"]:
        return True
    try:
        last = timeutil.parse_iso(b["last_auto"]).astimezone(now.tzinfo)
    except ValueError:
        return True
    if b["schedule"] == "daily":
        return last.date() < now.date()
    return now - last >= timedelta(days=7) - timedelta(hours=1)


def tick() -> None:
    """From the worker's scheduler loop: the automatic backup when it's due, and the Claude auto-switch."""
    try:
        if backup_due():
            made = make_backup(auto=True)
            with _lock:
                st = _state()
                st["backup"] = {**(st.get("backup") or {}), "last_auto": db._now()}
                db.set_setting(SETTING, st)
            log.info("Automatic backup: %s", made["name"])
    except Exception:
        log.exception("automatic backup failed")
    try:
        auto_switch_claude()
    except Exception:
        log.exception("Claude auto-switch failed")


# ---------------------------------------------------------------- export

SECRET_HINTS = (*backup.SECRET_HINTS, "pin", "vault", "den_system")
DATASETS = ("projects", "tasks", "notes", "crm", "calendar", "usage", "settings")
FORMATS = ("zip", "json", "csv")


def _tables() -> set[str]:
    with db._lock:
        return {r[0] for r in db.get_conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with db._lock:
        return [dict(r) for r in db.get_conn().execute(sql, params).fetchall()]


def collect(dataset: str) -> dict[str, list[dict[str, Any]]]:
    """One dataset as {table: rows}. Hidden (vault) projects and tasks, and everything of theirs,
    are left out, as is any setting that looks like a credential."""
    have = _tables()
    shown = "SELECT id FROM projects WHERE COALESCE(hidden, 0) = 0 AND deleted_at IS NULL"
    pcol = lambda t: {r[1] for r in db.get_conn().execute(f"PRAGMA table_info({t})")}   # noqa: E731

    def by_project(table: str, order: str = "rowid") -> list[dict[str, Any]]:
        if table not in have:
            return []
        with db._lock:
            cols = pcol(table)
        where = f" WHERE project_id IS NULL OR project_id IN ({shown})" if "project_id" in cols else ""
        return _rows(f"SELECT * FROM {table}{where} ORDER BY {order}")

    if dataset == "projects":
        return {"projects": _rows(f"SELECT * FROM projects WHERE id IN ({shown}) ORDER BY id"),
                **{t: by_project(t) for t in ("project_notes", "project_documents", "phases", "todos", "checklist_items") if t in have}}
    if dataset == "tasks":
        tasks = _rows(f"SELECT t.*, p.name AS project FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
                      f"WHERE t.status != 'deleted' AND COALESCE(t.hidden, 0) = 0 AND t.project_id IN ({shown}) ORDER BY t.id")
        ids = {t["id"] for t in tasks}
        events = [e for e in _rows("SELECT * FROM task_events ORDER BY id") if e["task_id"] in ids]
        return {"tasks": tasks, "task_events": events}
    if dataset == "notes":
        return {t: by_project(t) for t in ("pad_notes",) if t in have}
    if dataset == "crm":
        return {t: by_project(t) for t in ("clients", "contacts", "project_contacts", "activities", "activity_contacts", "followups", "won")
                if t in have}
    if dataset == "calendar":
        return {t: by_project(t) for t in ("project_events", "event_contacts", "reminders") if t in have}
    if dataset == "usage":
        return {"usage_log": by_project("usage_log")}
    if dataset == "settings":
        out = []
        for r in _rows("SELECT key, value FROM settings ORDER BY key"):
            if any(h in r["key"].lower() for h in SECRET_HINTS):
                continue
            try:
                out.append({"key": r["key"], "value": json.loads(r["value"])})
            except ValueError:
                out.append({"key": r["key"], "value": r["value"]})
        return {"settings": out, "project_settings": by_project("project_settings", "project_id"),
                **{t: _rows(f"SELECT * FROM {t} ORDER BY id") for t in ("recipes", "schedules") if t in have}}
    raise HTTPException(status_code=400, detail=f"Unknown dataset '{dataset}'")


def export(sets: list[str], fmt: str) -> tuple[bytes, str, str]:
    """(file, media type, name). JSON is one file; CSV is one table per file, so more than one
    table comes as a ZIP of CSVs; ZIP has each dataset as JSON plus its tables as CSV."""
    sets = list(dict.fromkeys(sets))
    if not sets:
        raise HTTPException(status_code=400, detail="Pick at least one dataset")
    if fmt not in FORMATS:
        raise HTTPException(status_code=400, detail="Format must be zip, json or csv")
    data = {s: collect(s) for s in sets}
    stamp = datetime.now().strftime("%Y-%m-%d")
    stem = f"den-{sets[0] if len(sets) == 1 else 'export'}-{stamp}"
    meta = {"exported_at": datetime.now(timezone.utc).isoformat(), "datasets": sets}
    if fmt == "json":
        return json.dumps({**meta, **data}, indent=2, default=str).encode(), "application/json", stem + ".json"
    tables = [(s, t, rows) for s, d in data.items() for t, rows in d.items()]
    if fmt == "csv" and len(tables) == 1:
        rows = tables[0][2]
        if tables[0][1] == "settings":
            rows = [{"key": r["key"], "value": backup._cell(r["value"])} for r in rows]
        return backup._csv_text(rows).encode("utf-8-sig"), "text/csv", stem + ".csv"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s, t, rows in tables:
            if t == "settings":
                rows = [{"key": r["key"], "value": backup._cell(r["value"])} for r in rows]
            z.writestr(f"{s}/{t}.csv", backup._csv_text(rows).encode("utf-8-sig"))
        if fmt == "zip":
            for s, d in data.items():
                z.writestr(f"{s}.json", json.dumps({**meta, "datasets": [s], **d}, indent=2, default=str))
    return buf.getvalue(), "application/zip", stem + ".zip"


def counts() -> dict[str, int]:
    """Rows per dataset, for the export chips."""
    out = {}
    for s in DATASETS:
        try:
            out[s] = sum(len(v) for v in collect(s).values())
        except Exception:
            out[s] = 0
    return out


# ---------------------------------------------------------------- the routes

class PolicyIn(BaseModel):
    auto: Optional[bool] = None
    at: Optional[int] = None
    order: Optional[list[str]] = Field(None, max_length=50)


class LookIn(BaseModel):
    name: Optional[str] = Field(None, max_length=60)
    color: Optional[str] = Field(None, max_length=7)


class OnIn(BaseModel):
    on: bool


class EmailIn(BaseModel):
    email: str = Field(..., max_length=254)


class BackupSettingsIn(BaseModel):
    schedule: Optional[str] = None
    drive: Optional[bool] = None


class RestoreIn(BaseModel):
    confirm: str = ""


class ExportIn(BaseModel):
    sets: list[str] = Field(..., max_length=len(DATASETS))
    format: Literal["zip", "json", "csv"] = "zip"


class PrefsIn(BaseModel):
    risky: Optional[list[str]] = Field(None, max_length=20)


RISKY = ("push", "deploy", "del", "pay", "mail", "deps", "db")


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    # ----------------------------------------------------------- AI accounts
    @r.get("/den/system/accounts")
    def get_accounts(refresh: bool = False):
        return ai_accounts(refresh)

    @r.put("/den/system/accounts/{provider}/policy")
    def put_policy(provider: str, body: PolicyIn):
        return set_policy(provider, body.auto, body.at, body.order)

    @r.put("/den/system/accounts/{provider}/{item_id}/look")
    def put_look(provider: str, item_id: str, body: LookIn):
        return set_look(provider, item_id, body.name, body.color)

    @r.delete("/den/system/accounts/other/{model_id}")
    def remove_other(model_id: str):
        """Log out of an OpenAI-compatible server: its model goes, and its key with it."""
        if not model_admin.is_custom(model_id) or (model_router.get_model(model_id) or {}).get("provider") != "openai_compat":
            raise HTTPException(status_code=404, detail="No such account")
        model_admin.remove(model_id)
        api_keys.set_compat_key(model_id, None)
        return {"ok": True}

    # ----------------------------------------------------------- settings extras
    @r.get("/den/system/prefs")
    def get_prefs():
        st = _state()
        return {"risky": st.get("risky", ["push", "deploy", "del", "pay", "mail"]), "risky_choices": list(RISKY)}

    @r.patch("/den/system/prefs")
    def patch_prefs(body: PrefsIn):
        if body.risky is not None:
            bad = [x for x in body.risky if x not in RISKY]
            if bad:
                raise HTTPException(status_code=400, detail="Unknown: " + ", ".join(bad))
            _save(risky=list(dict.fromkeys(body.risky)))
        return get_prefs()

    @r.get("/den/system/sites")
    def get_sites():
        return site_list()

    @r.put("/den/system/sites/{site_id}")
    def put_site(site_id: str, body: OnIn):
        return set_site(site_id, body.on)

    @r.delete("/den/system/sites/{site_id}")
    def delete_site(site_id: str):
        return remove_site(site_id)

    @r.get("/den/system/tools")
    def get_tools(refresh: bool = False):
        return tool_list(refresh)

    @r.put("/den/system/tools/{tool_id}")
    def put_tool(tool_id: str, body: OnIn):
        return set_tool(tool_id, body.on)

    @r.post("/den/system/tools/{tool_id}/install")
    def post_install(tool_id: str):
        return install_tool(tool_id)

    @r.get("/den/system/push-devices")
    def get_devices():
        return push_devices()

    @r.delete("/den/system/push-devices/{device_id}")
    def delete_device(device_id: int):
        return remove_push_device(device_id)

    @r.put("/den/system/alert-email")
    def put_email(body: EmailIn):
        return set_alert_email(body.email)

    @r.get("/den/system/backups")
    def get_backups():
        return {**backup_settings(), "items": backup_list()}

    @r.post("/den/system/backups")
    def post_backup():
        return make_backup()

    @r.put("/den/system/backups/settings")
    def put_backup_settings(body: BackupSettingsIn):
        return set_backup_settings(body.schedule, body.drive)

    @r.get("/den/system/backups/{name}")
    def download_backup(name: str):
        p = _backup_path(name)
        return Response(p.read_bytes(), media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{p.name}"'})

    @r.post("/den/system/backups/{name}/restore")
    def post_restore(name: str, body: RestoreIn):
        if body.confirm != "replace-everything":
            raise HTTPException(status_code=400, detail="Restore replaces everything - confirm=replace-everything is required.")
        return restore_backup(name)

    @r.get("/den/system/export")
    def export_counts():
        return {"datasets": list(DATASETS), "counts": counts()}

    @r.post("/den/system/export")
    def post_export(body: ExportIn):
        file, media, name = export(body.sets, body.format)
        return Response(file, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return r
