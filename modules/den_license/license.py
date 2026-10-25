"""
Licensing for the Den: who may run a given-away copy, on which machine, and with which tools.

The owner's Den issues a **licence** for each person. A licence is a small signed token holding the
person's name, which tools they may use, when it was issued, an optional end date, and how many days
the copy may run without checking in. It is signed with the owner's licence key (Ed25519, kept in the
Keychain), so it can't be faked or edited.

A given-away Den stores its licence and, on a schedule, **checks in** with the owner's Den. The
check-in does three things: it proves the licence is still valid, it **locks the licence to one
machine** (the owner's Den remembers the first machine a licence appears on and refuses a different
one — so a copied folder on another computer won't run), and it lets the owner **change which tools a
person may use, or revoke them, and have it take effect** at the next check-in. If a copy can't reach
the owner's Den, it keeps working until the offline window runs out, then it locks (it never deletes
anything).

This file is the engine (signing, the machine id, the owner's licence store, the check-in server, and
the install-side decision). `module.py` wires it to the Den. Enforcing the decision (pausing work,
limiting tools) is done by the module and the core; this file only decides.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

import platform as _platform

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

log = logging.getLogger("the-den.module.den_license")

KEY_ACCOUNT = "den-license-signing-key"
API = "/den-license/v1"
# The fields a licence token carries and that the signature covers. Machine binding is NOT in here -
# it is enforced by the owner's Den at check-in (first machine wins), so a licence can be issued
# before the person's machine is known.
SIGNED = ("id", "name", "tools", "issued_at", "expires_at", "max_offline_days", "server_url", "nonce")
# The tools a person can be allowed (same ids as tools.json). "*" means all of them.
DEFAULT_CATALOG = ["filesystem", "terminal", "git", "github", "web", "image_generation",
                   "gmail", "calendar", "reminders", "coding_handoff", "workspace", "ai_models"]
MAX_NAME = 80
MAX_OFFLINE_CAP = 365


class LicenseError(Exception):
    """Something the owner or the person can act on - the message is shown as is."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def machine_id() -> str:
    """A stable id for this computer. The same machine gives the same value; a different computer
    gives a different one. Not a secret - it only has to be steady and roughly unique."""
    raw = f"{_platform.system()}|{_platform.node()}|{uuid.getnode()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------- signing

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps({k: payload.get(k) for k in SIGNED}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def signing_key() -> Ed25519PrivateKey:
    """The owner's licence key, from the Keychain; made the first time it's needed."""
    from assistant import token_store
    stored = token_store.get(KEY_ACCOUNT)
    if stored and stored.get("private"):
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(stored["private"]))
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    token_store.put(KEY_ACCOUNT, {"private": raw.hex(), "created_at": _iso(_now())})
    return key


def public_key_b64(key: Optional[Ed25519PrivateKey] = None) -> str:
    key = key or signing_key()
    return _b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def public_from_b64(text: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode((text or "").strip(), validate=True))
    except Exception:
        raise LicenseError("The owner's licence key doesn't look right - copy it again from the owner.")


def sign_payload(key: Ed25519PrivateKey, payload: dict[str, Any]) -> str:
    return _b64(key.sign(canonical(payload)))


def make_token(key: Ed25519PrivateKey, payload: dict[str, Any]) -> str:
    """A licence token = the payload plus its signature, as one base64 string the owner hands over."""
    body = {**{k: payload.get(k) for k in SIGNED}, "signature": sign_payload(key, payload)}
    return base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


def read_token(token: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode((token or "").strip().encode()))
    except Exception:
        raise LicenseError("This licence key can't be read - paste the whole thing the owner gave you.")
    if not isinstance(data, dict) or not data.get("id") or not data.get("signature"):
        raise LicenseError("This licence key is incomplete.")
    return data


def verify_token(public_b64: str, token_data: dict[str, Any]) -> None:
    key = public_from_b64(public_b64)
    try:
        key.verify(base64.b64decode(token_data.get("signature", ""), validate=True), canonical(token_data))
    except (InvalidSignature, ValueError):
        raise LicenseError("This licence wasn't issued by the owner, so it was not accepted.")


def clean_tools(tools: Any, catalog: Optional[list[str]] = None) -> Any:
    """`"*"` (all) or a de-duplicated list of known tool ids."""
    cat = catalog or DEFAULT_CATALOG
    if tools == "*" or tools is None:
        return "*"
    if not isinstance(tools, list):
        raise LicenseError("Tools must be a list, or \"*\" for all of them.")
    out = [t for t in dict.fromkeys(tools) if t in cat]
    return out


# ---------------------------------------------------------------- owner side

class LicenseManager:
    """The owner's record of every licence issued, and the check-in logic that locks each to a
    machine and answers with the person's current tools. Stored in the module's own state."""

    def __init__(self, get: Callable[[str, Any], Any], put: Callable[[str, Any], None],
                 key_fn: Callable[[], Ed25519PrivateKey] = signing_key, catalog: Optional[list[str]] = None):
        self.get, self.put, self.key_fn = get, put, key_fn
        self.catalog = catalog or DEFAULT_CATALOG
        self._lock = threading.Lock()
        self._seen_at: dict[str, float] = {}

    def _all(self) -> list[dict[str, Any]]:
        v = self.get("licenses", [])
        return v if isinstance(v, list) else []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.put("licenses", rows)

    def public(self) -> list[dict[str, Any]]:
        """What the dashboard shows - no private key material (there is none stored per licence)."""
        now = _now()
        out = []
        for r in self._all():
            last = _parse(r.get("last_seen"))
            recent = bool(last and (now - last).days < 14)
            out.append({k: r.get(k) for k in ("id", "name", "tools", "machine_id", "issued_at",
                        "expires_at", "max_offline_days", "revoked", "last_seen", "last_version", "last_ip")}
                       | {"online": recent})
        return sorted(out, key=lambda x: x.get("issued_at") or "", reverse=True)

    def create(self, name: str, tools: Any = "*", expires_days: Optional[int] = None,
               max_offline_days: int = 30, server_url: str = "") -> tuple[dict[str, Any], str]:
        name = " ".join(str(name or "").split())[:MAX_NAME]
        if not name:
            raise LicenseError("Give the person (or their computer) a name.")
        try:
            max_offline_days = max(1, min(int(max_offline_days), MAX_OFFLINE_CAP))
        except (TypeError, ValueError):
            max_offline_days = 30
        issued = _now()
        expires = None
        if expires_days:
            try:
                expires = _iso(issued + timedelta(days=int(expires_days)))
            except (TypeError, ValueError):
                expires = None
        payload = {"id": secrets.token_hex(8), "name": name, "tools": clean_tools(tools, self.catalog),
                   "issued_at": _iso(issued), "expires_at": expires, "max_offline_days": max_offline_days,
                   "server_url": str(server_url or "").strip().rstrip("/"), "nonce": secrets.token_hex(6)}
        token = make_token(self.key_fn(), payload)
        record = {**payload, "machine_id": None, "revoked": False, "last_seen": None,
                  "last_version": None, "last_ip": None, "created_at": _iso(issued)}
        with self._lock:
            self._save(self._all() + [record])
        return record, token

    def _find(self, rows: list[dict[str, Any]], lic_id: str) -> Optional[dict[str, Any]]:
        return next((r for r in rows if r.get("id") == lic_id), None)

    def revoke(self, lic_id: str, revoked: bool = True) -> bool:
        with self._lock:
            rows = self._all()
            r = self._find(rows, lic_id)
            if not r:
                return False
            r["revoked"] = bool(revoked)
            self._save(rows)
        return True

    def set_tools(self, lic_id: str, tools: Any) -> bool:
        """Change which tools a person may use. Takes effect at their next check-in (live)."""
        with self._lock:
            rows = self._all()
            r = self._find(rows, lic_id)
            if not r:
                return False
            r["tools"] = clean_tools(tools, self.catalog)
            self._save(rows)
        return True

    def remove(self, lic_id: str) -> bool:
        with self._lock:
            rows = self._all()
            keep = [r for r in rows if r.get("id") != lic_id]
            self._save(keep)
        return len(keep) != len(rows)

    def check(self, token_data: dict[str, Any], machine: str, version: str, ip: str) -> dict[str, Any]:
        """A check-in from a given-away Den. Binds the licence to the first machine it sees, rejects a
        different machine, notes it, and returns the person's current tools (or that they're revoked)."""
        lic_id = token_data.get("id")
        with self._lock:
            rows = self._all()
            r = self._find(rows, lic_id)
            if not r:
                return {"valid": False, "reason": "unknown"}
            if r.get("revoked"):
                return {"valid": False, "reason": "revoked", "name": r.get("name")}
            machine = (machine or "")[:64]
            if not r.get("machine_id"):
                r["machine_id"] = machine                      # first machine wins
            elif machine and not hmac.compare_digest(str(r["machine_id"]), machine):
                r["last_seen"] = _iso(_now())
                self._save(rows)
                return {"valid": False, "reason": "machine", "name": r.get("name")}
            fresh = time.time() - self._seen_at.get(lic_id, 0) < 60
            if not fresh:
                r.update(last_seen=_iso(_now()), last_version=(version or r.get("last_version")), last_ip=ip)
                self._save(rows)
                self._seen_at[lic_id] = time.time()
            return {"valid": True, "revoked": False, "tools": r.get("tools", "*"), "name": r.get("name")}


# ---------------------------------------------------------------- install side

def evaluate(state: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """Decide, from what this Den knows, whether it may run and with which tools. Works offline -
    it only uses the stored licence and the last check-in result. Never raises."""
    now = now or _now()
    token = state.get("token")
    if not token:
        return {"status": "none", "reason": "This Den has no licence yet.", "tools": []}
    try:
        data = read_token(token)
        verify_token(state.get("public_key", ""), data)
    except LicenseError as e:
        return {"status": "locked", "reason": str(e), "tools": []}
    exp = _parse(data.get("expires_at"))
    if exp and now > exp:
        return {"status": "locked", "reason": "This licence has ended. Ask the owner for a new one.", "tools": []}
    last = state.get("last_result") or {}
    if last.get("revoked"):
        return {"status": "locked", "reason": "This licence was turned off by the owner.", "tools": []}
    if last.get("reason") == "machine":
        return {"status": "locked", "reason": "This licence is tied to a different computer.", "tools": []}
    max_off = int(data.get("max_offline_days") or 30)
    last_in = _parse(state.get("last_checkin"))
    if last_in is None:
        last_in = _parse(state.get("first_seen"))
    offline_days = (now - last_in).days if last_in else None
    if offline_days is not None and offline_days > max_off:
        return {"status": "locked", "tools": [],
                "reason": f"This Den hasn't reached the owner in {offline_days} days (limit {max_off}). "
                          "Connect to the owner's network so it can check in."}
    tools = last.get("tools", data.get("tools", "*"))
    return {"status": "ok", "reason": "", "tools": tools, "name": data.get("name"),
            "offline_days": offline_days, "max_offline_days": max_off, "expires_at": data.get("expires_at")}


class CheckinClient:
    """A given-away Den talking to the owner's Den."""

    def __init__(self, get: Callable[[str, Any], Any], put: Callable[[str, Any], None]):
        self.get, self.put = get, put

    def install(self, token: str, public_key: str, server_url: str) -> dict[str, Any]:
        data = read_token(token)                               # raises if unreadable
        verify_token(public_key, data)                        # raises if not the owner's
        self.put("license", {"token": token, "public_key": public_key.strip(),
                             "server_url": str(server_url or data.get("server_url") or "").strip().rstrip("/"),
                             "first_seen": _iso(_now()), "last_checkin": None, "last_result": {}})
        return self.state()

    def state(self) -> dict[str, Any]:
        st = self.get("license", {}) or {}
        ev = evaluate(st)
        return {**ev, "configured": bool(st.get("token")), "server_url": st.get("server_url", ""),
                "last_checkin": st.get("last_checkin")}

    def checkin(self, version: str = "") -> dict[str, Any]:
        import urllib.error
        import urllib.request
        st = self.get("license", {}) or {}
        if not st.get("token") or not st.get("server_url"):
            return self.state()
        body = json.dumps({"token": st["token"], "machine": machine_id(), "version": version}).encode()
        req = urllib.request.Request(st["server_url"] + API + "/check", data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "TheDen-license"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                result = json.loads(r.read(64 * 1024).decode())
        except (urllib.error.URLError, OSError, ValueError):
            return self.state()                               # offline: keep going until the window runs out
        if isinstance(result, dict):
            st["last_result"] = {"revoked": result.get("revoked", not result.get("valid", True)),
                                 "reason": result.get("reason", ""), "tools": result.get("tools", "*"),
                                 "valid": result.get("valid", False)}
            if result.get("valid"):
                st["last_checkin"] = _iso(_now())
            self.put("license", st)
        return self.state()


# ---------------------------------------------------------------- check-in server (owner)

class _Handler(BaseHTTPRequestHandler):
    server_version = "DenLicense/1"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("license server: " + fmt, *args)

    def _json(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:   # noqa: N802
        srv: "LicenseServer" = self.server.den   # type: ignore[attr-defined]
        ip = self.client_address[0]
        if srv.locked(ip):
            return self._json(429, {"valid": False, "reason": "rate_limited"})
        if self.path.split("?", 1)[0] != f"{API}/check":
            return self._json(404, {"valid": False, "reason": "not_found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(min(length, 64 * 1024)).decode())
            data = read_token(payload.get("token", ""))
            verify_token(srv.public_key, data)                # the token must be one we signed
        except Exception:
            srv.failed(ip)
            return self._json(400, {"valid": False, "reason": "bad_request"})
        result = srv.manager.check(data, payload.get("machine", ""), payload.get("version", ""), ip)
        self._json(200, result)


class LicenseServer:
    FAIL_LIMIT, FAIL_WINDOW = 30, 600

    def __init__(self, host: str, port: int, manager: LicenseManager, public_key: str):
        self.host, self.port, self.manager, self.public_key = host, port, manager, public_key
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._fails: dict[str, list[float]] = {}
        self.error = ""

    def locked(self, ip: str) -> bool:
        now = time.time()
        self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < self.FAIL_WINDOW]
        return len(self._fails[ip]) >= self.FAIL_LIMIT

    def failed(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def start(self) -> None:
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as e:
            self.error = f"Couldn't open port {self.port}: {e.strerror or e}"
            log.warning("license server not started: %s", self.error)
            return
        self.httpd.daemon_threads = True
        self.httpd.den = self   # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="den-license-server", daemon=True).start()
        log.info("license server on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    @property
    def running(self) -> bool:
        return self.httpd is not None
