"""
Signing in to the dashboard with a username and password, instead of pasting the API key.

There is one login (the Den has one owner). The password is kept only as a salted PBKDF2 hash,
in login.json next to this file - owner read/write only, and gitignored, like accounts.json.
Signing in gives the browser a random session token in an HttpOnly cookie (scripts on the page
can't read it); only its SHA-256 is stored, so login.json never holds a usable token either.

The API key keeps working everywhere it did (the X-API-Key header - Shortcuts, SwiftBar, curl),
and it is the way to create the login in the first place or reset a forgotten password.
Setting a new login signs every device out.

Stdlib only.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

FILE = Path(__file__).parent / "login.json"
COOKIE = "den_session"
ITERATIONS = 600_000
REMEMBER_SECONDS = 60 * 86400        # "Keep me signed in": 60 days, renewed on every page load
SHORT_SECONDS = 12 * 3600            # otherwise a browser-session cookie, and half a day at most
MIN_PASSWORD = 8
MAX_SESSIONS = 50

_lock = threading.Lock()


# ---------------------------------------------------------------- storage

def _load() -> dict[str, Any]:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    tmp = FILE.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, FILE)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS).hex()


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _live(sessions: dict[str, Any], now: float) -> dict[str, Any]:
    live = {k: v for k, v in sessions.items() if isinstance(v, dict) and v.get("expires", 0) > now}
    if len(live) > MAX_SESSIONS:                      # keep the newest
        live = dict(sorted(live.items(), key=lambda kv: kv[1].get("created", 0))[-MAX_SESSIONS:])
    return live


# ---------------------------------------------------------------- the login

def configured() -> bool:
    d = _load()
    return bool(d.get("username") and d.get("hash") and d.get("salt"))


def username() -> Optional[str]:
    return _load().get("username") if configured() else None


def check_password(name: str, password: str) -> bool:
    d = _load()
    if not configured():
        return False
    # Always hash, so a wrong username takes as long as a wrong password.
    got = _hash_password(password or "", bytes.fromhex(d["salt"]))
    same_name = hmac.compare_digest((name or "").strip().lower().encode(), d["username"].lower().encode())
    return hmac.compare_digest(got.encode(), d["hash"].encode()) and same_name


def validate(name: str, password: str) -> Optional[str]:
    """Why this username/password can't be used, or None."""
    name = (name or "").strip()
    if not name or len(name) > 64:
        return "Pick a username (up to 64 characters)."
    if any(c.isspace() for c in name):
        return "The username can't have spaces."
    if len(password or "") < MIN_PASSWORD:
        return f"The password needs at least {MIN_PASSWORD} characters."
    if len(password) > 256:
        return "That password is too long."
    return None


def set_login(name: str, password: str) -> None:
    """Create or replace the login. Every existing session ends."""
    problem = validate(name, password)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(16)
    with _lock:
        _save({"username": name.strip(), "salt": salt.hex(), "hash": _hash_password(password, salt),
               "changed_at": int(time.time()), "sessions": {}})


# ---------------------------------------------------------------- sessions

def new_session(remember: bool, label: str = "") -> tuple[str, Optional[int]]:
    """A fresh token for a browser, and the cookie's max-age (None: until the browser closes)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    life = REMEMBER_SECONDS if remember else SHORT_SECONDS
    with _lock:
        d = _load()
        sessions = _live(d.get("sessions") or {}, now)
        sessions[_token_id(token)] = {"created": int(now), "expires": int(now + life), "remember": bool(remember),
                                      "label": (label or "")[:200]}
        d["sessions"] = _live(sessions, now)
        _save(d)
    return token, (life if remember else None)


def valid_session(token: Optional[str]) -> bool:
    if not token or len(token) > 200:
        return False
    s = (_load().get("sessions") or {}).get(_token_id(token))
    return isinstance(s, dict) and s.get("expires", 0) > time.time()


def renew(token: Optional[str]) -> Optional[int]:
    """Push a remembered session's expiry out again; the cookie's new max-age, or None if not renewed."""
    if not valid_session(token):
        return None
    with _lock:
        d = _load()
        s = (d.get("sessions") or {}).get(_token_id(token))
        if not s or not s.get("remember"):
            return None
        s["expires"] = int(time.time() + REMEMBER_SECONDS)
        _save(d)
    return REMEMBER_SECONDS


def end_session(token: Optional[str]) -> None:
    if not token:
        return
    with _lock:
        d = _load()
        if (d.get("sessions") or {}).pop(_token_id(token), None) is not None:
            _save(d)


def end_all() -> None:
    with _lock:
        d = _load()
        if d.get("sessions"):
            d["sessions"] = {}
            _save(d)
