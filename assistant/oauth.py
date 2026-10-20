"""
OAuth 2.0 authorization-code flow with PKCE, for any provider described in
PROVIDERS. This file is only the protocol (build the consent URL, swap a
code for tokens, refresh, revoke); connections.py decides what to store.

You authorize Octopus in your own browser on the provider's page. No
password is ever seen or kept here. The `state` value ties the redirect back
to a request this server made, so the callback needs no API key.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

from assistant import net

STATE_TTL_SECONDS = 600


@dataclass(frozen=True)
class OAuthProvider:
    id: str
    label: str
    auth_url: str
    token_url: str
    revoke_url: Optional[str]
    client_id_env: str
    client_secret_env: str
    # Scopes always requested (identify the account) and per service, so a
    # connection can ask for only the services you turn on.
    base_scopes: tuple
    service_scopes: dict = field(default_factory=dict)
    # 'free' or 'paid'. A paid provider can't be connected until you opt in.
    cost: str = "free"
    cost_note: str = ""
    # False until the tool adapters for this provider exist.
    implemented: bool = True
    unavailable_note: str = ""
    extra_auth_params: dict = field(default_factory=dict)
    setup_steps: tuple = ()

    @property
    def services(self) -> tuple:
        return tuple(self.service_scopes)

    def client_id(self) -> str:
        return os.environ.get(self.client_id_env, "").strip()

    def client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, "").strip()

    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def scopes_for(self, services: Optional[list[str]] = None) -> list[str]:
        wanted = [s for s in (services or self.services) if s in self.service_scopes]
        out = list(self.base_scopes)
        for s in wanted:
            out += [x for x in self.service_scopes[s] if x not in out]
        return out


_G = "https://www.googleapis.com/auth/"
GOOGLE = OAuthProvider(
    id="google", label="Google",
    auth_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    revoke_url="https://oauth2.googleapis.com/revoke",
    client_id_env="GOOGLE_CLIENT_ID", client_secret_env="GOOGLE_CLIENT_SECRET",
    base_scopes=("openid", "email"),
    # Least privilege: read + compose/send (no gmail.modify, so nothing can be
    # deleted), and calendar events + free/busy only.
    service_scopes={
        "email": [_G + "gmail.readonly", _G + "gmail.compose"],
        "calendar": [_G + "calendar.events", _G + "calendar.freebusy"],
        # drive.file: only files the Den creates itself (the backups) - never the rest of your Drive.
        "drive": [_G + "drive.file"],
    },
    cost="free",
    cost_note="Gmail and Calendar APIs are free for personal use.",
    # select_account: always show Google's account chooser, so a second account can be connected.
    extra_auth_params={"access_type": "offline", "prompt": "select_account consent", "include_granted_scopes": "true"},
    setup_steps=(
        "Open console.cloud.google.com and create a project (free - no billing needed).",
        "Enable the Gmail API, the Google Calendar API and the Google Drive API.",
        "Configure the OAuth consent screen: External, add your own Google account as a test user.",
        "Credentials > Create credentials > OAuth client ID > Desktop app.",
        "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to the launchd plist and restart the service.",
    ),
)

MICROSOFT = OAuthProvider(
    id="microsoft", label="Microsoft",
    auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    revoke_url=None,
    client_id_env="MICROSOFT_CLIENT_ID", client_secret_env="MICROSOFT_CLIENT_SECRET",
    base_scopes=("openid", "email", "offline_access", "User.Read"),
    service_scopes={
        "email": ["Mail.Read", "Mail.ReadWrite", "Mail.Send"],
        "calendar": ["Calendars.ReadWrite"],
    },
    cost="free",
    cost_note="Microsoft Graph is free for personal and work accounts; registering an Entra app needs an Azure sign-in.",
    implemented=False,
    unavailable_note="The Outlook adapters aren't built yet - Google ships first.",
    extra_auth_params={"prompt": "select_account"},
)

PROVIDERS: dict[str, OAuthProvider] = {p.id: p for p in (GOOGLE, MICROSOFT)}

_pending: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()


class OAuthError(Exception):
    """A failure the user can act on. Never contains token material."""


class ReauthRequired(OAuthError):
    """The refresh token was revoked or expired - the account must be connected again."""


def redirect_uri(provider_id: str) -> str:
    base = os.environ.get("OCTOPUS_PUBLIC_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '8787')}"
    return f"{base.rstrip('/')}/assistant/oauth/{provider_id}/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def begin(provider: OAuthProvider, services: Optional[list[str]] = None, connection: Optional[str] = None,
          login_hint: Optional[str] = None) -> str:
    """The consent-page URL to open in a browser. connection: the account being
    signed in again (kept with the state for connections.finish); login_hint:
    its address, so the provider suggests it."""
    if not provider.implemented:
        raise OAuthError(provider.unavailable_note or f"{provider.label} isn't available yet")
    if not provider.configured():
        raise OAuthError(f"{provider.label} isn't configured: set {provider.client_id_env} and "
                         f"{provider.client_secret_env} for the service first")
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _pending_lock:
        for k in [k for k, v in _pending.items() if now - v["at"] > STATE_TTL_SECONDS]:
            del _pending[k]
        _pending[state] = {"provider": provider.id, "verifier": verifier, "at": now, "connection": connection,
                           "services": [s for s in (services or provider.services) if s in provider.services]}
    params = {
        "client_id": provider.client_id(), "redirect_uri": redirect_uri(provider.id),
        "response_type": "code", "scope": " ".join(provider.scopes_for(services)),
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        **provider.extra_auth_params,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{provider.auth_url}?{urlencode(params)}"


def take_state(state: str) -> dict[str, Any]:
    """Single-use: a state can be redeemed once, within the TTL."""
    with _pending_lock:
        entry = _pending.pop(state, None)
    if not entry or time.time() - entry["at"] > STATE_TTL_SECONDS:
        raise OAuthError("This sign-in link expired or was already used. Start the connection again.")
    return entry


def _token_request(provider: OAuthProvider, form: dict[str, str]) -> dict[str, Any]:
    form = {**form, "client_id": provider.client_id(), "client_secret": provider.client_secret()}
    try:
        return net.request("POST", provider.token_url, form=form)
    except net.ApiError as e:
        if e.code == "invalid_grant":
            raise ReauthRequired(f"{provider.label} says the sign-in is no longer valid - connect the account again.")
        raise OAuthError(f"{provider.label} token request failed: {e}")


def _blob(provider: OAuthProvider, token: dict[str, Any], previous: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    previous = previous or {}
    return {
        "access_token": token["access_token"],
        # Refreshing usually doesn't return a new refresh token; keep the one we have.
        "refresh_token": token.get("refresh_token") or previous.get("refresh_token"),
        "expires_at": time.time() + int(token.get("expires_in", 3600)),
        "scope": token.get("scope") or previous.get("scope") or "",
    }


def exchange_code(provider: OAuthProvider, code: str, verifier: str) -> tuple[dict[str, Any], Optional[str]]:
    """(token blob to store, the account's email address)."""
    token = _token_request(provider, {
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "redirect_uri": redirect_uri(provider.id)})
    if "access_token" not in token:
        raise OAuthError(f"{provider.label} returned no access token")
    blob = _blob(provider, token)
    if not blob["refresh_token"]:
        raise OAuthError(f"{provider.label} did not grant offline access. Remove Octopus from your "
                         f"{provider.label} account's third-party access list and connect again.")
    return blob, email_from_id_token(token.get("id_token"))


def refresh(provider: OAuthProvider, blob: dict[str, Any]) -> dict[str, Any]:
    if not blob.get("refresh_token"):
        raise ReauthRequired(f"No refresh token stored for {provider.label} - connect the account again.")
    token = _token_request(provider, {"grant_type": "refresh_token", "refresh_token": blob["refresh_token"]})
    if "access_token" not in token:
        raise OAuthError(f"{provider.label} returned no access token on refresh")
    return _blob(provider, token, previous=blob)


def revoke(provider: OAuthProvider, blob: dict[str, Any]) -> None:
    """Best effort - a failure here must not block disconnecting locally."""
    if not provider.revoke_url:
        return
    token = blob.get("refresh_token") or blob.get("access_token")
    if token:
        try:
            net.request("POST", provider.revoke_url, form={"token": token})
        except net.ApiError:
            pass


def email_from_id_token(id_token: Optional[str]) -> Optional[str]:
    """The id_token came straight from the token endpoint over TLS, so its
    payload can be read without checking the signature (OIDC core 3.1.3.7)."""
    if not id_token or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeError):
        return None
    email = claims.get("email") or claims.get("preferred_username")
    return str(email) if email else None
