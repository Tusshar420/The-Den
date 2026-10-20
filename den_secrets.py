"""
Secrets for The Den 2.0: the hidden private vault (notes and files in folders) and each project's
Env & credentials tab (logins for GitHub, Vercel, Hostinger...).

How they're protected:
- Everything secret is encrypted (AES-GCM) with a random 256-bit key that lives in the login Keychain
  (assistant.token_store), never in queue.db. A copy of the database or of vault/ is unreadable on its own.
- The PIN is only a gate, checked here: stored as a salted scrypt hash, never sent back, never the key.
  The right PIN opens a short session (an unlock token, kept in memory) that expires after the owner's
  "Lock again after" setting. Wrong PINs lock the gate for a while, longer each time.
- Every route also needs the API key (the router's dependency), so the PIN is a second factor, not the only one.
- Agents can't use `security` to read the key without asking: risk.py marks Keychain reads High.
"""

import base64
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import db
from assistant import token_store

KEY_ACCOUNT = "den-secrets-key"
SCOPES = ("vault", "creds")
FOLDERS = ("Private notes", "IDs & documents", "Banking", "Contracts")
PLATFORMS = ("GitHub", "Vercel", "Hostinger", "AWS", "Google Workspace", "Stripe", "Figma", "Cloudflare", "cPanel", "Custom")
ENVS = ("All", "Production", "Staging", "Dev")
LOCK_CHOICES = (1, 5, 15, None)          # minutes; None = "Never" (still ends when the service restarts)
NEVER_SECONDS = 12 * 3600                # "Never" still means a working day, not forever
MAX_FILE_BYTES = 25 * 1024 * 1024
FREE_TRIES = 5                           # wrong PINs before the gate locks
LOCKOUT_SECONDS = 60                     # first lockout; doubles each time, up to an hour

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}       # unlock token -> {scope, until}
_key_cache: dict[str, bytes] = {}
_ready = False


class SecretsError(HTTPException):
    pass


# ------------------------------------------------------------------ storage

def _init() -> None:
    global _ready
    if _ready:
        return
    with db._lock:
        conn = db.get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS den_secret_pins (scope TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS den_secret_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS den_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, label TEXT NOT NULL, platform TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '', env TEXT NOT NULL DEFAULT 'All', secret TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS den_vault_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, folder TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                mime TEXT NOT NULL DEFAULT 'application/octet-stream', size INTEGER NOT NULL DEFAULT 0, secret TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        conn.commit()
    _ready = True


def _state(k: str, default: Any = None) -> Any:
    _init()
    with db._lock:
        r = db.get_conn().execute("SELECT v FROM den_secret_state WHERE k = ?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def _set_state(k: str, v: Any) -> None:
    _init()
    with db._lock:
        conn = db.get_conn()
        conn.execute("INSERT INTO den_secret_state (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, json.dumps(v)))
        conn.commit()


def vault_dir() -> Path:
    return Path(db.DB_PATH).parent / "vault"


# ------------------------------------------------------------------ the key and the cipher

def _key() -> bytes:
    with _lock:
        if "k" in _key_cache:
            return _key_cache["k"]
        try:
            stored = token_store.get(KEY_ACCOUNT)
            if not stored:
                stored = {"k": base64.b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")}
                token_store.put(KEY_ACCOUNT, stored)
        except token_store.TokenStoreError as e:
            raise SecretsError(status_code=503, detail=f"The Keychain isn't available: {e}")
        _key_cache["k"] = base64.b64decode(stored["k"])
        return _key_cache["k"]


def seal(data: bytes, aad: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_key()).encrypt(nonce, data, aad.encode())


def unseal(blob: bytes, aad: str) -> bytes:
    try:
        return AESGCM(_key()).decrypt(blob[:12], blob[12:], aad.encode())
    except Exception:
        raise SecretsError(status_code=500, detail="Couldn't decrypt this item (was the Keychain key replaced?)")


def _seal_json(obj: dict[str, Any], aad: str) -> str:
    return base64.b64encode(seal(json.dumps(obj).encode(), aad)).decode("ascii")


def _unseal_json(text: str, aad: str) -> dict[str, Any]:
    return json.loads(unseal(base64.b64decode(text), aad))


# ------------------------------------------------------------------ PINs

def _hash(pin: str, salt: bytes) -> str:
    return Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1).derive(pin.encode()).hex()


def _pin_scope(scope: str) -> str:
    """With "one PIN for both" on, the vault PIN guards credentials too."""
    return "vault" if _state("one_pin", True) else scope


def _pin_row(scope: str) -> Optional[dict[str, Any]]:
    _init()
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM den_secret_pins WHERE scope = ?", (scope,)).fetchone()
    return dict(r) if r else None


def _check_pin(scope: str, pin: str) -> bool:
    row = _pin_row(scope)
    return bool(row) and hmac.compare_digest(_hash(pin, bytes.fromhex(row["salt"])), row["hash"])


def _gate(scope: str) -> None:
    """Refuse while locked out after too many wrong PINs."""
    until = _state(f"lockout_until:{scope}", 0)
    if until and until > time.time():
        raise SecretsError(status_code=429, detail=f"Too many wrong PINs. Try again in {int(until - time.time()) + 1}s.")


def _wrong(scope: str) -> None:
    fails = _state(f"fails:{scope}", 0) + 1
    _set_state(f"fails:{scope}", fails)
    if fails >= FREE_TRIES:
        strikes = _state(f"strikes:{scope}", 0) + 1
        _set_state(f"strikes:{scope}", strikes)
        _set_state(f"lockout_until:{scope}", time.time() + min(3600, LOCKOUT_SECONDS * 2 ** (strikes - 1)))
        _set_state(f"fails:{scope}", 0)


def _right(scope: str) -> None:
    for k in ("fails", "strikes", "lockout_until"):
        _set_state(f"{k}:{scope}", 0)


def _valid_pin(pin: Any) -> str:
    pin = str(pin or "")
    if len(pin) != 4 or not pin.isdigit():
        raise SecretsError(status_code=400, detail="A PIN is 4 digits")
    return pin


def status() -> dict[str, Any]:
    one = bool(_state("one_pin", True))
    return {"one_pin": one, "lock_after_min": _state("lock_after_min", 5),
            "vault": {"set": bool(_pin_row("vault"))}, "creds": {"set": bool(_pin_row("vault" if one else "creds"))}}


class PinIn(BaseModel):
    scope: str
    current: Optional[str] = None
    new: str


class SettingsIn(BaseModel):
    one_pin: Optional[bool] = None
    lock_after_min: Optional[int] = Field(None)
    never: Optional[bool] = None          # lock_after_min = null can't be told apart from "not sent"


def set_pin(body: PinIn) -> dict[str, Any]:
    if body.scope not in (*SCOPES, "both"):
        raise SecretsError(status_code=400, detail="Unknown PIN")
    new = _valid_pin(body.new)
    targets = ["vault"] if body.scope == "both" or _state("one_pin", True) else [body.scope]
    for scope in targets:
        _gate(scope)
        if _pin_row(scope):
            if not body.current or not _check_pin(scope, str(body.current)):
                _wrong(scope)
                raise SecretsError(status_code=403, detail="The current PIN isn't right")
            _right(scope)
        salt = os.urandom(16)
        with db._lock:
            conn = db.get_conn()
            conn.execute("INSERT INTO den_secret_pins (scope, salt, hash, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET "
                         "salt = excluded.salt, hash = excluded.hash, updated_at = excluded.updated_at", (scope, salt.hex(), _hash(new, salt), db._now()))
            conn.commit()
    lock_all()                    # a new PIN ends every open session
    return {"ok": True}


def update_settings(body: SettingsIn) -> dict[str, Any]:
    if body.one_pin is not None:
        if not body.one_pin and not _pin_row("creds") and _pin_row("vault"):
            # Splitting: credentials start with a copy of the shared PIN until it's changed.
            row = _pin_row("vault")
            with db._lock:
                conn = db.get_conn()
                conn.execute("INSERT OR REPLACE INTO den_secret_pins (scope, salt, hash, updated_at) VALUES ('creds', ?, ?, ?)", (row["salt"], row["hash"], db._now()))
                conn.commit()
        _set_state("one_pin", bool(body.one_pin))
        lock_all()
    if body.never:
        _set_state("lock_after_min", None)
    elif body.lock_after_min is not None:
        if body.lock_after_min not in LOCK_CHOICES:
            raise SecretsError(status_code=400, detail="Lock after 1, 5 or 15 minutes, or never")
        _set_state("lock_after_min", body.lock_after_min)
    return status()


# ------------------------------------------------------------------ sessions

def unlock(scope: str, pin: str) -> dict[str, Any]:
    if scope not in SCOPES:
        raise SecretsError(status_code=400, detail="Unknown lock")
    gate = _pin_scope(scope)
    _gate(gate)
    if not _pin_row(gate):
        raise SecretsError(status_code=409, detail="No PIN yet - set one in Settings › PINs & locks")
    if not _check_pin(gate, _valid_pin(pin)):
        _wrong(gate)
        left = FREE_TRIES - _state(f"fails:{gate}", 0)
        raise SecretsError(status_code=403, detail="Wrong PIN" + (f" · {left} tries left" if 0 < left < FREE_TRIES else ""))
    _right(gate)
    minutes = _state("lock_after_min", 5)
    seconds = minutes * 60 if minutes else NEVER_SECONDS
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.time()
        for t in [t for t, s in _sessions.items() if s["until"] < now]:
            _sessions.pop(t, None)
        _sessions[token] = {"scope": scope, "until": now + seconds, "seconds": seconds}
    return {"token": token, "expires_in": seconds}


def lock_all() -> None:
    with _lock:
        _sessions.clear()


def require(scope: str, token: Optional[str]) -> None:
    """The request carries a live unlock token for this lock; each use keeps it alive a little longer."""
    with _lock:
        s = _sessions.get(token or "")
        now = time.time()
        if not s or s["until"] < now or s["scope"] != scope:
            if s:
                _sessions.pop(token, None)
            raise SecretsError(status_code=401, detail="Locked")
        s["until"] = max(s["until"], now + min(s["seconds"], 300))


# ------------------------------------------------------------------ credentials

class CredIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    platform: str = "Custom"
    url: str = Field("", max_length=500)
    env: str = "All"
    username: str = Field("", max_length=300)
    password: str = Field("", max_length=2000)
    note: str = Field("", max_length=4000)


class CredPatch(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=120)
    platform: Optional[str] = None
    url: Optional[str] = Field(None, max_length=500)
    env: Optional[str] = None
    username: Optional[str] = Field(None, max_length=300)
    password: Optional[str] = Field(None, max_length=2000)
    note: Optional[str] = Field(None, max_length=4000)


def _cred_aad(cid: int, project_id: int) -> str:
    return f"cred:{project_id}:{cid}"


def _check_cred(platform: Optional[str], env: Optional[str], url: Optional[str]) -> None:
    if platform is not None and platform not in PLATFORMS:
        raise SecretsError(status_code=400, detail="Unknown platform")
    if env is not None and env not in ENVS:
        raise SecretsError(status_code=400, detail="Unknown environment")
    if url and not (url.startswith("https://") or url.startswith("http://")):
        raise SecretsError(status_code=400, detail="The login URL must start with http:// or https://")


def _cred_view(r: dict[str, Any]) -> dict[str, Any]:
    inner = _unseal_json(r["secret"], _cred_aad(r["id"], r["project_id"]))
    # The password stays out of the list - it's fetched on its own when revealed or copied.
    return {"id": r["id"], "project_id": r["project_id"], "label": r["label"], "platform": r["platform"], "url": r["url"],
            "env": r["env"], "username": inner.get("username", ""), "note": inner.get("note", ""),
            "has_password": bool(inner.get("password")), "updated_at": r["updated_at"]}


def list_creds(project_id: int) -> list[dict[str, Any]]:
    _init()
    with db._lock:
        rows = [dict(r) for r in db.get_conn().execute("SELECT * FROM den_credentials WHERE project_id = ? ORDER BY label COLLATE NOCASE", (project_id,)).fetchall()]
    return [_cred_view(r) for r in rows]


def _cred_row(cid: int) -> dict[str, Any]:
    _init()
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM den_credentials WHERE id = ?", (cid,)).fetchone()
    if not r:
        raise SecretsError(status_code=404, detail="No such login")
    return dict(r)


def add_cred(project_id: int, body: CredIn) -> dict[str, Any]:
    _init()
    if not db.get_project(project_id):
        raise SecretsError(status_code=404, detail="Project not found")
    _check_cred(body.platform, body.env, body.url)
    now = db._now()
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute("INSERT INTO den_credentials (project_id, label, platform, url, env, secret, created_at, updated_at) VALUES (?, ?, ?, ?, ?, '', ?, ?)",
                           (project_id, body.label.strip(), body.platform, body.url.strip(), body.env, now, now))
        cid = cur.lastrowid
        conn.execute("UPDATE den_credentials SET secret = ? WHERE id = ?",
                     (_seal_json({"username": body.username, "password": body.password, "note": body.note}, _cred_aad(cid, project_id)), cid))
        conn.commit()
    return _cred_view(_cred_row(cid))


def edit_cred(cid: int, body: CredPatch) -> dict[str, Any]:
    r = _cred_row(cid)
    ch = body.model_dump(exclude_none=True)
    _check_cred(ch.get("platform"), ch.get("env"), ch.get("url"))
    inner = _unseal_json(r["secret"], _cred_aad(cid, r["project_id"]))
    for k in ("username", "password", "note"):
        if k in ch:
            inner[k] = ch.pop(k)
    plain = {k: (v.strip() if isinstance(v, str) else v) for k, v in ch.items()}
    with db._lock:
        conn = db.get_conn()
        sets = ", ".join(f"{k} = ?" for k in plain)
        conn.execute(f"UPDATE den_credentials SET {sets + ', ' if sets else ''}secret = ?, updated_at = ? WHERE id = ?",
                     [*plain.values(), _seal_json(inner, _cred_aad(cid, r["project_id"])), db._now(), cid])
        conn.commit()
    return _cred_view(_cred_row(cid))


def cred_password(cid: int) -> dict[str, Any]:
    r = _cred_row(cid)
    return {"password": _unseal_json(r["secret"], _cred_aad(cid, r["project_id"])).get("password", "")}


def delete_cred(cid: int) -> dict[str, Any]:
    _cred_row(cid)
    with db._lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM den_credentials WHERE id = ?", (cid,))
        conn.commit()
    return {"ok": True}


# ------------------------------------------------------------------ vault

def _item(iid: int) -> dict[str, Any]:
    _init()
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM den_vault_items WHERE id = ?", (iid,)).fetchone()
    if not r:
        raise SecretsError(status_code=404, detail="Not in the vault")
    return dict(r)


def _item_view(r: dict[str, Any]) -> dict[str, Any]:
    out = {"id": r["id"], "folder": r["folder"], "kind": r["kind"], "name": r["name"], "mime": r["mime"], "size": r["size"],
           "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if r["kind"] == "note" and r["secret"]:
        out["text"] = _unseal_json(r["secret"], f"vault:{r['id']}").get("text", "")
    return out


def vault_list() -> dict[str, Any]:
    _init()
    with db._lock:
        rows = [dict(r) for r in db.get_conn().execute("SELECT * FROM den_vault_items ORDER BY folder, updated_at DESC").fetchall()]
    items = [_item_view(r) for r in rows]
    return {"folders": [{"name": f, "items": [i for i in items if i["folder"] == f]} for f in FOLDERS]}


class NoteIn(BaseModel):
    folder: str = "Private notes"
    name: str = Field("Note", max_length=200)
    text: str = Field("", max_length=100000)


def _folder(f: str) -> str:
    _init()
    if f not in FOLDERS:
        raise SecretsError(status_code=400, detail="Unknown folder")
    return f


def save_note(body: NoteIn, iid: Optional[int] = None) -> dict[str, Any]:
    folder, now = _folder(body.folder), db._now()
    with db._lock:
        conn = db.get_conn()
        if iid is None:
            iid = conn.execute("INSERT INTO den_vault_items (folder, kind, name, created_at, updated_at) VALUES (?, 'note', ?, ?, ?)",
                               (folder, body.name.strip() or "Note", now, now)).lastrowid
        elif not conn.execute("SELECT 1 FROM den_vault_items WHERE id = ? AND kind = 'note'", (iid,)).fetchone():
            raise SecretsError(status_code=404, detail="Not in the vault")
        conn.execute("UPDATE den_vault_items SET folder = ?, name = ?, secret = ?, size = ?, updated_at = ? WHERE id = ?",
                     (folder, body.name.strip() or "Note", _seal_json({"text": body.text}, f"vault:{iid}"), len(body.text), now, iid))
        conn.commit()
    return _item_view(_item(iid))


def add_file(folder: str, name: str, mime: str, data: bytes) -> dict[str, Any]:
    folder = _folder(folder)
    if not data:
        raise SecretsError(status_code=400, detail="The file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise SecretsError(status_code=413, detail="Files in the vault can be up to 25 MB")
    name = (Path(name).name or "file")[:200]
    now = db._now()
    with db._lock:
        conn = db.get_conn()
        iid = conn.execute("INSERT INTO den_vault_items (folder, kind, name, mime, size, created_at, updated_at) VALUES (?, 'file', ?, ?, ?, ?, ?)",
                           (folder, name, (mime or "application/octet-stream")[:100], len(data), now, now)).lastrowid
        conn.commit()
    d = vault_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = d / f"{iid}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(seal(data, f"vault:{iid}"))
    os.replace(tmp, d / f"{iid}.bin")
    return _item_view(_item(iid))


def read_file(iid: int) -> tuple[dict[str, Any], bytes]:
    r = _item(iid)
    if r["kind"] != "file":
        raise SecretsError(status_code=400, detail="That's a note, not a file")
    try:
        blob = (vault_dir() / f"{iid}.bin").read_bytes()
    except FileNotFoundError:
        raise SecretsError(status_code=404, detail="The file is missing from the vault folder")
    return r, unseal(blob, f"vault:{iid}")


def delete_item(iid: int) -> dict[str, Any]:
    r = _item(iid)
    with db._lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM den_vault_items WHERE id = ?", (iid,))
        conn.commit()
    if r["kind"] == "file":
        (vault_dir() / f"{iid}.bin").unlink(missing_ok=True)
    return {"ok": True}


# ------------------------------------------------------------------ routes

class UnlockIn(BaseModel):
    scope: str
    pin: str


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])
    no_store = {"Cache-Control": "no-store"}

    def token(x_den_unlock: Optional[str] = Header(None)) -> Optional[str]:
        return x_den_unlock

    @r.get("/den/secrets/status")
    def secrets_status():
        return status()

    @r.put("/den/secrets/pin")
    def secrets_pin(body: PinIn):
        return set_pin(body)

    @r.put("/den/secrets/settings")
    def secrets_settings(body: SettingsIn):
        return update_settings(body)

    @r.post("/den/secrets/unlock")
    def secrets_unlock(body: UnlockIn):
        return unlock(body.scope, body.pin)

    @r.post("/den/secrets/lock")
    def secrets_lock(t: Optional[str] = Depends(token)):
        with _lock:
            _sessions.pop(t or "", None)
        return {"ok": True}

    # credentials (Projects › Env & credentials)
    @r.get("/den/projects/{project_id}/credentials")
    def creds(project_id: int, t: Optional[str] = Depends(token)):
        require("creds", t)
        return {"items": list_creds(project_id), "platforms": list(PLATFORMS), "envs": list(ENVS)}

    @r.post("/den/projects/{project_id}/credentials")
    def creds_add(project_id: int, body: CredIn, t: Optional[str] = Depends(token)):
        require("creds", t)
        return add_cred(project_id, body)

    @r.patch("/den/credentials/{cid}")
    def creds_edit(cid: int, body: CredPatch, t: Optional[str] = Depends(token)):
        require("creds", t)
        return edit_cred(cid, body)

    @r.get("/den/credentials/{cid}/password")
    def creds_password(cid: int, t: Optional[str] = Depends(token)):
        require("creds", t)
        return Response(content=json.dumps(cred_password(cid)), media_type="application/json", headers=no_store)

    @r.delete("/den/credentials/{cid}")
    def creds_delete(cid: int, t: Optional[str] = Depends(token)):
        require("creds", t)
        return delete_cred(cid)

    # vault
    @r.get("/den/vault")
    def vault(t: Optional[str] = Depends(token)):
        require("vault", t)
        return Response(content=json.dumps(vault_list()), media_type="application/json", headers=no_store)

    @r.post("/den/vault/notes")
    def vault_note(body: NoteIn, t: Optional[str] = Depends(token)):
        require("vault", t)
        return save_note(body)

    @r.put("/den/vault/notes/{iid}")
    def vault_note_edit(iid: int, body: NoteIn, t: Optional[str] = Depends(token)):
        require("vault", t)
        return save_note(body, iid)

    @r.post("/den/vault/files")
    async def vault_upload(request: Request, folder: str, name: str, t: Optional[str] = Depends(token)):
        require("vault", t)
        data = await request.body()
        return add_file(folder, name, request.headers.get("content-type", ""), data)

    @r.get("/den/vault/files/{iid}")
    def vault_download(iid: int, t: Optional[str] = Depends(token)):
        require("vault", t)
        item, data = read_file(iid)
        safe = item["name"].replace('"', "").replace("\n", " ")
        return Response(content=data, media_type="application/octet-stream",
                        headers={**no_store, "Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff"})

    @r.delete("/den/vault/items/{iid}")
    def vault_delete(iid: int, t: Optional[str] = Depends(token)):
        require("vault", t)
        return delete_item(iid)

    return r
