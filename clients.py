"""
CRM clients (accounts) and their contacts.

A client is who the work is for: name, industry, website, address, notes, a status (prospect /
active / past) and a logo or colour. A contact is a person - usually at a client - with a role,
email, phone and the way they like to be reached. Projects belong to one client (projects.client_id,
picked on the project's Overview tab) and contacts are linked to projects as the key contact (one
per project) or a stakeholder (db.project_contacts).

This module checks a change before it is saved (clean_client / clean_contact - every message is fit
to show the user) and works out what the client pages show on top of the rows: the client's projects
and how much they're worth (value).
"""

import re
from typing import Any, Optional

import crm
import db

STATUSES = ("prospect", "active", "past")
HEALTH = ("healthy", "watch", "at_risk")      # Den 2.1 client form: the owner's read of the relationship
CONTACT_METHODS = ("email", "phone", "text", "whatsapp")
ROLES = db.PROJECT_CONTACT_ROLES
# Contract value: "total" counts every project but lost ones; "active" only the work under way (these stages).
ACTIVE_STAGES = ("active", "on_hold")

MAX_NAME = 120
MAX_SHORT = 120
MAX_ADDRESS = 500
MAX_NOTES = 20_000
MAX_EMAIL = 254
MAX_PHONE = 40

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+()\-.\s/]+$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ClientError(crm.RecordError):
    """A value a client or contact can't hold - the message is shown to the user as is."""


def _text(value: Any, label: str, limit: int, required: bool = False) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise ClientError(f"{label} must be text")
    value = re.sub(r"[ \t]+", " ", value).strip() if value else ""
    if required and not value:
        raise ClientError(f"Give the {label.lower()}")
    if len(value) > limit:
        raise ClientError(f"{label} is too long (at most {limit} characters)")
    return value or None


def _notes(value: Any) -> str:
    if value is not None and not isinstance(value, str):
        raise ClientError("Notes must be text")
    value = (value or "").strip()
    if len(value) > MAX_NOTES:
        raise ClientError(f"Notes are too long (at most {MAX_NOTES} characters)")
    return value


def _choice(value: Any, options: tuple[str, ...], label: str, optional: bool = False) -> Optional[str]:
    if optional and value in (None, ""):
        return None
    if value not in options:
        raise ClientError(f"{label} must be one of: {', '.join(options)}")
    return value


def clean_client(patch: dict[str, Any], client_id: Optional[int] = None) -> dict[str, Any]:
    """The checked, normalised version of a new client (client_id None: name required) or of a
    change to one (only the fields sent). None or "" clears an optional field. Raises ClientError."""
    allowed = ("name", "industry", "website", "address", "notes", "status", "color", "health")
    unknown = set(patch) - set(allowed)
    if unknown:
        raise ClientError(f"Can't set: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k == "name":
            name = _text(v, "Client name", MAX_NAME, required=True)
            other = db.get_client_by_name(name)
            if other and other["id"] != client_id:
                raise ClientError(f"There's already a client called {other['name']}")
            out[k] = name
        elif k == "industry":
            out[k] = _text(v, "Industry", MAX_SHORT)
        elif k == "website":
            try:
                out[k] = crm.web_address(v) if v not in (None, "") else None
            except crm.RecordError:
                raise ClientError(f"Not a web address: {str(v)[:80]}") from None
        elif k == "address":
            if v is not None and not isinstance(v, str):
                raise ClientError("Address must be text")
            v = "\n".join(line.strip() for line in (v or "").strip().splitlines()).strip()
            if len(v) > MAX_ADDRESS:
                raise ClientError(f"Address is too long (at most {MAX_ADDRESS} characters)")
            out[k] = v or None
        elif k == "notes":
            out[k] = _notes(v)
        elif k == "status":
            out[k] = _choice(v, STATUSES, "Status")
        elif k == "health":
            out[k] = None if v in (None, "") else _choice(v, HEALTH, "Health")
        elif k == "color":
            if v in (None, ""):
                out[k] = None
            elif isinstance(v, str) and _COLOR_RE.match(v.strip()):
                out[k] = v.strip().lower()
            else:
                raise ClientError("Colour must look like #e8825a")
    if client_id is None and "name" not in out:
        raise ClientError("Give the client name")
    return out


def clean_contact(patch: dict[str, Any], new: bool = False) -> dict[str, Any]:
    """The checked, normalised version of a new contact (new=True: name required) or of a change to
    one. client_id must be a client that exists (or None for no client). Raises ClientError."""
    unknown = set(patch) - set(db.CONTACT_FIELDS)
    if unknown:
        raise ClientError(f"Can't set: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k == "name":
            out[k] = _text(v, "Contact name", MAX_NAME, required=True)
        elif k == "role":
            out[k] = _text(v, "Role", MAX_SHORT)
        elif k == "email":
            email = _text(v, "Email", MAX_EMAIL)
            if email and not _EMAIL_RE.match(email):
                raise ClientError(f"Not an email address: {email[:80]}")
            out[k] = email
        elif k == "phone":
            phone = _text(v, "Phone", MAX_PHONE)
            if phone and (not _PHONE_RE.match(phone) or sum(ch.isdigit() for ch in phone) < 3):
                raise ClientError("A phone number has digits, and maybe + ( ) - . or spaces")
            out[k] = phone
        elif k == "notes":
            out[k] = _notes(v)
        elif k == "preferred_method":
            out[k] = _choice(v, CONTACT_METHODS, "Preferred contact method", optional=True)
        elif k == "client_id":
            if v in (None, ""):
                out[k] = None
            elif isinstance(v, bool) or not isinstance(v, int) or not db.get_client(v):
                raise ClientError("That client doesn't exist (any more)")
            else:
                out[k] = v
    if new and "name" not in out:
        raise ClientError("Give the contact name")
    return out


# ------------------------------------------------------------------ worked out

def value(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Contract value per currency, biggest first: total (every project but lost ones) and active
    (only the work under way - ACTIVE_STAGES). Projects without a value don't count."""
    sums: dict[str, dict[str, Any]] = {}
    for p in projects:
        v, stage = p.get("contract_value"), crm.stage_of(p)
        if v is None or stage == "lost":
            continue
        row = sums.setdefault(p.get("currency") or "USD", {"currency": p.get("currency") or "USD", "total": 0.0, "active": 0.0})
        row["total"] += v
        if stage in ACTIVE_STAGES:
            row["active"] += v
    out = [{**r, "total": round(r["total"], 2), "active": round(r["active"], 2)} for r in sums.values()]
    return sorted(out, key=lambda r: (-r["total"], r["currency"]))


def _project_row(p: dict[str, Any]) -> dict[str, Any]:
    """A project as the client page lists it."""
    keep = ("id", "name", "status", "category", "active", "hidden", "due_date", "completed_date", "contract_value",
            "currency", "billing_type", "priority", "health", "color", "image", "path")
    return {**{k: p.get(k) for k in keep}, "stage": crm.stage_of(p), "schedule": crm.schedule(p)}


def summary(client: dict[str, Any], projects: list[dict[str, Any]], contact_count: int) -> dict[str, Any]:
    """A client with the figures its list row shows."""
    return {**client, "project_count": len(projects),
            "active_projects": sum(1 for p in projects if crm.stage_of(p) in ACTIVE_STAGES),
            "contact_count": contact_count, "value": value(projects)}


def list_all() -> list[dict[str, Any]]:
    """Every client with its counts and value (one pass over projects and contacts)."""
    by_client: dict[int, list[dict[str, Any]]] = {}
    for p in db.list_projects():
        if p.get("client_id"):
            by_client.setdefault(p["client_id"], []).append(p)
    contacts: dict[int, int] = {}
    for c in db.list_contacts():
        if c.get("client_id"):
            contacts[c["client_id"]] = contacts.get(c["client_id"], 0) + 1
    return [summary(c, by_client.get(c["id"], []), contacts.get(c["id"], 0)) for c in db.list_clients()]


def page(client_id: int) -> Optional[dict[str, Any]]:
    """One client's page: its details, projects (stage, due date, value), contacts and value."""
    client = db.get_client(client_id)
    if not client:
        return None
    projects = db.client_projects(client_id)
    contacts = db.list_contacts(client_id)
    return {**summary(client, projects, len(contacts)), "projects": [_project_row(p) for p in projects], "contacts": contacts}
