"""
The CRM record on a project: what it is, who it's for, how much it's worth, when it's due.

The fields themselves live on the projects table (db.PROJECT_CRM_COLUMNS). This module checks a
change before it is saved (clean), and works out what the Overview tab shows on top of them:
how far along the work is (progress, from the project's phases and to-dos) and where it stands
against its due date (schedule).
"""

import math
import re
from datetime import date
from typing import Any, Optional
from urllib.parse import urlsplit

import db
import phases

PRIORITIES = ("low", "medium", "high", "critical")
HEALTHS = ("on_track", "at_risk", "off_track")
BILLING_TYPES = ("fixed", "hourly", "retainer")
DATE_FIELDS = ("start_date", "due_date", "completed_date")
# The pipeline (the Board view): stage key -> the project status it stands for. A project with no stage
# picked yet gets one from its status (STATUS_STAGE); picking a stage moves the status along with it.
# negotiation: between proposal and won (active) - the Den 2.0 pipeline board has its own column for it.
STAGES = ("lead", "proposal", "negotiation", "active", "on_hold", "done", "lost")
STAGE_STATUS = {"lead": "not_started", "proposal": "not_started", "negotiation": "not_started", "active": "in_progress",
                "on_hold": "on_hold", "done": "completed", "lost": "archived"}
STATUS_STAGE = {"not_started": "lead", "in_progress": "active", "need_info": "active",
                "on_hold": "on_hold", "completed": "done", "archived": "done"}
# The fields a PATCH may set. client_id: the client (clients.py) the project is for, or None.
EDITABLE = ("description", "stage", "priority", "health", *DATE_FIELDS, "contract_value", "currency",
            "billing_type", "owner", "tags", "links", "color", "logo_bg", "client_id")

# Due within this many days (and not done): "due soon".
DUE_SOON_DAYS = 7

MAX_DESCRIPTION = 20_000
MAX_OWNER = 120
MAX_TAG = 40
MAX_TAGS = 30
MAX_LABEL = 60
MAX_URL = 2_000
MAX_LINKS = 20
MAX_VALUE = 1e12

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# "mailto:", "javascript:"... - a scheme with no "//". Not "localhost:3000" (a port follows the colon).
_OTHER_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.I)
_LABELS = {"start_date": "Start date", "due_date": "Due date", "completed_date": "Completed date"}


class RecordError(ValueError):
    """A value the record can't hold - the message is shown to the user as is."""


def _choice(value: Any, options: tuple[str, ...], label: str, optional: bool = False) -> Optional[str]:
    if optional and value in (None, ""):
        return None
    if value not in options:
        raise RecordError(f"{label} must be one of: {', '.join(options)}")
    return value


def _date(value: Any, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise RecordError(f"{_LABELS[field]} must be a date like 2026-10-31")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise RecordError(f"{_LABELS[field]} isn't a real date: {value}") from None


def _text(value: Any, label: str, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RecordError(f"{label} must be text")
    value = value.strip()
    if len(value) > limit:
        raise RecordError(f"{label} is too long (at most {limit} characters)")
    return value or None


def _value(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecordError("Contract value must be a number")
    if not math.isfinite(value) or value < 0:
        raise RecordError("Contract value can't be negative")
    if value > MAX_VALUE:
        raise RecordError("Contract value is too large")
    return round(float(value), 2)


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise RecordError("Tags must be a list of words")
    out, seen = [], set()
    for t in value:
        t = re.sub(r"\s+", " ", t).strip()
        if not t or t.lower() in seen:
            continue
        if len(t) > MAX_TAG:
            raise RecordError(f"Tag '{t[:20]}…' is too long (at most {MAX_TAG} characters)")
        seen.add(t.lower())
        out.append(t)
    if len(out) > MAX_TAGS:
        raise RecordError(f"At most {MAX_TAGS} tags")
    return out


def web_address(url: Any) -> str:
    """A checked http(s) address. "github.com/me/app" (what people type) gets https:// in front."""
    if not isinstance(url, str) or not url.strip():
        raise RecordError("Every link needs a URL")
    url = url.strip()
    if "://" not in url and not _OTHER_SCHEME_RE.match(url):
        url = "https://" + url
    parts = urlsplit(url)
    try:
        ok = parts.scheme in ("http", "https") and bool(parts.hostname) and (parts.port or 0) >= 0
    except ValueError:                       # a port that isn't a number
        ok = False
    if not ok or any(c.isspace() for c in url) or len(url) > MAX_URL:
        raise RecordError(f"Not a web address: {url[:80]}")
    return url


def _links(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise RecordError("Links must be a list of {label, url}")
    if len(value) > MAX_LINKS:
        raise RecordError(f"At most {MAX_LINKS} links")
    out = []
    for x in value:
        url = web_address(x.get("url"))
        parts = urlsplit(url)
        label = x.get("label")
        if label is not None and not isinstance(label, str):
            raise RecordError("A link's label must be text")
        label = (label or "").strip() or parts.netloc
        if len(label) > MAX_LABEL:
            raise RecordError(f"Link label '{label[:20]}…' is too long (at most {MAX_LABEL} characters)")
        out.append({"label": label, "url": url})
    return out


def clean(patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The checked, normalised version of a change to a project's record. `patch` holds only the
    fields being changed (None or "" clears an optional one); `current` is the project as it is,
    so dates are checked against the ones that aren't changing. Raises RecordError."""
    unknown = set(patch) - set(EDITABLE)
    if unknown:
        raise RecordError(f"Can't set: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k == "description":
            if v is not None and not isinstance(v, str):
                raise RecordError("Description must be text")
            v = (v or "").strip()
            if len(v) > MAX_DESCRIPTION:
                raise RecordError(f"Description is too long (at most {MAX_DESCRIPTION} characters)")
            out[k] = v
        elif k == "stage":
            out[k] = _choice(v, STAGES, "Stage")
        elif k == "priority":
            out[k] = _choice(v, PRIORITIES, "Priority")
        elif k == "health":
            out[k] = _choice(v, HEALTHS, "Health")
        elif k == "billing_type":
            out[k] = _choice(v, BILLING_TYPES, "Billing type", optional=True)
        elif k in DATE_FIELDS:
            out[k] = _date(v, k)
        elif k == "contract_value":
            out[k] = _value(v)
        elif k == "currency":
            code = v.strip().upper() if isinstance(v, str) else v
            if not isinstance(code, str) or not _CURRENCY_RE.match(code):
                raise RecordError("Currency must be a 3-letter code like USD, INR or EUR")
            out[k] = code
        elif k == "owner":
            out[k] = _text(v, "Owner", MAX_OWNER)
        elif k in ("color", "logo_bg"):
            if v in (None, ""):
                out[k] = None
            elif isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v.strip()):
                out[k] = v.strip().lower()
            else:
                raise RecordError("Color must look like #e8825a")
        elif k == "tags":
            out[k] = _tags(v)
        elif k == "links":
            out[k] = _links(v)
        elif k == "client_id":
            if v in (None, ""):
                out[k] = None
            elif isinstance(v, bool) or not isinstance(v, int) or not db.get_client(v):
                raise RecordError("That client doesn't exist (any more)")
            else:
                out[k] = v

    merged = {f: out[f] if f in out else current.get(f) for f in DATE_FIELDS}
    start, due, done = merged["start_date"], merged["due_date"], merged["completed_date"]
    if start and due and due < start:
        raise RecordError("Due date can't be before the start date")
    if start and done and done < start:
        raise RecordError("Completed date can't be before the start date")
    return out


# ------------------------------------------------------------------ pipeline

def stage_of(project: dict[str, Any]) -> str:
    """The pipeline stage: the one picked, else the one the project's status stands for."""
    stage = project.get("stage")
    return stage if stage in STAGES else STATUS_STAGE.get(project.get("status"), "lead")


def sync_stage_status(current: dict[str, Any], new_status: Optional[str], new_stage: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Keep stage and status telling the same story. Returns (status to save, stage to save), None
    meaning leave it. Picking a stage moves the status to match; changing the status moves the
    stage only if the stage no longer fits (Lead and Proposal both fit "not started", Active fits
    "need info" too)."""
    if new_stage:
        fits = lambda s: STAGE_STATUS[new_stage] == s or (new_stage == "active" and s == "need_info")
        if new_status is not None or fits(current.get("status")):
            return None, None
        return STAGE_STATUS[new_stage], None
    if new_status and current.get("stage") in STAGES:      # no stage picked yet: stage_of follows the status by itself
        stage = current["stage"]
        if STAGE_STATUS[stage] == new_status or (stage == "active" and new_status == "need_info"):
            return None, None
        return None, STATUS_STAGE[new_status]
    return None, None


# ------------------------------------------------------------------ worked out

def schedule(project: dict[str, Any], today: Optional[date] = None) -> dict[str, Any]:
    """Where the project stands against its due date.
    due_state: None (no due date) | done (completed, archived or has a completed date) |
    overdue | due_soon (within DUE_SOON_DAYS) | upcoming. days_left is negative when overdue."""
    today = today or phases._today()
    due = project.get("due_date")
    if not due:
        return {"days_left": None, "due_state": None}
    try:
        days_left = (date.fromisoformat(due) - today).days
    except ValueError:
        return {"days_left": None, "due_state": None}
    if project.get("completed_date") or project.get("status") in ("completed", "archived"):
        state = "done"
    elif days_left < 0:
        state = "overdue"
    elif days_left <= DUE_SOON_DAYS:
        state = "due_soon"
    else:
        state = "upcoming"
    return {"days_left": days_left, "due_state": state}


def progress(project_id: int) -> dict[str, Any]:
    """How far along the work is, from the project's phases and to-dos. Each to-do is one unit of
    work and is done once its task is; a phase with no to-dos is one unit of its own; a phase
    marked done counts all its to-dos as done. Cancelled to-dos don't count. percent is None when
    there's nothing to measure yet."""
    todos = [t for t in db.list_todos(project_id) if phases.todo_state(t) != "cancelled"]
    plan = db.list_phases(project_id)
    by_phase: dict[Optional[int], list[dict[str, Any]]] = {}
    for t in todos:
        by_phase.setdefault(t.get("phase_id"), []).append(t)
    phase_ids = {p["id"] for p in plan}
    units = done = 0
    for p in plan:
        items = by_phase.get(p["id"], [])
        if p["status"] == "done":
            n = max(1, len(items))
            units, done = units + n, done + n
        elif items:
            units += len(items)
            done += sum(1 for t in items if phases.todo_state(t) == "done")
        else:
            units += 1
    loose = [t for t in todos if t.get("phase_id") not in phase_ids]   # not in any (existing) phase
    units += len(loose)
    done += sum(1 for t in loose if phases.todo_state(t) == "done")
    return {
        "percent": round(100 * done / units) if units else None,
        "todos_done": sum(1 for t in todos if phases.todo_state(t) == "done"),
        "todos_total": len(todos),
        "phases_done": sum(1 for p in plan if p["status"] == "done"),
        "phases_total": len(plan),
    }


def client_brief(client: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """What a project shows of its client: enough for a chip that links to the client's page."""
    if not client:
        return None
    return {k: client.get(k) for k in ("id", "name", "status", "color", "logo")}


def record(project: dict[str, Any], today: Optional[date] = None) -> dict[str, Any]:
    """The project with everything the Overview tab works out added: "progress" and "schedule", its
    client (client_brief, None without one) and the contacts linked to it (the key contact first)."""
    client = db.get_client(project["client_id"]) if project.get("client_id") else None
    return {**project, "stage": stage_of(project), "progress": progress(project["id"]), "schedule": schedule(project, today),
            "client": client_brief(client), "client_name": client["name"] if client else None,
            "contacts": db.project_contacts(project["id"])}
