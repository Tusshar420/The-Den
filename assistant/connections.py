"""
Connected accounts: what the dashboard shows, and the one function tools use
to get a working access token.

A provider can have several accounts connected (a personal and a work
Google account). Each is one row of the `connections` table, by its own id:
the provider's id for the first one ("google"), "<provider>-<hex>" for the
others. Account facts (email, status, granted scopes) live in that table; the
tokens live in the Keychain (token_store), under the same id. Nothing
returned from here to the API contains a token.

Per service (email / calendar) one account is the default - the setting
`assistant_default_<service>` holds its id; with none chosen it's the first
connected. Tools use the default unless they're told which account to use
(resolve()).
"""

import secrets
import threading
import time
from typing import Any, Optional

import db
from assistant import oauth, token_store

# Statuses shown in the UI (see status_of).
CONNECTED, DISCONNECTED, NOT_CONFIGURED = "connected", "disconnected", "not_configured"
REAUTH, UNAVAILABLE, PAID_BLOCKED = "reauth_required", "unavailable", "paid_blocked"

SERVICES = ("email", "calendar", "drive")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class NotConnected(Exception):
    """No usable account for what was asked. The message says what to do."""


def _lock_for(conn_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(conn_id, threading.Lock())


def _provider(provider_id: str) -> oauth.OAuthProvider:
    p = oauth.PROVIDERS.get(provider_id)
    if not p:
        raise oauth.OAuthError(f"Unknown provider '{provider_id}'")
    return p


def provider_of(conn_id: str) -> str:
    """The provider an account belongs to ('google-1a2b3c4d' -> 'google')."""
    row = db.get_connection(conn_id)
    return row["provider"] if row else conn_id.split("-")[0]


def _name(p: oauth.OAuthProvider, row: Optional[dict[str, Any]]) -> str:
    """'Google (me@gmail.com)' - which account, for messages."""
    return f"{p.label} ({row['account']})" if row and row.get("account") else p.label


def label(conn_id: str) -> str:
    row = db.get_connection(conn_id)
    return (row or {}).get("account") or conn_id


def paid_allowed(provider: oauth.OAuthProvider) -> bool:
    return provider.cost != "paid" or bool(db.get_setting(f"assistant_allow_paid_{provider.id}", False))


def services_granted(provider: oauth.OAuthProvider, granted: list[str]) -> list[str]:
    have = set(granted)
    return [s for s, scopes in provider.service_scopes.items() if set(scopes) <= have]


def _account_view(p: oauth.OAuthProvider, row: dict[str, Any]) -> dict[str, Any]:
    granted = services_granted(p, row["scopes"])
    return {"id": row["id"], "account": row["account"], "status": row["status"], "connected_at": row["connected_at"],
            "last_error": row["last_error"], "services_granted": granted,
            # The services this account is the default for (only says something when there's a choice).
            "default_for": [s for s in granted if default_for(s) == row["id"]]}


def status_of(provider_id: str) -> dict[str, Any]:
    """Everything the Connections page shows for one provider: the provider
    itself and each of its connected accounts (`accounts`). The top-level
    account / status / services describe them all together."""
    p = _provider(provider_id)
    accounts = [_account_view(p, r) for r in db.list_connections(provider_id) if r["status"] != DISCONNECTED]
    if not p.implemented:
        status = UNAVAILABLE
    elif any(a["status"] == CONNECTED for a in accounts):
        status = CONNECTED
    elif any(a["status"] == REAUTH for a in accounts):
        status = REAUTH
    elif not p.configured():
        status = NOT_CONFIGURED
    elif not paid_allowed(p):
        status = PAID_BLOCKED
    else:
        status = DISCONNECTED
    first = next((a for a in accounts if a["status"] == CONNECTED), accounts[0] if accounts else None)
    granted = {s for a in accounts for s in a["services_granted"]}
    return {
        "id": p.id, "label": p.label, "status": status,
        "account": first["account"] if first else None,
        "connected_at": first["connected_at"] if first else None,
        "last_error": next((a["last_error"] for a in accounts if a["status"] == REAUTH and a["last_error"]), None),
        "accounts": accounts,
        # Another account can be connected (the provider is set up and allowed).
        "can_add": p.implemented and p.configured() and paid_allowed(p),
        "cost": p.cost, "cost_note": p.cost_note,
        "services": list(p.services),
        "services_granted": [s for s in p.services if s in granted] if status in (CONNECTED, REAUTH) else [],
        "configured": p.configured(),
        "env": [p.client_id_env, p.client_secret_env],
        "note": p.unavailable_note if not p.implemented else "",
        "setup_steps": list(p.setup_steps),
        "redirect_uri": oauth.redirect_uri(p.id),
    }


def list_status() -> list[dict[str, Any]]:
    return [status_of(pid) for pid in oauth.PROVIDERS]


def begin(provider_id: str, services: Optional[list[str]] = None, reconnect: Optional[str] = None) -> dict[str, str]:
    """Start a sign-in. reconnect: the id of an account that needs signing in
    again - the provider's page then suggests that account."""
    p = _provider(provider_id)
    if not paid_allowed(p):
        raise oauth.OAuthError(f"{p.label} needs a paid plan. Nothing is enabled until you switch it on yourself.")
    row = db.get_connection(reconnect) if reconnect else None
    if row and row["provider"] != p.id:
        raise oauth.OAuthError("That account belongs to a different provider.")
    url = oauth.begin(p, services, connection=row["id"] if row else None,
                      login_hint=row["account"] if row else None)
    return {"auth_url": url, "redirect_uri": oauth.redirect_uri(p.id)}


def _slot(p: oauth.OAuthProvider, email: Optional[str], reconnect: Optional[str]) -> str:
    """Which account id a finished sign-in is stored under: the account already
    connected with this address, the one being reconnected, or a new one."""
    rows = db.list_connections(p.id)
    if email:
        same = next((r for r in rows if (r["account"] or "").lower() == email.lower()), None)
        if same:
            return same["id"]
    if reconnect and any(r["id"] == reconnect and not r["account"] for r in rows):
        return reconnect
    if not any(r["id"] == p.id for r in rows):
        return p.id
    return f"{p.id}-{secrets.token_hex(4)}"


def finish(state: str, code: str, expected_provider: Optional[str] = None) -> dict[str, Any]:
    """Redeem the OAuth callback: store the tokens, record the account. Signing
    in with an account that's already connected refreshes it; any other
    account is added next to the ones there."""
    entry = oauth.take_state(state)
    if expected_provider and entry["provider"] != expected_provider:
        raise oauth.OAuthError("That sign-in belongs to a different provider.")
    p = _provider(entry["provider"])
    blob, email = oauth.exchange_code(p, code, entry["verifier"])
    conn_id = _slot(p, email, entry.get("connection"))
    with _lock_for(conn_id):
        token_store.put(conn_id, blob)
        db.save_connection(conn_id, email, CONNECTED, blob["scope"].split(), provider=p.id)
    db.add_activity("connections", "connect", target=f"{p.label}: {email or 'account'}")
    return status_of(p.id)


def access_token(conn_id: str) -> str:
    """A valid access token for this connected account, refreshed if it is
    about to expire. Raises NotConnected / oauth.ReauthRequired."""
    row = db.get_connection(conn_id)
    p = oauth.PROVIDERS.get(row["provider"] if row else conn_id.split("-")[0])
    if not p:
        raise NotConnected("That account isn't connected. Connect it under Personal > Connections.")
    if not row or row["status"] == DISCONNECTED:
        raise NotConnected(f"{p.label} isn't connected. Connect it under Personal > Connections.")
    name = _name(p, row)
    if row["status"] == REAUTH:
        raise oauth.ReauthRequired(f"{name} needs to be connected again (Personal > Connections).")
    with _lock_for(conn_id):
        blob = token_store.get(conn_id)
        if not blob:
            db.save_connection(conn_id, None, REAUTH, error="Stored credentials are missing")
            raise oauth.ReauthRequired(f"{name}'s stored credentials are missing - connect it again.")
        if blob.get("expires_at", 0) - 60 > time.time():
            return blob["access_token"]
        try:
            fresh = oauth.refresh(p, blob)
        except oauth.ReauthRequired as e:
            db.save_connection(conn_id, None, REAUTH, error=str(e))
            db.add_activity("connections", "refresh", target=name, result="error", error=str(e))
            raise
        token_store.put(conn_id, fresh)
        return fresh["access_token"]


def expire_access_token(conn_id: str) -> None:
    """Make the next access_token() call refresh, e.g. after a surprise 401."""
    with _lock_for(conn_id):
        blob = token_store.get(conn_id)
        if blob:
            blob["expires_at"] = 0
            token_store.put(conn_id, blob)


def connected_with(service: str) -> list[str]:
    """Ids of the accounts that are connected and were granted this service
    ('email' / 'calendar'), the default one first."""
    out = []
    for row in db.list_connections():
        p = oauth.PROVIDERS.get(row["provider"])
        if not p or not p.implemented:
            continue
        if row["status"] in (CONNECTED, REAUTH) and service in services_granted(p, row["scopes"]):
            out.append(row["id"])
    preferred = db.get_setting(f"assistant_default_{service}")
    return sorted(out, key=lambda cid: cid != preferred)


def default_for(service: str) -> Optional[str]:
    ids = connected_with(service)
    return ids[0] if ids else None


def set_default(service: str, conn_id: str) -> None:
    """Make this account the one used for a service when nobody says which."""
    if service not in SERVICES:
        raise ValueError(f"Unknown service '{service}'")
    if conn_id not in connected_with(service):
        raise ValueError(f"That account isn't connected for {service}")
    db.set_setting(f"assistant_default_{service}", conn_id)


def resolve(service: str, hint: str) -> str:
    """The id of the connected account a person meant by `hint` - an account
    id, its address, or a recognisable part of it ("work", "lawaccounting").
    Raises NotConnected saying which accounts there are when nothing (or more
    than one) fits."""
    ids = connected_with(service)
    if not ids:
        raise NotConnected(f"There's no connected account for {service} - connect one under Personal > Connections.")
    h = (hint or "").strip().lower()
    if not h or h in ("default", "primary", "main"):
        return ids[0]
    addr = {cid: label(cid).lower() for cid in ids}
    exact = [cid for cid in ids if h in (cid.lower(), addr[cid])]
    if exact:
        return exact[0]
    partial = [cid for cid in ids if h in addr[cid]]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(label(cid) for cid in (partial or ids))
    if partial:
        raise NotConnected(f'More than one connected account matches "{hint}" ({names}) - say which one.')
    raise NotConnected(f'No connected {service} account matches "{hint}". Connected: {names}.')


def disconnect(conn_id: str) -> dict[str, Any]:
    """Revoke at the provider (best effort), then forget the tokens locally."""
    row = db.get_connection(conn_id)
    p = _provider(row["provider"] if row else conn_id.split("-")[0])
    with _lock_for(conn_id):
        try:
            blob = token_store.get(conn_id)
        except token_store.TokenStoreError:
            blob = None
        if blob:
            oauth.revoke(p, blob)
        token_store.remove(conn_id)
        db.delete_connection(conn_id)
    for service in SERVICES:
        if db.get_setting(f"assistant_default_{service}") == conn_id:
            db.set_setting(f"assistant_default_{service}", None)
    db.add_activity("connections", "disconnect", target=f"{p.label}: {row['account'] if row else ''}".strip(": "))
    return status_of(p.id)
