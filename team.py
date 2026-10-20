"""
The team: the company's employees.

An employee has a name, job title, department, how to reach them, a status (active / on leave /
inactive), how they're employed, who they report to, a start date, skills and notes. They can be given
tasks (db.employee_tasks - work for a person, separate from the Octopus run queue), have time off
(db.employee_leave) and attend meetings on the project calendar (db.event_employees, set through
timeline.create / update with employee_ids; their email address is invited with the meeting, see
invites.guest_emails).

This module checks a change before it is saved (clean_employee / clean_task / clean_leave - every
message is fit to show the user) and works out what the Team pages show on top of the rows: open and
overdue task counts, who is away today, and the upcoming meetings.
"""

import re
from datetime import date
from typing import Any, Optional

import crm
import db
import timeline

STATUSES = ("active", "on_leave", "inactive")
EMPLOYMENT_TYPES = ("full_time", "part_time", "contractor", "intern")
TASK_STATUSES = ("open", "in_progress", "done")
PRIORITIES = ("low", "normal", "high")
LEAVE_KINDS = ("vacation", "sick", "other")

MAX_NAME = 120
MAX_SHORT = 120
MAX_TITLE = 200
MAX_NOTES = 20_000
MAX_EMAIL = 254
MAX_PHONE = 40
MAX_SKILLS = 500

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+()\-.\s/]+$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class TeamError(crm.RecordError):
    """A value an employee, task or leave can't hold - the message is shown to the user as is."""


def _text(value: Any, label: str, limit: int, required: bool = False) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise TeamError(f"{label} must be text")
    value = re.sub(r"[ \t]+", " ", value).strip() if value else ""
    if required and not value:
        raise TeamError(f"Give the {label.lower()}")
    if len(value) > limit:
        raise TeamError(f"{label} is too long (at most {limit} characters)")
    return value or None


def _notes(value: Any) -> str:
    if value is not None and not isinstance(value, str):
        raise TeamError("Notes must be text")
    value = (value or "").strip()
    if len(value) > MAX_NOTES:
        raise TeamError(f"Notes are too long (at most {MAX_NOTES} characters)")
    return value


def _choice(value: Any, options: tuple[str, ...], label: str) -> str:
    if value not in options:
        raise TeamError(f"{label} must be one of: {', '.join(options)}")
    return value


def _day(value: Any, label: str, optional: bool = True) -> Optional[str]:
    if value in (None, ""):
        if optional:
            return None
        raise TeamError(f"Give the {label.lower()}")
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise TeamError(f"{label} must be a date like 2026-10-31") from None


def _id(value: Any, label: str) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TeamError(f"{label} must be a number")
    return value


# ------------------------------------------------------------------ employees

def clean_employee(patch: dict[str, Any], employee_id: Optional[int] = None) -> dict[str, Any]:
    """The checked, normalised version of a new employee (employee_id None: name required) or of a
    change to one (only the fields sent). None or "" clears an optional field. Raises TeamError."""
    unknown = set(patch) - set(db.EMPLOYEE_FIELDS)
    if unknown:
        raise TeamError(f"Can't set: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k == "name":
            out[k] = _text(v, "Name", MAX_NAME, required=True)
        elif k in ("job_title", "department", "location"):
            out[k] = _text(v, {"job_title": "Job title", "department": "Department", "location": "Location"}[k], MAX_SHORT)
        elif k == "email":
            email = _text(v, "Email", MAX_EMAIL)
            if email and not _EMAIL_RE.match(email):
                raise TeamError(f"Not an email address: {email[:80]}")
            out[k] = email.lower() if email else None
        elif k == "phone":
            phone = _text(v, "Phone", MAX_PHONE)
            if phone and (not _PHONE_RE.match(phone) or sum(ch.isdigit() for ch in phone) < 3):
                raise TeamError("A phone number has digits, and maybe + ( ) - . or spaces")
            out[k] = phone
        elif k == "status":
            out[k] = _choice(v, STATUSES, "Status")
        elif k == "employment_type":
            out[k] = _choice(v, EMPLOYMENT_TYPES, "Employment type")
        elif k == "manager_id":
            mid = _id(v, "Manager")
            if mid is not None:
                if mid == employee_id:
                    raise TeamError("Nobody can report to themselves")
                if not db.get_employee(mid):
                    raise TeamError("That manager doesn't exist (any more)")
                if employee_id is not None and _reports_up_to(mid, employee_id):
                    raise TeamError("That would make a loop: they already report to this person")
            out[k] = mid
        elif k == "start_date":
            out[k] = _day(v, "Start date")
        elif k == "skills":
            out[k] = _text(v, "Skills", MAX_SKILLS)
        elif k == "notes":
            out[k] = _notes(v)
        elif k == "color":
            if v in (None, ""):
                out[k] = None
            elif isinstance(v, str) and _COLOR_RE.match(v.strip()):
                out[k] = v.strip().lower()
            else:
                raise TeamError("Colour must look like #e8825a")
    if employee_id is None and "name" not in out:
        raise TeamError("Give the name")
    return out


def _reports_up_to(start_id: int, target_id: int) -> bool:
    """Whether target_id is anywhere in start_id's chain of managers."""
    seen = set()
    cur: Optional[int] = start_id
    while cur is not None and cur not in seen:
        if cur == target_id:
            return True
        seen.add(cur)
        row = db.get_employee(cur)
        cur = row["manager_id"] if row else None
    return False


# ---------------------------------------------------------------------- tasks

def clean_task(patch: dict[str, Any], new: bool = False) -> dict[str, Any]:
    """The checked version of a new task for an employee (new=True: employee_id and title required)
    or of a change to one. Marking it done stamps done_at; reopening clears it."""
    allowed = ("employee_id", "title", "notes", "due_date", "priority", "status", "project_id")
    unknown = set(patch) - set(allowed)
    if unknown:
        raise TeamError(f"Can't set: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k == "employee_id":
            eid = _id(v, "Employee")
            if eid is None or not db.get_employee(eid):
                raise TeamError("That employee doesn't exist (any more)")
            out[k] = eid
        elif k == "title":
            out[k] = _text(v, "Task title", MAX_TITLE, required=True)
        elif k == "notes":
            out[k] = _notes(v)
        elif k == "due_date":
            out[k] = _day(v, "Due date")
        elif k == "priority":
            out[k] = _choice(v, PRIORITIES, "Priority")
        elif k == "status":
            out[k] = _choice(v, TASK_STATUSES, "Status")
            out["done_at"] = db._now() if v == "done" else None
        elif k == "project_id":
            pid = _id(v, "Project")
            if pid is not None and not db.get_project(pid):
                raise TeamError("That project doesn't exist (any more)")
            out[k] = pid
    if new:
        if "employee_id" not in out:
            raise TeamError("Pick who the task is for")
        if "title" not in out:
            raise TeamError("Give the task title")
    return out


# ---------------------------------------------------------------------- leave

def clean_leave(patch: dict[str, Any]) -> dict[str, Any]:
    """The checked version of a new stretch of time off (employee_id, start_date and end_date required)."""
    allowed = ("employee_id", "kind", "start_date", "end_date", "note")
    unknown = set(patch) - set(allowed)
    if unknown:
        raise TeamError(f"Can't set: {', '.join(sorted(unknown))}")
    eid = _id(patch.get("employee_id"), "Employee")
    if eid is None or not db.get_employee(eid):
        raise TeamError("That employee doesn't exist (any more)")
    start = _day(patch.get("start_date"), "Start date", optional=False)
    end = _day(patch.get("end_date") or start, "End date", optional=False)
    if end < start:
        raise TeamError("The last day can't be before the first")
    return {"employee_id": eid, "kind": _choice(patch.get("kind") or "vacation", LEAVE_KINDS, "Kind"),
            "start_date": start, "end_date": end, "note": _text(patch.get("note"), "Note", MAX_SHORT)}


# ------------------------------------------------------------------ worked out

def _today() -> str:
    return timeline.today().isoformat()


def away_today(leave: list[dict[str, Any]], today: Optional[str] = None) -> set[int]:
    """The employees with time off covering today."""
    today = today or _today()
    return {l["employee_id"] for l in leave if l["start_date"] <= today <= l["end_date"]}


def summary(employee: dict[str, Any], tasks: list[dict[str, Any]], away: bool, today: Optional[str] = None) -> dict[str, Any]:
    """An employee with the figures their list row shows: open / overdue tasks and whether they're away today."""
    today = today or _today()
    open_ = [t for t in tasks if t["status"] != "done"]
    return {**employee, "open_tasks": len(open_), "overdue_tasks": sum(1 for t in open_ if t["due_date"] and t["due_date"] < today),
            "away_today": away}


def list_all() -> list[dict[str, Any]]:
    """Every employee with their task counts (one pass over tasks and leave)."""
    by_emp: dict[int, list[dict[str, Any]]] = {}
    for t in db.list_employee_tasks():
        by_emp.setdefault(t["employee_id"], []).append(t)
    away = away_today(db.list_leave())
    return [summary(e, by_emp.get(e["id"], []), e["id"] in away) for e in db.list_employees()]


def page(employee_id: int) -> Optional[dict[str, Any]]:
    """One employee's page: details, tasks, time off, direct reports, and their meetings (upcoming first)."""
    emp = db.get_employee(employee_id)
    if not emp:
        return None
    today = _today()
    tasks = db.list_employee_tasks(employee_id)
    leave = db.list_leave(employee_id)
    meetings = [m for m in timeline.views(db.employee_meetings(employee_id)) if m["status"] != "cancelled"]
    upcoming = [m for m in meetings if m["status"] == "open"]
    manager = db.get_employee(emp["manager_id"]) if emp.get("manager_id") else None
    reports = [e for e in db.list_employees() if e.get("manager_id") == employee_id]
    return {**summary(emp, tasks, employee_id in away_today(leave, today), today), "tasks": tasks, "leave": leave,
            "meetings": upcoming, "past_meetings": [m for m in meetings if m["status"] != "open"][-10:],
            "manager": {"id": manager["id"], "name": manager["name"]} if manager else None,
            "reports": [{"id": r["id"], "name": r["name"], "job_title": r["job_title"]} for r in reports]}


def overview() -> dict[str, Any]:
    """The Team page header: headcount by status, who is away today, and tasks that are overdue."""
    emps = list_all()
    return {"total": len(emps), "active": sum(1 for e in emps if e["status"] == "active"),
            "away_today": [e["id"] for e in emps if e["away_today"]],
            "open_tasks": sum(e["open_tasks"] for e in emps), "overdue_tasks": sum(e["overdue_tasks"] for e in emps),
            "departments": sorted({e["department"] for e in emps if e.get("department")}, key=str.lower)}


def clean_employee_ids(value: Any) -> list[int]:
    """Meeting attendees: employee ids, each one existing (used by timeline for a meeting's employee_ids)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TeamError("employee_ids must be a list of employees")
    try:
        ids = list(dict.fromkeys(int(v) for v in value))
    except (TypeError, ValueError):
        raise TeamError("employee_ids must be a list of employees") from None
    for eid in ids:
        if not db.get_employee(eid):
            raise TeamError("One of those employees doesn't exist (any more)")
    return ids
