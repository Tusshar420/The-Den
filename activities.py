"""
CRM activities and follow-ups: what happened with a project, client or contact, and what to do next.

An activity is something that happened - a call, meeting, email, note or decision - with when it
happened, a summary, who was involved (contacts) and an optional outcome. The Den logs one more kind
itself: 'stage', each time a project moves along the pipeline (record_stage_change, and
db._auto_in_progress when a project's first task starts).

A follow-up is a to-do with a due date (and maybe a time) - "send invoice", "chase feedback" - marked
done when finished. An open one past its due time is overdue. Its reminder goes out as a phone push
at the due time (DEFAULT_TIME on the day when it has none), held through quiet hours - see
pending_reminders and worker.check_followup_reminders.

Every activity and follow-up is logged on exactly one subject: a project, a client or a contact.

feed() builds the activity timeline a project / client / contact page shows, newest first: logged
activities, follow-ups done, stage changes and (for a project) its finished tasks, plus the open
follow-ups pinned on top. last_activity() is the "Last activity" of the Projects table.

This module checks every value before it is saved (clean_activity / clean_followup - every message is
fit to show the user); db.py only stores rows.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import crm
import db
from assistant import timeutil

KINDS = ("call", "meeting", "email", "note", "decision")
KIND_LABELS = {"call": "Call", "meeting": "Meeting", "email": "Email", "note": "Note", "decision": "Decision",
               "stage": "Stage change"}
STAGE_LABELS = {"lead": "Lead", "proposal": "Proposal", "negotiation": "Negotiation", "active": "Active", "on_hold": "On hold", "done": "Done", "lost": "Lost"}
FOLLOWUP_STATUSES = ("open", "done")
SUBJECTS = ("project_id", "client_id", "contact_id")

# A follow-up with no time is reminded about at this local time on its day.
DEFAULT_TIME = time(9, 0)
# Follow-ups done this long ago still show on the calendar (struck through).
DONE_SHOWN_DAYS = 30
MAX_SUMMARY = 4000
MAX_OUTCOME = 2000
MAX_TITLE = 200
MAX_NOTES = 4000
MAX_PEOPLE = 30
FEED_LIMIT = 150

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ActivityError(crm.RecordError):
    """A value an activity or follow-up can't hold - the message is shown to the user as is."""


class NotFound(ActivityError):
    """The project, client, contact, activity or follow-up isn't there (any more)."""


# ------------------------------------------------------------------ basics

def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now(now: Optional[datetime] = None) -> datetime:
    return (now or timeutil.now()).astimezone(timeutil.local_tz())


def _when(value: Any) -> str:
    """When something happened: an ISO timestamp (no offset = local time) as UTC ISO."""
    if not isinstance(value, str) or not value.strip():
        raise ActivityError("Say when it happened")
    try:
        dt = timeutil.parse_iso(value.strip())
    except ValueError:
        raise ActivityError("When it happened must be a date and time like 2026-10-03T14:30") from None
    if dt > timeutil.now() + timedelta(days=366):
        raise ActivityError("That's more than a year from now")
    return _utc(dt)


def _text(value: Any, label: str, limit: int, required: bool = False) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise ActivityError(f"{label} must be text")
    value = (value or "").strip()
    if required and not value:
        raise ActivityError(f"Give the {label.lower()}")
    if len(value) > limit:
        raise ActivityError(f"{label} is too long (at most {limit} characters)")
    return value or None


def _id(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _subject(patch: dict[str, Any], current: Optional[dict[str, Any]] = None) -> dict[str, Optional[int]]:
    """Exactly one of project_id / client_id / contact_id, and it must exist. Sending one subject
    replaces the current one."""
    sent = {k: patch[k] for k in SUBJECTS if k in patch and patch[k] not in (None, "")}
    if not sent:
        if current and any(current.get(k) for k in SUBJECTS):
            return {}
        raise ActivityError("Log it on a project, a client or a contact")
    if len(sent) > 1:
        raise ActivityError("Log it on one project, client or contact - not several")
    (k, v), = sent.items()
    v = _id(v)
    if k == "project_id":
        p = db.get_project(v) if v else None
        if not p or db.is_general_project(p):
            raise NotFound("That project doesn't exist (any more)")
    elif not v or not (db.get_client(v) if k == "client_id" else db.get_contact(v)):
        raise NotFound(f"That {'client' if k == 'client_id' else 'contact'} doesn't exist (any more)")
    return {s: (v if s == k else None) for s in SUBJECTS}


def _people(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(_id(x) for x in value):
        raise ActivityError("Who was involved must be a list of contacts")
    ids = list(dict.fromkeys(value))
    if len(ids) > MAX_PEOPLE:
        raise ActivityError(f"At most {MAX_PEOPLE} people")
    for cid in ids:
        if not db.get_contact(cid):
            raise NotFound("One of those contacts doesn't exist (any more)")
    return ids


# ------------------------------------------------------------------ activities

def clean_activity(patch: dict[str, Any], current: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], Optional[list[int]]]:
    """(fields, contact_ids) - the checked version of a new activity (current None: kind, when and
    summary required) or of a change to one (only the fields sent; contact_ids None = unchanged).
    Raises ActivityError."""
    allowed = ("kind", "occurred_at", "summary", "outcome", "contact_ids", *SUBJECTS)
    unknown = set(patch) - set(allowed)
    if unknown:
        raise ActivityError(f"Can't set: {', '.join(sorted(unknown))}")
    if current and current["kind"] == "stage":
        raise ActivityError("A stage change is logged by the Den - it can be deleted, not edited")
    out: dict[str, Any] = {}
    if "kind" in patch or current is None:
        if patch.get("kind") not in KINDS:
            raise ActivityError(f"Pick what it was: {', '.join(KINDS)}")
        out["kind"] = patch["kind"]
    if "occurred_at" in patch or current is None:
        out["occurred_at"] = _when(patch.get("occurred_at"))
    if "summary" in patch or current is None:
        out["summary"] = _text(patch.get("summary"), "Summary", MAX_SUMMARY, required=True)
    if "outcome" in patch:
        out["outcome"] = _text(patch["outcome"], "Outcome", MAX_OUTCOME)
    if any(k in patch for k in SUBJECTS) or current is None:
        out.update(_subject(patch, current))
    people = _people(patch["contact_ids"]) if "contact_ids" in patch else (None if current else [])
    return out, people


def create_activity(patch: dict[str, Any]) -> dict[str, Any]:
    fields, people = clean_activity(patch)
    return activity_view(db.add_crm_activity(fields, people))


def update_activity(activity_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    current = db.get_crm_activity(activity_id)
    if not current:
        raise NotFound("That activity is gone")
    fields, people = clean_activity(patch, current)
    db.update_crm_activity(activity_id, fields, people)
    return activity_view(db.get_crm_activity(activity_id))


def stage_summary(old: str, new: str) -> str:
    return f"{STAGE_LABELS.get(old, old)} → {STAGE_LABELS.get(new, new)}"


def record_stage_change(project_id: int, old: str, new: str, by: str = "user") -> Optional[dict[str, Any]]:
    """Put a pipeline move on the project's timeline (nothing when the stage didn't change)."""
    if old == new:
        return None
    return db.add_crm_activity({"kind": "stage", "occurred_at": _utc(datetime.now(timezone.utc)), "summary": stage_summary(old, new),
                            "project_id": project_id, "data": {"from": old, "to": new, "by": by}})


# ------------------------------------------------------------------ follow-ups

def _due_date(value: Any) -> str:
    v = value.strip() if isinstance(value, str) else ""
    if not _DATE_RE.match(v):
        raise ActivityError("Due date must be a date like 2026-10-31")
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise ActivityError(f"Due date isn't a real date: {v}") from None


def _due_time(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
        raise ActivityError("Due time must be like 14:30 (24-hour)")
    return value.strip()


def due_at(f: dict[str, Any]) -> datetime:
    """When a follow-up is due (local): its time, else DEFAULT_TIME on its day - when its reminder goes."""
    t = time(*(int(x) for x in f["due_time"].split(":"))) if f.get("due_time") else DEFAULT_TIME
    return timeutil.at(date.fromisoformat(f["due_date"]), t)


def is_overdue(f: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Open and past due: past its time when it has one, else from the day after its date."""
    if f["status"] != "open":
        return False
    now = _now(now)
    if f.get("due_time"):
        return now > due_at(f)
    return now.date() > date.fromisoformat(f["due_date"])


def _reminded(f: dict[str, Any], now: datetime) -> Optional[str]:
    """reminded_at for a follow-up just added or re-dated: already marked when it's due already,
    so adding (or moving) one into the past doesn't push straight away."""
    return _utc(now) if f["status"] == "open" and due_at(f) <= now else None


def clean_followup(patch: dict[str, Any], current: Optional[dict[str, Any]] = None,
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """The checked version of a new follow-up (current None: title and due date required) or of a
    change to one. Marking it done stamps done_at; a new due date / time, or reopening it, starts
    its reminder over. Raises ActivityError."""
    allowed = ("title", "notes", "due_date", "due_time", "status", *SUBJECTS)
    unknown = set(patch) - set(allowed)
    if unknown:
        raise ActivityError(f"Can't set: {', '.join(sorted(unknown))}")
    now = _now(now)
    out: dict[str, Any] = {}
    if "title" in patch or current is None:
        out["title"] = re.sub(r"\s+", " ", _text(patch.get("title"), "Follow-up", MAX_TITLE, required=True))
    if "notes" in patch:
        out["notes"] = _text(patch["notes"], "Notes", MAX_NOTES) or ""
    if "due_date" in patch or current is None:
        out["due_date"] = _due_date(patch.get("due_date"))
    if "due_time" in patch:
        out["due_time"] = _due_time(patch["due_time"])
    if "status" in patch:
        if patch["status"] not in FOLLOWUP_STATUSES:
            raise ActivityError("Status must be open or done")
        out["status"] = patch["status"]
    if any(k in patch for k in SUBJECTS) or current is None:
        out.update(_subject(patch, current))
    merged = {**(current or {"status": "open"}), **out}
    if current is None:
        merged.setdefault("due_time", None)
        out["reminded_at"] = _reminded(merged, now)
        if merged["status"] == "done":
            out["done_at"] = _utc(now)
    else:
        if "status" in out and out["status"] != current["status"]:
            out["done_at"] = _utc(now) if out["status"] == "done" else None
        redated = any(k in out and out[k] != current.get(k) for k in ("due_date", "due_time"))
        reopened = out.get("status") == "open" and current["status"] != "open"
        if redated or reopened:
            out["reminded_at"] = _reminded(merged, now)
    return out


def create_followup(patch: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    return followup_view(db.add_followup(clean_followup(patch, now=now)), now)


def update_followup(followup_id: int, patch: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    current = db.get_followup(followup_id)
    if not current:
        raise NotFound("That follow-up is gone")
    db.update_followup(followup_id, clean_followup(patch, current, now))
    return followup_view(db.get_followup(followup_id), now)


# ------------------------------------------------------------------ views

class _Names:
    """Project / client / contact names, read once per request."""

    def __init__(self) -> None:
        self.projects = {p["id"]: p for p in db.list_projects()}
        self.clients = {c["id"]: c for c in db.list_clients()}
        self.contacts = {c["id"]: c for c in db.list_contacts()}

    def subject(self, row: dict[str, Any]) -> dict[str, Any]:
        """{subject: 'project'|'client'|'contact', subject_id, subject_name}."""
        for kind, table in (("project", self.projects), ("client", self.clients), ("contact", self.contacts)):
            sid = row.get(f"{kind}_id")
            if sid:
                return {"subject": kind, "subject_id": sid, "subject_name": (table.get(sid) or {}).get("name")}
        return {"subject": None, "subject_id": None, "subject_name": None}

    def people(self, ids: list[int]) -> list[dict[str, Any]]:
        return [{"id": i, "name": self.contacts[i]["name"]} for i in ids if i in self.contacts]


def activity_view(a: dict[str, Any], names: Optional[_Names] = None) -> dict[str, Any]:
    names = names or _Names()
    return {**a, **names.subject(a), "kind_label": KIND_LABELS.get(a["kind"], a["kind"]),
            "people": names.people(a.get("contact_ids") or [])}


def followup_view(f: dict[str, Any], now: Optional[datetime] = None, names: Optional[_Names] = None) -> dict[str, Any]:
    """A follow-up as the dashboard shows it: due_at (UTC ISO), days_left, overdue, and what it's on."""
    now = _now(now)
    names = names or _Names()
    days = (date.fromisoformat(f["due_date"]) - now.date()).days
    return {**f, **names.subject(f), "due_at": _utc(due_at(f)), "days_left": days, "overdue": is_overdue(f, now)}


def list_followups(statuses: Optional[list[str]] = None, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every follow-up for Home and the Calendar: the open ones and the ones done in the last
    DONE_SHOWN_DAYS, soonest due first."""
    now = _now(now)
    since = _utc(now - timedelta(days=DONE_SHOWN_DAYS))
    names = _Names()
    rows = db.list_followups(statuses or list(FOLLOWUP_STATUSES), done_since=since)
    return [followup_view(f, now, names) for f in rows]


def _scope(project_id: Optional[int], client_id: Optional[int], contact_id: Optional[int]) -> dict[str, list[int]]:
    """Whose activity a page's timeline shows. A project: its own. A contact: their own, and every
    activity they were in. A client: its own, its projects' and its contacts'."""
    if project_id:
        return {"project_ids": [project_id], "client_ids": [], "contact_ids": []}
    if contact_id:
        return {"project_ids": [], "client_ids": [], "contact_ids": [contact_id]}
    return {"project_ids": [p["id"] for p in db.client_projects(client_id)], "client_ids": [client_id],
            "contact_ids": [c["id"] for c in db.list_contacts(client_id)]}


def feed(project_id: Optional[int] = None, client_id: Optional[int] = None, contact_id: Optional[int] = None,
         limit: int = FEED_LIMIT, now: Optional[datetime] = None) -> dict[str, Any]:
    """The activity timeline of one project, client or contact: {"open": open follow-ups (overdue
    first, then soonest due), "items": newest first}. items mix activities (type 'activity' - a
    stage change is kind 'stage'), follow-ups done (type 'followup', at done_at) and, for a project,
    its finished tasks (type 'task'). Each item has "at" (UTC ISO) to sort by."""
    if sum(1 for x in (project_id, client_id, contact_id) if x) != 1:
        raise ActivityError("Ask for one project, client or contact")
    if project_id and not db.get_project(project_id):
        raise NotFound("Project not found")
    if client_id and not db.get_client(client_id):
        raise NotFound("Client not found")
    if contact_id and not db.get_contact(contact_id):
        raise NotFound("Contact not found")
    now = _now(now)
    scope = _scope(project_id, client_id, contact_id)
    names = _Names()
    items: list[dict[str, Any]] = []
    for a in db.list_crm_activities(scope["project_ids"], scope["client_ids"], scope["contact_ids"], limit):
        items.append({"type": "activity", "at": a["occurred_at"], **activity_view(a, names)})
    fus = [followup_view(f, now, names) for f in db.list_followups(None, scope["project_ids"], scope["client_ids"], scope["contact_ids"])]
    for f in fus:
        if f["status"] == "done" and f.get("done_at"):
            items.append({"type": "followup", "at": f["done_at"], **f})
    if project_id:
        for t in db.finished_tasks(project_id, limit):
            items.append({"type": "task", "at": t["finished_at"], "id": t["id"], "thread_id": t.get("thread_id"),
                          "title": t.get("title") or (t.get("prompt") or "")[:120], "status": t["status"], "kind": t.get("kind")})
    items.sort(key=lambda x: _sort_key(x["at"]), reverse=True)
    open_ = sorted((f for f in fus if f["status"] == "open"), key=lambda f: (not f["overdue"], f["due_at"]))
    return {"open": open_, "items": items[:limit]}


def _sort_key(iso: Optional[str]) -> datetime:
    try:
        return timeutil.parse_iso(iso).astimezone(timezone.utc) if iso else datetime.min.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def last_activity() -> dict[int, str]:
    """Per project, when anything last happened on it (UTC ISO): an activity logged, a stage change,
    a follow-up added or done, a task finished. Projects with none of these are left out."""
    return {pid: _utc(max(_sort_key(x) for x in times)) for pid, times in db.last_activity_times().items()}


# ------------------------------------------------------------------ reminders

def pending_reminders(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """[{followup, title, message, open_hash}] - open follow-ups whose due time has come and whose
    reminder hasn't gone out yet."""
    now = _now(now)
    names = _Names()
    out = []
    for f in db.list_followups(["open"]):
        if f.get("reminded_at") or due_at(f) > now:
            continue
        v = followup_view(f, now, names)
        where = v["subject_name"] or ""
        when = f"due at {timeutil.fmt_time(due_at(f))}" if f.get("due_time") else "due today"
        if v["days_left"] < 0:
            when = f"was due {timeutil.fmt_day(date.fromisoformat(f['due_date']))}"
        hash_ = {"project": f"#/project/{v['subject_id']}/overview", "client": f"#/client/{v['subject_id']}",
                 "contact": f"#/contact/{v['subject_id']}"}.get(v["subject"], "#/home")
        out.append({"followup": f, "title": "Follow-up" + (f" · {where}" if where else ""),
                    "message": f"{f['title']} - {when}."[:300], "open_hash": hash_})
    return out


def mark_reminded(f: dict[str, Any], now: Optional[datetime] = None) -> None:
    db.update_followup(f["id"], {"reminded_at": _utc(_now(now))})
