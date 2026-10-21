"""
Which Claude subscription account Claude Code runs on.

"mac" is whatever account this Mac is logged into (`claude /login`, kept in
the Keychain). It can be hidden (`claude_mac_hidden`) once there's a token
account to run on instead - then it isn't listed or used, and it comes back by
itself if the last token account goes. Every other account is a long-lived token
from `claude setup-token`, saved in accounts.json next to this file - owner
read/write only, and gitignored, so a token never reaches GitHub.

The `claude_account` setting is the default account. A project can have its
own instead (its `claude_account` project setting): every Claude Code task of
that project runs on that account, and when it hits its usage limit the
project's tasks wait for that account to reset - they never move to another.
The worker runs one task at a time per account, so projects on different
accounts run side by side (worker.py). A run on a token account
gets CLAUDE_CODE_OAUTH_TOKEN, which Claude Code uses in place of the Keychain
login; nothing else changes - chats, sessions, memory and settings stay in the
same ~/.claude, so a chat can carry on across a switch.

Each account's latest 5-hour / weekly usage is kept per account
(`claude_usage_by_account`); `claude_usage`, which the dashboard's usage bars
read, always holds the active account's.

Stdlib + db only.
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db
import den_platform

FILE = Path(__file__).parent / "accounts.json"
MAC = "mac"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_BIN = den_platform.claude_bin()
PROBE_CWD = Path(__file__).parent / "workspace" / "assistant"

# What a failed run says when the account's login is bad (a revoked or expired token, or a logged-out Mac).
AUTH_FAILED = re.compile(r"failed to authenticate|oauth (access )?token (is |has )?(invalid|expired|revoked)"
                         r"|invalid api key|please run /login", re.IGNORECASE)

# Colors an account can be marked with (dots / side lines across the dashboard). An account
# with none chosen gets one from here by its position in the list, so they differ out of the box.
PALETTE = ["#e8825a", "#4f9de8", "#4fbf8a", "#b583e8", "#e8c24f", "#e8627f", "#4fc6c9", "#9aa3b2"]
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")

_lock = threading.Lock()
_mac_cache: dict[str, Any] = {"at": 0.0, "info": None}
MAC_CACHE_SECONDS = 600


# ---------------------------------------------------------------- storage

def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []
    return [a for a in data.get("accounts", []) if isinstance(a, dict) and a.get("id") and a.get("token")]


def _save(accounts: list[dict[str, Any]]) -> None:
    tmp = FILE.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"accounts": accounts}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, FILE)


def _find(account_id: str) -> Optional[dict[str, Any]]:
    return next((a for a in _load() if a["id"] == account_id), None)


# ---------------------------------------------------------------- the Mac's own login

def mac_login(refresh: bool = False) -> dict[str, Any]:
    """Email / organisation / plan of the account this Mac is logged into (cached - it's a subprocess)."""
    now = time.time()
    if not refresh and _mac_cache["info"] is not None and now - _mac_cache["at"] < MAC_CACHE_SECONDS:
        return _mac_cache["info"]
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
    info: dict[str, Any] = {}
    try:
        r = subprocess.run([CLAUDE_BIN, "auth", "status"], capture_output=True, text=True, timeout=20,
                           env=env, stdin=subprocess.DEVNULL)
        d = json.loads(r.stdout)
        info = {"logged_in": bool(d.get("loggedIn")), "email": d.get("email"),
                "org": d.get("orgName"), "plan": d.get("subscriptionType")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        info = {"logged_in": None}
    _mac_cache.update(at=now, info=info)
    return info


# ---------------------------------------------------------------- listing and choosing

def mac_shown() -> bool:
    """False once the Mac's login is hidden - only while there's a token account to use instead."""
    return not db.get_setting("claude_mac_hidden") or not _load()


def _fallback() -> str:
    """The account to use when none valid is chosen: the Mac's login, or the first token account if it's hidden."""
    if mac_shown():
        return MAC
    return _load()[0]["id"]


def active_id() -> str:
    chosen = db.get_setting("claude_account") or MAC
    return chosen if exists(chosen) else _fallback()


def exists(account_id: Optional[str]) -> bool:
    if account_id == MAC:
        return mac_shown()
    return bool(account_id and _find(account_id))


def for_project(project_id: int, own: Optional[str] = "?", default: Optional[str] = None) -> str:
    """The account a project's Claude Code tasks run on: its own, or the default
    account if it has none. own / default skip the lookups when the caller has
    them already (own=None means "no account of its own")."""
    if own == "?":
        own = db.get_project_settings(project_id)["claude_account"]
    if own and exists(own):
        return own
    return default or active_id()


def set_alias(account_id: str, alias: str) -> bool:
    """A short display name for an account ("" clears it). False if there's no such account."""
    alias = (alias or "").strip()[:24]
    if account_id == MAC:
        db.set_setting("claude_mac_alias", alias)
        return True
    with _lock:
        accounts = _load()
        for a in accounts:
            if a["id"] == account_id:
                a["alias"] = alias
                _save(accounts)
                return True
    return False


def set_details(account_id: str, name: Optional[str] = None, alias: Optional[str] = None, email: Optional[str] = None) -> bool:
    """Edit an account's name, short display name (alias) and email; None leaves a field as it is.
    This Mac's login has no name or email of its own to edit, only an alias. False if there's no such account."""
    if account_id == MAC:
        return set_alias(account_id, alias) if alias is not None else True
    with _lock:
        accounts = _load()
        for a in accounts:
            if a["id"] == account_id:
                if name is not None:
                    if not name.strip():
                        raise ValueError("Give the account a name")
                    a["label"] = name.strip()[:60]
                if alias is not None:
                    a["alias"] = alias.strip()[:24]
                if email is not None:
                    a["email"] = email.strip()[:120] or None
                _save(accounts)
                return True
    return False


def set_color(account_id: str, color: str) -> bool:
    """The color marking an account ("" goes back to the automatic one). False if there's no such account."""
    color = (color or "").strip().lower()
    if color and not COLOR_RE.fullmatch(color):
        raise ValueError("Color must look like #aabbcc")
    if account_id == MAC:
        db.set_setting("claude_mac_color", color)
        return True
    with _lock:
        accounts = _load()
        for a in accounts:
            if a["id"] == account_id:
                a["color"] = color
                _save(accounts)
                return True
    return False


def label(account_id: str) -> str:
    if account_id == MAC:
        if db.get_setting("claude_mac_alias"):
            return db.get_setting("claude_mac_alias")
        info = mac_login()
        return "This Mac's login" + (f" ({info['email']})" if info.get("email") else "")
    a = _find(account_id)
    return (a.get("alias") or a["label"]) if a else account_id


def public_list() -> list[dict[str, Any]]:
    """Every account without its token, the Mac's login first, each with its latest
    usage, when its usage limit resets if it's limited now, and the projects that
    have it as their own."""
    usage = db.get_setting("claude_usage_by_account") or {}
    active = active_id()
    limits = db.claude_account_limits(active)
    hits = db.get_setting("claude_last_hit_by_account") or {}
    out = []
    if mac_shown():
        mac = mac_login()
        out.append({"id": MAC, "kind": "mac", "label": "This Mac's login", "email": mac.get("email"),
                    "org": mac.get("org"), "plan": mac.get("plan"), "logged_in": mac.get("logged_in"),
                    "alias": db.get_setting("claude_mac_alias") or ""})
    for a in _load():
        out.append({"id": a["id"], "kind": "token", "label": a["label"], "email": a.get("email") or None,
                    "alias": a.get("alias") or "", "org": None, "plan": None, "added_at": a.get("added_at"), "auth_error": a.get("auth_error")})
    chosen = {MAC: db.get_setting("claude_mac_color") or ""}
    chosen.update({a["id"]: a.get("color") or "" for a in _load()})
    for i, a in enumerate(out):
        a["color"] = chosen.get(a["id"]) or PALETTE[i % len(PALETTE)]
        a["color_set"] = bool(chosen.get(a["id"]))
    for a in out:
        a["active"] = a["id"] == active
        a["usage"] = current(usage.get(a["id"]))
        a["limited_until"] = limits.get(a["id"])
        hit = hits.get(a["id"])
        if not hit and a["limited_until"]:  # limited before hits were recorded: use the last reading
            u = a["usage"] or {}
            w = u.get("windows") or {}
            hit = {"at": u.get("updated_at"), "five_hour": (w.get("five_hour") or {}).get("utilization"),
                   "seven_day": (w.get("seven_day") or {}).get("utilization")}
        a["last_hit"] = hit
        a["projects"] = [{"id": p["id"], "name": p["name"]} for p in db.projects_on_account(a["id"])]
    return out


def env_for(account_id: str, base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The environment for a Claude Code run on this account."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k != TOKEN_ENV}
    a = None if account_id == MAC else _find(account_id)
    if a:
        env[TOKEN_ENV] = a["token"]
    return env


# ---------------------------------------------------------------- usage

def record_usage(account_id: str, usage: dict[str, Any]) -> None:
    """Keep this account's latest usage, and mirror it to claude_usage if it's the active one."""
    with _lock:
        by = dict(db.get_setting("claude_usage_by_account") or {})
        by[account_id] = usage
        db.set_setting("claude_usage_by_account", by)
        if account_id == active_id():
            db.set_setting("claude_usage", usage)


def record_hit(account_id: str) -> None:
    """Remember that this account just hit its usage limit and how full each window was."""
    usage = (db.get_setting("claude_usage_by_account") or {}).get(account_id) or {}
    pct = {k: (v or {}).get("utilization") for k, v in (usage.get("windows") or {}).items()}
    with _lock:
        by = dict(db.get_setting("claude_last_hit_by_account") or {})
        by[account_id] = {"at": datetime.now(timezone.utc).isoformat(),
                          "five_hour": pct.get("five_hour"), "seven_day": pct.get("seven_day")}
        db.set_setting("claude_last_hit_by_account", by)


def probe(account_id: str, timeout: int = 90) -> dict[str, Any]:
    """One tiny Haiku request (no tools, no saved session) on this account, only to read its
    live 5-hour / weekly usage. Returns {"ok", "usage", "error"}; also records the usage."""
    cmd = [CLAUDE_BIN, "-p", "Reply with just: ok", "--model", "haiku", "--output-format", "stream-json",
           "--verbose", "--tools", "", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"]
    PROBE_CWD.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(cmd, cwd=str(PROBE_CWD), capture_output=True, text=True, timeout=timeout,
                           env=env_for(account_id), stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return {"ok": False, "usage": None, "error": f"Could not find '{CLAUDE_BIN}'"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "usage": None, "error": "Claude Code took too long to answer"}
    usage, result = None, None
    for line in r.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "rate_limit_event":
            usage = usage_from_event(msg.get("rate_limit_info") or {})
        elif msg.get("type") == "result":
            result = msg
    if usage:
        record_usage(account_id, usage)
    failed = r.returncode != 0 or not result or result.get("is_error")
    error = None
    if failed:
        error = str((result or {}).get("result") or r.stderr or r.stdout)[-300:].strip() or "Claude Code failed"
    set_auth_error(account_id, error if error and AUTH_FAILED.search(error) else None)
    return {"ok": not failed, "usage": usage, "error": error}


def probe_in_background(account_id: str) -> None:
    threading.Thread(target=probe, args=(account_id,), daemon=True, name=f"account-probe-{account_id}").start()


def current(usage: Optional[dict[str, Any]], now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """A stored reading as it stands now: a window whose reset time has passed is empty again
    (0%, next reset unknown until a run reports it). A reading only changes when something runs,
    so without this the bars sit at their old level after a reset."""
    if not usage:
        return usage
    now = now or datetime.now(timezone.utc)

    def gone(iso: Any) -> bool:
        try:
            return bool(iso) and datetime.fromisoformat(str(iso).replace("Z", "+00:00")) <= now
        except ValueError:
            return False

    out = dict(usage)
    out["windows"] = {k: ({**w, "utilization": 0, "resets_at": None} if isinstance(w, dict) and gone(w.get("resets_at")) else w)
                      for k, w in (usage.get("windows") or {}).items()}
    if gone(usage.get("resets_at")):
        out.update(status="allowed", limited_window=None, resets_at=None)
    return out


def _epoch_iso(v: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(v), timezone.utc).isoformat() if v else None
    except (TypeError, ValueError):
        return None


def usage_from_event(info: dict[str, Any]) -> dict[str, Any]:
    """A rate_limit_event's rate_limit_info as the dashboard's claude_usage shape."""
    windows = {}
    for name, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict):
            windows[name] = {"utilization": w.get("utilization"), "resets_at": _epoch_iso(w.get("resetsAt"))}
    return {
        "status": info.get("status"),
        "limited_window": info.get("rateLimitType"),
        "resets_at": _epoch_iso(info.get("resetsAt")),
        "windows": windows,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- changes

def set_auth_error(account_id: str, error: Optional[str]) -> None:
    """Remember (or clear) that this token account's login failed, so the dashboard can say so."""
    if account_id == MAC:
        if error:
            _mac_cache.update(at=0.0)   # look again - the Mac may have been logged out
        return
    with _lock:
        accounts = _load()
        for a in accounts:
            if a["id"] == account_id and a.get("auth_error") != error:
                a["auth_error"] = error
                _save(accounts)
                return


def add(label: str, token: str, email: Optional[str] = None) -> dict[str, Any]:
    """Save a `claude setup-token` token as an account, after checking it works. Raises ValueError."""
    label, token, email = label.strip(), token.strip(), (email or "").strip() or None
    if not label:
        raise ValueError("Give the account a name")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,400}", token):
        raise ValueError("That doesn't look like a token from `claude setup-token` - paste the whole line it printed")
    if any(a["token"] == token for a in _load()):
        raise ValueError("That token is already saved")
    account = {"id": uuid.uuid4().hex[:10], "label": label[:60], "email": email, "token": token,
               "added_at": datetime.now(timezone.utc).isoformat()}
    with _lock:
        _save(_load() + [account])
    check = probe(account["id"])
    if not check["ok"]:
        remove(account["id"])
        raise ValueError("Claude Code couldn't log in with that token: " + (check["error"] or "unknown error"))
    return next(a for a in public_list() if a["id"] == account["id"])


def remove(account_id: str) -> bool:
    """Delete a token account, or hide the Mac's login (it has no token to delete)."""
    if account_id == MAC:
        return _hide_mac()
    with _lock:
        accounts = _load()
        left = [a for a in accounts if a["id"] != account_id]
        if len(left) == len(accounts):
            return False
        _save(left)
        by = dict(db.get_setting("claude_usage_by_account") or {})
        by.pop(account_id, None)
        db.set_setting("claude_usage_by_account", by)
    db.clear_project_account(account_id)   # its projects go back to the default account
    if db.get_setting("claude_account") == account_id:
        activate(_fallback())
    return True


def _hide_mac() -> bool:
    if not mac_shown():
        return False
    if not _load():
        raise ValueError("Add another account first - Claude Code needs one to run on")
    with _lock:
        db.set_setting("claude_mac_hidden", True)
        by = dict(db.get_setting("claude_usage_by_account") or {})
        by.pop(MAC, None)
        db.set_setting("claude_usage_by_account", by)
    db.clear_project_account(MAC)   # its projects go back to the default account
    if (db.get_setting("claude_account") or MAC) == MAC:
        activate(_fallback())
    return True


def show_mac() -> None:
    """List the Mac's login again after it was hidden (it doesn't become the default)."""
    db.set_setting("claude_mac_hidden", False)


def activate(account_id: str) -> None:
    """Make this the default account - for the next Claude Code runs of every
    project without its own account - and show its usage."""
    if not exists(account_id):
        raise ValueError("No such account")
    with _lock:
        db.set_setting("claude_account", account_id)
        db.set_setting("claude_usage", (db.get_setting("claude_usage_by_account") or {}).get(account_id))
