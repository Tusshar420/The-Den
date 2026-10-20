"""
API keys for the api lane's providers (OpenAI, Gemini) - as many per provider as you like,
the way accounts.py keeps several Claude accounts.

The key in the launchd plist (OPENAI_API_KEY / GEMINI_API_KEY) is always listed first, as
"<provider>-env". Every other key is pasted on the AI Providers page and kept in api_keys.json
next to this file - owner read/write only, and gitignored, so a key never reaches GitHub.

Each provider has a default key (setting `api_key_default_<provider>`). A call uses the default
first; when that key is rate-limited, out of credit or rejected, the same call is tried on the
provider's other keys in order before giving up (providers.py). Unlike a Claude account there's no
session to keep, so moving to another key mid-thread is harmless.

Stdlib + db only.
"""

import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db

FILE = Path(__file__).parent / "api_keys.json"

# provider -> (label, the plist variable its built-in key comes from, where to check a new key)
PROVIDERS = {
    "openai": ("OpenAI", "OPENAI_API_KEY",
               os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/models"),
    "gemini": ("Google Gemini", "GEMINI_API_KEY",
               os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/") + "/models"),
}

_lock = threading.Lock()


# ---------------------------------------------------------------- storage

def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []
    return [k for k in data.get("keys", []) if isinstance(k, dict) and k.get("id") and k.get("key")
            and k.get("provider") in PROVIDERS]


def _save(keys: list[dict[str, Any]]) -> None:
    tmp = FILE.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"keys": keys, "compat": _raw().get("compat") or {}}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, FILE)


def env_id(provider: str) -> str:
    return f"{provider}-env"


def _env_key(provider: str) -> str:
    return os.environ.get(PROVIDERS[provider][1], "").strip()


def _all(provider: str) -> list[dict[str, Any]]:
    """Every key of a provider, the plist's first: {id, label, key, ...}."""
    out = []
    if _env_key(provider):
        out.append({"id": env_id(provider), "provider": provider, "label": "From the service settings",
                    "key": _env_key(provider), "env": True})
    return out + [k for k in _load() if k["provider"] == provider]


def _provider_of(key_id: str) -> Optional[str]:
    for p in PROVIDERS:
        if key_id == env_id(p):
            return p
    k = next((k for k in _load() if k["id"] == key_id), None)
    return k["provider"] if k else None


# ---------------------------------------------------------------- choosing

def default_id(provider: str) -> Optional[str]:
    keys = _all(provider)
    chosen = db.get_setting(f"api_key_default_{provider}")
    if any(k["id"] == chosen for k in keys):
        return chosen
    return keys[0]["id"] if keys else None


def keys_for(provider: str) -> list[tuple[str, str]]:
    """(id, key) for every key of this provider, the default first - the order a call tries them in."""
    keys = _all(provider)
    first = default_id(provider)
    keys.sort(key=lambda k: k["id"] != first)
    return [(k["id"], k["key"]) for k in keys]


def available(provider: str) -> bool:
    return bool(_all(provider))


def missing_reason(provider: str) -> str:
    return f"{PROVIDERS[provider][1]} is not set"


def label(key_id: str) -> str:
    provider = _provider_of(key_id)
    k = next((k for k in _all(provider) if k["id"] == key_id), None) if provider else None
    return k["label"] if k else key_id


def public_list(provider: str) -> list[dict[str, Any]]:
    """Every key of a provider without the key itself - only its last four characters."""
    first = default_id(provider)
    return [{"id": k["id"], "provider": provider, "label": k["label"], "hint": "…" + k["key"][-4:],
             "env": bool(k.get("env")), "env_var": PROVIDERS[provider][1] if k.get("env") else None,
             "default": k["id"] == first, "added_at": k.get("added_at"), "error": k.get("error")}
            for k in _all(provider)]


def set_error(key_id: str, error: Optional[str]) -> None:
    """Remember (or clear) that a pasted key was rejected or ran out of credit, so the page can say so."""
    with _lock:
        keys = _load()
        for k in keys:
            if k["id"] == key_id and k.get("error") != error:
                k["error"] = error
                _save(keys)
                return


# ---------------------------------------------------------------- changes

def _check(provider: str, key: str, timeout: int = 20) -> None:
    """One free request (list the models) to see the key works. Raises ValueError if it doesn't."""
    url = PROVIDERS[provider][2]
    headers = {"Authorization": f"Bearer {key}"} if provider == "openai" else {"x-goog-api-key": key}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout):
            return
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise ValueError(f"{PROVIDERS[provider][0]} didn't accept that key ({e.code})")
        return                      # 429 / 5xx: the key is real, the provider is just busy
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ValueError(f"Couldn't reach {PROVIDERS[provider][0]} to check the key: {e}")


# ---------------------------------------------------------------- OpenAI-compatible models
# Each one (Mistral, a local Ollama, OpenRouter...) has its own server and key: kept in the
# same owner-only file, under "compat" by model id. A local server may need none.

def _raw() -> dict[str, Any]:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def compat_key(model_id: str) -> Optional[str]:
    return (_raw().get("compat") or {}).get(model_id) or None


def set_compat_key(model_id: str, key: Optional[str]) -> None:
    with _lock:
        data = _raw()
        compat = dict(data.get("compat") or {})
        if key:
            compat[model_id] = key.strip()
        else:
            compat.pop(model_id, None)
        tmp = FILE.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({**data, "keys": data.get("keys", []), "compat": compat}, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, FILE)


def add(provider: str, label: str, key: str, check: bool = True) -> dict[str, Any]:
    """Save another key for a provider, after checking it works. Raises ValueError."""
    if provider not in PROVIDERS:
        raise ValueError("Keys can be added for OpenAI and Google Gemini")
    label, key = (label or "").strip(), (key or "").strip()
    if not label:
        raise ValueError("Give the key a name")
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{20,300}", key):
        raise ValueError("That doesn't look like an API key - paste the whole key")
    if any(k["key"] == key for k in _all(provider)):
        raise ValueError("That key is already saved")
    if check:
        _check(provider, key)
    entry = {"id": uuid.uuid4().hex[:10], "provider": provider, "label": label[:60], "key": key,
             "added_at": datetime.now(timezone.utc).isoformat()}
    with _lock:
        _save(_load() + [entry])
    return next(k for k in public_list(provider) if k["id"] == entry["id"])


def remove(key_id: str) -> bool:
    """Delete a pasted key. The plist's key can't be removed here - it lives in the plist."""
    if any(key_id == env_id(p) for p in PROVIDERS):
        raise ValueError("That key comes from the service settings (the launchd plist) - remove it there")
    with _lock:
        keys = _load()
        left = [k for k in keys if k["id"] != key_id]
        if len(left) == len(keys):
            return False
        _save(left)
    return True


def make_default(key_id: str) -> None:
    provider = _provider_of(key_id)
    if not provider:
        raise ValueError("No such key")
    db.set_setting(f"api_key_default_{provider}", key_id)
