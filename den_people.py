"""
The Contacts and Team pages of The Den 2.0 (static/den/DenPeople.dc.html):
what they show that the CRM and team endpoints don't give on their own.

Contacts are the CRM's contacts (db.contacts, checked by clients.py). The page
also wants, per contact, a type (client / lead / vendor / team), a favourite
star, tags and a city - kept here in den_contact_meta - plus its linked projects
and deals, worked out from the projects it's on and its client's projects (a
CRM deal is a project). Notes are the contact's own notes column.

The team is team.py's employees. On top of them this keeps:
- den_staff: how many open tasks a person can carry (capacity) and their
  yearly leave allowance in working days.
- den_leave_requests: time off asked for, waiting for a yes or no. Only an
  approved request becomes a real db.employee_leave row, so everything else
  that reads time off (away today, the old Team page) never sees a pending
  or rejected one. Time off added the old way shows as approved.
- den_task_state: the "Review" step. employee_tasks only knows open /
  in_progress / done, so Review is in_progress plus a flag here - the rest of
  the Den keeps counting it as work under way.
"""

import json
import re
from datetime import date, timedelta
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import clients
import crm
import db
import team

KINDS = ("client", "lead", "vendor", "team")
LEAVE_TYPES = ("Casual", "Sick", "Earned", "Unpaid")
# employee_leave only has vacation / sick / other; the request keeps the exact type.
TYPE_KIND = {"Casual": "other", "Sick": "sick", "Earned": "vacation", "Unpaid": "other"}
KIND_TYPE = {"vacation": "Earned", "sick": "Sick", "other": "Casual"}
# Leave that doesn't come out of the allowance.
UNCOUNTED = ("Unpaid",)
STAGES = ("todo", "doing", "review", "done")
STAGE_STATUS = {"todo": "open", "doing": "in_progress", "review": "in_progress", "done": "done"}
# A deal is a project put in a selling stage on the CRM board (or lost there), or any project with a value
# on it - not every project whose status only implies "lead" because nothing has started yet.
SALE_STAGES = ("lead", "proposal", "negotiation", "lost")
METHOD_TAGS = {"email": "Prefers email", "phone": "Prefers calls", "text": "Prefers texts", "whatsapp": "Prefers WhatsApp"}

DEFAULT_CAPACITY = 8
DEFAULT_ALLOWANCE = 12.0
MAX_TAGS = 12
MAX_TAG = 40
MAX_CITY = 120

_ready_conn = None


def _init() -> None:
    """Create this page's tables the first time they're needed on this database."""
    global _ready_conn
    conn = db.get_conn()
    if _ready_conn is conn:
        return
    with db._lock:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS den_contact_meta (
                contact_id INTEGER PRIMARY KEY, kind TEXT, fav INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '[]', city TEXT, company TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS den_staff (
                employee_id INTEGER PRIMARY KEY, capacity INTEGER, leave_allowance REAL, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS den_leave_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, type TEXT NOT NULL,
                start_date TEXT NOT NULL, end_date TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'pending',
                leave_id INTEGER, created_at TEXT NOT NULL, decided_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_den_leave_req_emp ON den_leave_requests(employee_id, start_date);
            CREATE TABLE IF NOT EXISTS den_task_state (task_id INTEGER PRIMARY KEY, review INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
        """)
        conn.commit()
    _ready_conn = conn


def _rows(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    _init()
    with db._lock:
        return [dict(r) for r in db.get_conn().execute(sql, args).fetchall()]


def _exec(sql: str, args: tuple = ()) -> int:
    _init()
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def _upsert(table: str, key: str, key_value: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    fields = {**fields, "updated_at": db._now()}
    cols = ", ".join(fields)
    _exec(f"INSERT INTO {table} ({key}, {cols}) VALUES (?, {', '.join('?' * len(fields))}) "
          f"ON CONFLICT({key}) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in fields)}",
          (key_value, *fields.values()))


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


# ------------------------------------------------------------------ contacts

def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise _bad("Tags must be a list of words")
    out = list(dict.fromkeys(re.sub(r"\s+", " ", t).strip() for t in value if t and t.strip()))
    if len(out) > MAX_TAGS:
        raise _bad(f"At most {MAX_TAGS} tags")
    if any(len(t) > MAX_TAG for t in out):
        raise _bad(f"A tag is at most {MAX_TAG} characters")
    return out


def _short(value: Any, label: str, limit: int) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise _bad(f"{label} must be text")
    value = (value or "").strip()
    if len(value) > limit:
        raise _bad(f"{label} is too long (at most {limit} characters)")
    return value or None


def _meta_patch(body: dict[str, Any]) -> dict[str, Any]:
    """The den_contact_meta columns a request changes (kind, fav, tags, city), checked."""
    out: dict[str, Any] = {}
    if "kind" in body:
        if body["kind"] not in KINDS:
            raise _bad(f"Type must be one of: {', '.join(KINDS)}")
        out["kind"] = body["kind"]
    if "fav" in body:
        out["fav"] = 1 if body["fav"] else 0
    if "tags" in body:
        out["tags"] = json.dumps(_tags(body["tags"]))
    if "city" in body:
        out["city"] = _short(body["city"], "Location", MAX_CITY)
    return out


def _company(name: Any) -> tuple[Optional[int], Optional[str]]:
    """A company typed in the form: the client it names (client_id), or else just the text to keep.
    It never makes a new client - that stays a choice made on the CRM page."""
    name = _short(name, "Company", clients.MAX_NAME)
    if not name:
        return None, None
    c = db.get_client_by_name(name)
    return (c["id"], None) if c else (None, name)


def contacts_view() -> list[dict[str, Any]]:
    """Every contact as the page lists it: type, favourite, tags, city, company, and its linked
    projects and deals. A contact without a type picked gets one worked out from its record."""
    _init()
    meta = {m["contact_id"]: m for m in _rows("SELECT * FROM den_contact_meta")}
    clients_by_id = {c["id"]: c for c in db.list_clients()}
    projects = [p for p in db.list_projects() if not db.is_general_project(p)]
    by_id = {p["id"]: p for p in projects}
    by_client: dict[int, list[dict[str, Any]]] = {}
    for p in projects:
        if p.get("client_id"):
            by_client.setdefault(p["client_id"], []).append(p)
    staff_emails = {(e.get("email") or "").lower() for e in db.list_employees() if e.get("email")}
    key_of: set[int] = {r["contact_id"] for r in _rows("SELECT contact_id FROM project_contacts WHERE role = 'key'")}
    out = []
    for c in db.list_contacts():
        m = meta.get(c["id"]) or {}
        client = clients_by_id.get(c.get("client_id"))
        kind = m.get("kind")
        if kind not in KINDS:
            if c.get("email") and c["email"].lower() in staff_emails:
                kind = "team"
            elif client:
                kind = "lead" if client.get("status") == "prospect" else "client"
            else:
                kind = "lead"
        try:
            own_tags = json.loads(m.get("tags") or "[]")
        except ValueError:
            own_tags = []
        auto = (["Key contact"] if c["id"] in key_of else []) + ([METHOD_TAGS[c["preferred_method"]]] if c.get("preferred_method") in METHOD_TAGS else [])
        linked = list(dict.fromkeys([pid for pid in c.get("project_ids") or [] if pid in by_id] +
                                    [p["id"] for p in by_client.get(c.get("client_id"), [])]))
        projs = [by_id[pid] for pid in linked]
        deals = [p for p in projs if p.get("stage") in SALE_STAGES or p.get("contract_value") is not None]
        city = m.get("city") or ((client.get("address") or "").strip().splitlines()[-1].strip() if client and client.get("address") else None)
        out.append({**c, "kind": kind, "kind_set": bool(m.get("kind")), "fav": bool(m.get("fav")),
                    "tags": list(dict.fromkeys(own_tags + auto)), "own_tags": own_tags, "city": city,
                    "company": client["name"] if client else m.get("company"), "client_name": client["name"] if client else None,
                    "projects": [{"id": p["id"], "name": p["name"], "color": p.get("color")} for p in projs],
                    "deals": [{"id": p["id"], "name": p["name"], "value": p.get("contract_value"), "currency": p.get("currency") or "USD",
                               "billing_type": p.get("billing_type"), "stage": crm.stage_of(p)} for p in deals]})
    return out


def contact_view(contact_id: int) -> dict[str, Any]:
    return next(c for c in contacts_view() if c["id"] == contact_id)


class ContactIn(BaseModel):
    # Send only what changes. The contact's own fields go through clients.clean_contact.
    name: Optional[str] = None
    role: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    preferred_method: Optional[str] = None
    client_id: Optional[int] = None
    company: Optional[str] = None                 # matched to a client by name, else kept as text
    kind: Optional[str] = None                    # client | lead | vendor | team
    fav: Optional[bool] = None
    tags: Optional[list[str]] = None
    city: Optional[str] = None


_CONTACT_OWN = ("name", "role", "email", "phone", "notes", "preferred_method", "client_id")


def _split(body: ContactIn) -> tuple[dict[str, Any], dict[str, Any]]:
    sent = {k: getattr(body, k) for k in body.model_fields_set}
    own = {k: v for k, v in sent.items() if k in _CONTACT_OWN}
    meta = _meta_patch(sent)
    if "company" in sent and "client_id" not in sent:
        cid, text = _company(sent["company"])
        own["client_id"] = cid
        meta["company"] = text
    return own, meta


def create_contact(body: ContactIn) -> dict[str, Any]:
    own, meta = _split(body)
    try:
        fields = clients.clean_contact(own, new=True)
    except clients.ClientError as e:
        raise _bad(str(e))
    contact = db.add_contact(fields)
    _upsert("den_contact_meta", "contact_id", contact["id"], meta)
    return contact_view(contact["id"])


def update_contact(contact_id: int, body: ContactIn) -> dict[str, Any]:
    if not db.get_contact(contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    own, meta = _split(body)
    try:
        fields = clients.clean_contact(own)
    except clients.ClientError as e:
        raise _bad(str(e))
    db.update_contact(contact_id, fields)
    _upsert("den_contact_meta", "contact_id", contact_id, meta)
    return contact_view(contact_id)


# ---------------------------------------------------------------------- team

def _today() -> str:
    return team._today()


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def workdays(start: str, end: str, year: Optional[int] = None) -> int:
    """Monday-to-Friday days from start to end, inclusive (only those in `year`, if given)."""
    d, last, n = date.fromisoformat(start), date.fromisoformat(end), 0
    while d <= last:
        if d.weekday() < 5 and (year is None or d.year == year):
            n += 1
        d += timedelta(days=1)
    return n


def leave_list() -> list[dict[str, Any]]:
    """Everyone's time off and requests, one list: pending and rejected requests (id "r<n>") and
    every real time-off row (id "l<n>", approved) - with the requested type and note when it came
    from a request. Rows of people no longer on the team are left out."""
    _init()
    emps = {e["id"] for e in db.list_employees()}
    reqs = [r for r in _rows("SELECT * FROM den_leave_requests ORDER BY start_date, id") if r["employee_id"] in emps]
    by_leave = {r["leave_id"]: r for r in reqs if r["status"] == "approved" and r["leave_id"]}
    out = []
    for r in reqs:
        if r["status"] in ("pending", "rejected"):
            out.append({"id": f"r{r['id']}", "request_id": r["id"], "employee_id": r["employee_id"], "type": r["type"],
                        "start_date": r["start_date"], "end_date": r["end_date"], "note": r["note"], "status": r["status"]})
    for l in db.list_leave():
        if l["employee_id"] not in emps:
            continue
        r = by_leave.get(l["id"])
        out.append({"id": f"l{l['id']}", "request_id": r["id"] if r else None, "leave_id": l["id"], "employee_id": l["employee_id"],
                    "type": r["type"] if r else KIND_TYPE.get(l["kind"], "Casual"), "start_date": l["start_date"],
                    "end_date": l["end_date"], "note": l.get("note") or (r and r["note"]), "status": "approved"})
    for x in out:
        x["days"] = _days(x["start_date"], x["end_date"])
        x["workdays"] = workdays(x["start_date"], x["end_date"])
    return sorted(out, key=lambda x: (x["start_date"], x["id"]))


def _stages() -> dict[int, bool]:
    return {r["task_id"]: bool(r["review"]) for r in _rows("SELECT * FROM den_task_state")}


def stage_of(task: dict[str, Any], review: bool = False) -> str:
    """The four-step status the page shows, from the task's own status and the Review flag."""
    if task["status"] == "done":
        return "done"
    if task["status"] == "in_progress":
        return "review" if review else "doing"
    return "todo"


def team_view() -> dict[str, Any]:
    """The Team page in one call: the people (with capacity, allowance and leave used this year),
    their tasks with the four-step stage, and everyone's time off and requests."""
    _init()
    today = _today()
    year = int(today[:4])
    staff_meta = {r["employee_id"]: r for r in _rows("SELECT * FROM den_staff")}
    leave = leave_list()
    review = _stages()
    projects = {p["id"]: p for p in db.list_projects()}
    staff = []
    for e in team.list_all():
        if e["status"] == "inactive":
            continue
        m = staff_meta.get(e["id"]) or {}
        allowance = m.get("leave_allowance") if m.get("leave_allowance") is not None else DEFAULT_ALLOWANCE
        used = sum(workdays(l["start_date"], l["end_date"], year) for l in leave
                   if l["employee_id"] == e["id"] and l["status"] == "approved" and l["type"] not in UNCOUNTED)
        staff.append({**e, "skills_list": [s.strip() for s in re.split(r"[,;\n]", e.get("skills") or "") if s.strip()],
                      "capacity": m.get("capacity") or DEFAULT_CAPACITY, "leave_allowance": allowance,
                      "leave_used": used, "leave_left": max(0, allowance - used)})
    tasks = []
    for t in db.list_employee_tasks():
        p = projects.get(t.get("project_id"))
        tasks.append({**t, "stage": stage_of(t, review.get(t["id"], False)),
                      "project_name": p["name"] if p else None})
    return {"today": today, "staff": staff, "tasks": tasks, "leave": leave, "leave_types": LEAVE_TYPES, "stages": STAGES}


class StaffIn(BaseModel):
    capacity: Optional[int] = None                # open tasks they can carry at once
    leave_allowance: Optional[float] = None       # working days a year


class LeaveRequestIn(BaseModel):
    employee_id: int
    type: str = "Casual"                          # Casual | Sick | Earned | Unpaid
    start_date: str
    end_date: Optional[str] = None                # defaults to start_date
    note: Optional[str] = None


class StageIn(BaseModel):
    stage: str                                    # todo | doing | review | done


def set_staff(employee_id: int, body: StaffIn) -> dict[str, Any]:
    if not db.get_employee(employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    fields: dict[str, Any] = {}
    if "capacity" in body.model_fields_set:
        if body.capacity is not None and not 1 <= body.capacity <= 100:
            raise _bad("Capacity is between 1 and 100 tasks")
        fields["capacity"] = body.capacity
    if "leave_allowance" in body.model_fields_set:
        if body.leave_allowance is not None and not 0 <= body.leave_allowance <= 365:
            raise _bad("The leave allowance is between 0 and 365 days")
        fields["leave_allowance"] = body.leave_allowance
    _upsert("den_staff", "employee_id", employee_id, fields)
    return next((s for s in team_view()["staff"] if s["id"] == employee_id), db.get_employee(employee_id))


def request_leave(body: LeaveRequestIn) -> dict[str, Any]:
    """A request for time off, waiting for Approve / Reject. Checked the same way real time off is."""
    if body.type not in LEAVE_TYPES:
        raise _bad(f"Type must be one of: {', '.join(LEAVE_TYPES)}")
    try:
        f = team.clean_leave({"employee_id": body.employee_id, "kind": TYPE_KIND[body.type], "start_date": body.start_date,
                              "end_date": body.end_date or body.start_date, "note": body.note})
    except team.TeamError as e:
        raise _bad(str(e))
    rid = _exec("INSERT INTO den_leave_requests (employee_id, type, start_date, end_date, note, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)", (f["employee_id"], body.type, f["start_date"], f["end_date"], f["note"], db._now()))
    return next(x for x in leave_list() if x["request_id"] == rid)


def _pending(request_id: int) -> dict[str, Any]:
    rows = _rows("SELECT * FROM den_leave_requests WHERE id = ?", (request_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="That request is gone")
    if rows[0]["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"That request was already {rows[0]['status']}")
    if not db.get_employee(rows[0]["employee_id"]):
        raise HTTPException(status_code=404, detail="That person isn't on the team any more")
    return rows[0]


def decide_leave(request_id: int, approve: bool) -> dict[str, Any]:
    """Approve (it becomes real time off) or reject a pending request."""
    r = _pending(request_id)
    if not approve:
        _exec("UPDATE den_leave_requests SET status = 'rejected', decided_at = ? WHERE id = ?", (db._now(), request_id))
        return next(x for x in leave_list() if x["request_id"] == request_id)
    leave = db.add_leave(team.clean_leave({"employee_id": r["employee_id"], "kind": TYPE_KIND.get(r["type"], "other"),
                                           "start_date": r["start_date"], "end_date": r["end_date"], "note": r["note"]}))
    _exec("UPDATE den_leave_requests SET status = 'approved', leave_id = ?, decided_at = ? WHERE id = ?", (leave["id"], db._now(), request_id))
    return next(x for x in leave_list() if x.get("leave_id") == leave["id"])


def set_stage(task_id: int, stage: str) -> dict[str, Any]:
    """Move a person's task along To do -> In progress -> Review -> Done."""
    t = db.get_employee_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if stage not in STAGES:
        raise _bad(f"Stage must be one of: {', '.join(STAGES)}")
    if STAGE_STATUS[stage] != t["status"]:
        db.update_employee_task(task_id, team.clean_task({"status": STAGE_STATUS[stage]}))
    _upsert("den_task_state", "task_id", task_id, {"review": 1 if stage == "review" else 0})
    t = db.get_employee_task(task_id)
    return {**t, "stage": stage_of(t, stage == "review")}


# -------------------------------------------------------------------- router

def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/people/contacts")
    def list_contacts():
        return {"contacts": contacts_view(), "kinds": KINDS}

    @r.post("/den/people/contacts")
    def new_contact(body: ContactIn):
        return create_contact(body)

    @r.patch("/den/people/contacts/{contact_id}")
    def patch_contact(contact_id: int, body: ContactIn):
        return update_contact(contact_id, body)

    @r.get("/den/people/team")
    def get_team():
        return team_view()

    @r.patch("/den/people/staff/{employee_id}")
    def patch_staff(employee_id: int, body: StaffIn):
        return set_staff(employee_id, body)

    @r.post("/den/people/leave")
    def new_leave(body: LeaveRequestIn):
        return request_leave(body)

    @r.post("/den/people/leave/{request_id}/approve")
    def approve_leave(request_id: int):
        return decide_leave(request_id, True)

    @r.post("/den/people/leave/{request_id}/reject")
    def reject_leave(request_id: int):
        return decide_leave(request_id, False)

    @r.post("/den/people/tasks/{task_id}/stage")
    def task_stage(task_id: int, body: StageIn):
        return set_stage(task_id, body.stage)

    return r
