"""
Connected accounts: what the dashboard shows, and the one function tools use
to get a working access token.

Account facts (email, status, granted scopes) live in the `connections`
table; the tokens live in the Keychain (token_store). Nothing returned from
here to the API contains a token.
"""

import threading
import time
from typing import Any, Optional

import db
from assistant import oauth, token_store

# Statuses shown in the UI (see status_of).
CONNECTED, DISCONNECTED, NOT_CONFIGURED = "connected", "disconnected", "not_configured"
REAUTH, UNAVAILABLE, PAID_BLOCKED = "reauth_required", "unavailable", "paid_blocked"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class NotConnected(Exception):
    """No usable account for what was asked. The message says what to do."""


def _lock_for(provider_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(provider_id, threading.Lock())


def _provider(provider_id: str) -> oauth.OAuthProvider:
    p = oauth.PROVIDERS.get(provider_id)
    if not p:
        raise oauth.OAuthError(f"Unknown provider '{provider_id}'")
    return p


def paid_allowed(provider: oauth.OAuthProvider) -> bool:
    return provider.cost != "paid" or bool(db.get_setting(f"assistant_allow_paid_{provider.id}", False))


def services_granted(provider: oauth.OAuthProvider, granted: list[str]) -> list[str]:
    have = set(granted)
    return [s for s, scopes in provider.service_scopes.items() if set(scopes) <= have]


def status_of(provider_id: str) -> dict[str, Any]:
    """Everything the Connections page shows for one provider."""
    p = _provider(provider_id)
    row = db.get_connection(provider_id)
    if not p.implemented:
        status = UNAVAILABLE
    elif row and row["status"] == CONNECTED:
        status = CONNECTED
    elif row and row["status"] == REAUTH:
        status = REAUTH
    elif not p.configured():
        status = NOT_CONFIGURED
    elif not paid_allowed(p):
        status = PAID_BLOCKED
    else:
        status = DISCONNECTED
    granted = row["scopes"] if row else []
    return {
        "id": p.id, "label": p.label, "status": status,
        "account": row["account"] if row else None,
        "connected_at": row["connected_at"] if row else None,
        "last_error": row["last_error"] if row else None,
        "cost": p.cost, "cost_note": p.cost_note,
        "services": list(p.services),
        "services_granted": services_granted(p, granted) if status in (CONNECTED, REAUTH) else [],
        "configured": p.configured(),
        "env": [p.client_id_env, p.client_secret_env],
        "note": p.unavailable_note if not p.implemented else "",
        "setup_steps": list(p.setup_steps),
        "redirect_uri": oauth.redirect_uri(p.id),
    }


def list_status() -> list[dict[str, Any]]:
    return [status_of(pid) for pid in oauth.PROVIDERS]


def begin(provider_id: str, services: Optional[list[str]] = None) -> dict[str, str]:
    p = _provider(provider_id)
    if not paid_allowed(p):
        raise oauth.OAuthError(f"{p.label} needs a paid plan. Nothing is enabled until you switch it on yourself.")
    return {"auth_url": oauth.begin(p, services), "redirect_uri": oauth.redirect_uri(p.id)}


def finish(state: str, code: str, expected_provider: Optional[str] = None) -> dict[str, Any]:
    """Redeem the OAuth callback: store the tokens, record the account."""
    entry = oauth.take_state(state)
    if expected_provider and entry["provider"] != expected_provider:
        raise oauth.OAuthError("That sign-in belongs to a different provider.")
    p = _provider(entry["provider"])
    blob, email = oauth.exchange_code(p, code, entry["verifier"])
    token_store.put(p.id, blob)
    db.save_connection(p.id, email, CONNECTED, blob["scope"].split())
    db.add_activity("connections", "connect", target=f"{p.label}: {email or 'account'}")
    return status_of(p.id)


def access_token(provider_id: str) -> str:
    """A valid access token for the connected account, refreshed if it is
    about to expire. Raises NotConnected / oauth.ReauthRequired."""
    p = _provider(provider_id)
    row = db.get_connection(provider_id)
    if not row or row["status"] == DISCONNECTED:
        raise NotConnected(f"{p.label} isn't connected. Connect it under Personal > Connections.")
    if row["status"] == REAUTH:
        raise oauth.ReauthRequired(f"{p.label} needs to be connected again (Personal > Connections).")
    with _lock_for(provider_id):
        blob = token_store.get(provider_id)
        if not blob:
            db.save_connection(provider_id, None, REAUTH, error="Stored credentials are missing")
            raise oauth.ReauthRequired(f"{p.label}'s stored credentials are missing - connect it again.")
        if blob.get("expires_at", 0) - 60 > time.time():
            return blob["access_token"]
        try:
            fresh = oauth.refresh(p, blob)
        except oauth.ReauthRequired as e:
            db.save_connection(provider_id, None, REAUTH, error=str(e))
            db.add_activity("connections", "refresh", target=p.label, result="error", error=str(e))
            raise
        token_store.put(provider_id, fresh)
        return fresh["access_token"]


def expire_access_token(provider_id: str) -> None:
    """Make the next access_token() call refresh, e.g. after a surprise 401."""
    with _lock_for(provider_id):
        blob = token_store.get(provider_id)
        if blob:
            blob["expires_at"] = 0
            token_store.put(provider_id, blob)


def connected_with(service: str) -> list[str]:
    """Provider ids that are connected and were granted this service
    ('email' / 'calendar'), preferred one first."""
    out = []
    for pid, p in oauth.PROVIDERS.items():
        if not p.implemented:
            continue
        row = db.get_connection(pid)
        if row and row["status"] in (CONNECTED, REAUTH) and service in services_granted(p, row["scopes"]):
            out.append(pid)
    preferred = db.get_setting(f"assistant_default_{service}")
    return sorted(out, key=lambda pid: pid != preferred)


def disconnect(provider_id: str) -> dict[str, Any]:
    """Revoke at the provider (best effort), then forget the tokens locally."""
    p = _provider(provider_id)
    with _lock_for(provider_id):
        try:
            blob = token_store.get(provider_id)
        except token_store.TokenStoreError:
            blob = None
        if blob:
            oauth.revoke(p, blob)
        token_store.remove(provider_id)
        row = db.get_connection(provider_id)
        db.delete_connection(provider_id)
    db.add_activity("connections", "disconnect", target=f"{p.label}: {row['account'] if row else ''}".strip(": "))
    return status_of(provider_id)
