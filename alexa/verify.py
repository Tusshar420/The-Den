"""
Is this request really from Amazon, for our skill, and fresh?

/alexa is the one path of The Den that is reachable from the internet (through
Tailscale Funnel), so it can't lean on X-API-Key. Instead every request is
checked the way Amazon requires of a self-hosted skill:

    SignatureCertChainUrl   https://s3.amazonaws.com/echo.api/...  (nowhere else)
    the certificate         valid now, for echo-api.amazon.com, chaining to a trusted root
    Signature-256           RSA / SHA-256 signature of the exact body bytes
    request.timestamp       within 150 seconds of now (no replays)
    applicationId           our skill (ALEXA_SKILL_ID)

Anything that fails raises VerifyError; the endpoint answers 400.
"""

import base64
import posixpath
import threading
import time
import urllib.request
import warnings
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.verification import PolicyBuilder, Store

CERT_HOST = "s3.amazonaws.com"
CERT_PATH_PREFIX = "/echo.api/"
CERT_SAN = "echo-api.amazon.com"
MAX_SKEW_SECONDS = 150
ROOTS_FILE = "/etc/ssl/cert.pem"
CERT_CACHE_SECONDS = 6 * 3600

_cache: dict[str, tuple[float, list[x509.Certificate]]] = {}
_cache_lock = threading.Lock()
_roots: Optional[Store] = None


class VerifyError(Exception):
    pass


def valid_cert_url(url: str) -> bool:
    """Amazon's rules for SignatureCertChainUrl, after removing dot segments and duplicate slashes."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return False
    if u.scheme.lower() != "https" or (u.hostname or "").lower() != CERT_HOST or port not in (None, 443):
        return False
    path = posixpath.normpath(u.path.replace("//", "/")) if u.path else ""
    if u.path.endswith("/") and not path.endswith("/"):
        path += "/"
    return path.startswith(CERT_PATH_PREFIX)


def _trusted_roots() -> Store:
    global _roots
    if _roots is None:
        with open(ROOTS_FILE, "rb") as f, warnings.catch_warnings():
            warnings.simplefilter("ignore")      # one of macOS's roots has a negative serial number
            _roots = Store(x509.load_pem_x509_certificates(f.read()))
    return _roots


def _fetch_chain(url: str) -> list[x509.Certificate]:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < CERT_CACHE_SECONDS:
            return hit[1]
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            pem = r.read(256 * 1024)
        chain = x509.load_pem_x509_certificates(pem)
    except Exception as e:
        raise VerifyError(f"could not load the signing certificate: {e}")
    _check_chain(chain)
    with _cache_lock:
        _cache[url] = (now, chain)
    return chain


def _check_chain(chain: list[x509.Certificate]) -> None:
    """Valid dates, echo-api.amazon.com in the SANs, and a chain to a trusted root CA."""
    if not chain:
        raise VerifyError("empty certificate chain")
    try:
        verifier = PolicyBuilder().store(_trusted_roots()).time(datetime.now(timezone.utc).replace(tzinfo=None)) \
            .build_server_verifier(x509.DNSName(CERT_SAN))
        verifier.verify(chain[0], chain[1:])
    except Exception as e:
        raise VerifyError(f"signing certificate is not valid for {CERT_SAN}: {e}")


def check_signature(body: bytes, cert_url: str, signature_b64: str) -> None:
    if not cert_url or not signature_b64:
        raise VerifyError("missing signature headers")
    if not valid_cert_url(cert_url):
        raise VerifyError("certificate URL is not Amazon's")
    leaf = _fetch_chain(cert_url)[0]
    # A cached chain can outlive its leaf - re-check the dates on every request.
    now = datetime.now(timezone.utc)
    if not (leaf.not_valid_before_utc <= now <= leaf.not_valid_after_utc):
        with _cache_lock:
            _cache.pop(cert_url, None)
        raise VerifyError("signing certificate has expired")
    try:
        leaf.public_key().verify(base64.b64decode(signature_b64), body, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError) as e:
        raise VerifyError(f"signature does not match the request: {e or 'invalid'}")


def check_timestamp(envelope: dict[str, Any], now: Optional[datetime] = None) -> None:
    raw = str((envelope.get("request") or {}).get("timestamp") or "")
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise VerifyError("request has no valid timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if abs(((now or datetime.now(timezone.utc)) - ts).total_seconds()) > MAX_SKEW_SECONDS:
        raise VerifyError("request is too old (or the clock is off)")


def application_id(envelope: dict[str, Any]) -> str:
    ctx = (envelope.get("context") or {}).get("System") or {}
    return str(((ctx.get("application") or {}).get("applicationId"))
               or (((envelope.get("session") or {}).get("application") or {}).get("applicationId")) or "")


def check_application(envelope: dict[str, Any], skill_id: str) -> None:
    if not skill_id or application_id(envelope) != skill_id:
        raise VerifyError("request is for a different skill")
