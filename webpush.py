"""
Web Push to the dashboard installed as a web app - Safari's "Add to Dock" on
the Mac, or "Add to Home Screen" on the iPhone. Sent alongside the
ActivitySmith pushes, never instead of them.

The browser subscribes through static/sw.js (POST /push/subscribe); each push
is encrypted for that one subscription (RFC 8291, aes128gcm) and signed with
this server's VAPID key (RFC 8292). The key pair is made on first use and
kept in the settings table, so every process (API, worker, approval_server)
signs with the same one.

Web Push needs a secure origin: the dashboard has to be opened over https -
Tailscale Serve's https://<mac>.<tailnet>.ts.net - not http://100.x:8787.

Only stdlib + `cryptography`, which both venvs (.venv and .approval-venv) have.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import approval_links
import db

log = logging.getLogger("the-den.webpush")

KEY_SETTING = "webpush_vapid_private_pem"
ORIGIN_SETTING = "webpush_origin"     # the https origin the dashboard last subscribed from
RECORD_SIZE = 4096
MAX_BODY = 600                        # pushes are capped near 4 KB once encrypted; keep well clear
URGENT = ("approval", "failure", "reminder")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ------------------------------------------------------------------ VAPID

def vapid_key() -> ec.EllipticCurvePrivateKey:
    """This server's signing key, made (and stored) the first time it's needed."""
    pem = db.get_setting(KEY_SETTING)
    if not pem:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        db.set_setting(KEY_SETTING, pem)
    return serialization.load_pem_private_key(pem.encode(), password=None)


def public_key() -> str:
    """The applicationServerKey the browser subscribes with."""
    return b64url(_raw_public(vapid_key().public_key()))


def subject() -> str:
    """Who the push service contacts about abuse. Apple insists on a valid
    https: or mailto: URL; the dashboard's own https origin is the default."""
    sub = os.environ.get("VAPID_SUBJECT") or db.get_setting(ORIGIN_SETTING) or ""
    return sub if sub.startswith(("https://", "mailto:")) else "mailto:the-den@localhost.localdomain"


def vapid_header(endpoint: str, key: ec.EllipticCurvePrivateKey, now: Optional[float] = None) -> str:
    u = urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": subject()}
    signing_input = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." \
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing_input + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={b64url(_raw_public(key.public_key()))}"


# ------------------------------------------------------------------ RFC 8291 encryption

def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(p256dh: str, auth: str, plaintext: bytes, salt: Optional[bytes] = None,
            server_key: Optional[ec.EllipticCurvePrivateKey] = None) -> bytes:
    """One aes128gcm record for this subscription. salt / server_key are only
    passed in by the tests (RFC 8291's worked example); normally both are fresh."""
    ua_public = unb64url(p256dh)
    auth_secret = unb64url(auth)
    salt = salt or os.urandom(16)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(server_key.public_key())
    ecdh = server_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    # HKDF with a single 32-byte block of output each time: expand = HMAC(prk, info || 0x01)
    ikm = _hmac(_hmac(auth_secret, ecdh), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    body = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)   # 0x02: this is the last (only) record
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + body


# ------------------------------------------------------------------ sending

def payload(title: str, body: str, kind: str = "info", url: str = "/dashboard",
            approval_id: Optional[int] = None) -> dict[str, Any]:
    """What static/sw.js shows. Approve / Deny links are relative: the
    service worker fetches them from whatever origin it's running on."""
    out: dict[str, Any] = {"title": title, "body": (body or "")[:MAX_BODY], "kind": kind, "url": url or "/dashboard",
                           "tag": f"approval-{approval_id}" if approval_id is not None else None}
    if approval_id is not None:
        out["approvalId"] = approval_id
        if os.environ.get("API_KEY"):
            out["approveUrl"] = approval_links.decide_path(approval_id, "approve")
            out["denyUrl"] = approval_links.decide_path(approval_id, "deny")
    try:
        out["pending"] = len(db.list_pending_approvals())   # the Dock icon's badge
    except Exception:
        pass
    return out


def send_one(sub: dict[str, Any], data: dict[str, Any], key: ec.EllipticCurvePrivateKey) -> bool:
    """True if the push service took it. A subscription it says is gone
    (404 / 410: unsubscribed, app removed from the Dock) is deleted."""
    urgent = data.get("kind") in URGENT
    req = urllib.request.Request(
        sub["endpoint"],
        data=encrypt(sub["p256dh"], sub["auth"], json.dumps(data).encode()),
        headers={"Authorization": vapid_header(sub["endpoint"], key), "Content-Encoding": "aes128gcm",
                 "Content-Type": "application/octet-stream", "TTL": "3600" if urgent else "86400",
                 "Urgency": "high" if urgent else "normal", "User-Agent": "the-den/1.0"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            db.delete_push_subscription(sub["endpoint"])
        else:
            log.warning("Web push to %s failed: %s %s", urlsplit(sub["endpoint"]).netloc, e.code, e.read()[:200])
    except Exception as e:
        log.warning("Web push to %s failed: %s", urlsplit(sub["endpoint"]).netloc, e)
    return False


def send_all(title: str, body: str, kind: str = "info", url: str = "/dashboard",
             approval_id: Optional[int] = None) -> int:
    """Push to every subscribed browser; returns how many took it. Best-effort:
    never raises, so a push problem can't break whatever triggered it."""
    try:
        subs = db.list_push_subscriptions()
        if not subs:
            return 0
        key, data = vapid_key(), payload(title, body, kind, url, approval_id)
        return sum(send_one(s, data, key) for s in subs)
    except Exception as e:
        log.warning("Web push failed: %s", e)
        return 0
