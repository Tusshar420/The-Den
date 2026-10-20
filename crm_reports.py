"""
The CRM dashboard and reports: what needs attention across every project, and a summary of one
client or one project to download.

dashboard() is the CRM page (#/crm): projects overdue and due in the next 7 / 30 days, at-risk and
off-track ones, follow-ups due today and overdue, the pipeline by stage (count + value), active
value by client, projects with nothing happening for STALE_DAYS+ days and a workload view by due
week. It takes the dashboard's Work / Personal / All switch (scope) and an optional client. Its
"counts" are what Home's compact CRM card shows.

project_report() / client_report() are the summaries (details, stage history, activities,
follow-ups, value, progress); to_markdown() / to_csv() turn either into a file.

Nothing here writes - it only reads what crm.py, clients.py and activities.py already keep.
"""

import csv
import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import activities
import clients
import crm
import db
from assistant import timeutil

SCOPES = ("all", "work", "personal")
# Still in play: not done and not lost.
OPEN_STAGES = ("lead", "proposal", "negotiation", "active", "on_hold")
# An open project with nothing logged, moved or finished for this long is "gone quiet".
STALE_DAYS = 14
# The workload view: this week and the ones after it.
WORKLOAD_WEEKS = 8
DUE_WEEK_DAYS = 7
DUE_MONTH_DAYS = 30
RISK_ORDER = {"off_track": 0, "at_risk": 1}
REPORT_LIMIT = 1000
FORMATS = ("json", "csv", "md")


class ReportError(ValueError):
    """A request the dashboard can't answer - the message is shown to the user as is."""


# ------------------------------------------------------------------ basics

def _local_now(now: Optional[datetime] = None) -> datetime:
    return (now or timeutil.now()).astimezone(timeutil.local_tz())


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = timeutil.parse_iso(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_open(p: dict[str, Any]) -> bool:
    """Still in play: an open stage, and not completed (a completed date or status counts as done)."""
    return crm.stage_of(p) in OPEN_STAGES and p.get("status") not in ("completed", "archived") and not p.get("completed_date")


def _money_rows(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Contract value per currency of these projects (whatever their stage), biggest first."""
    sums: dict[str, float] = {}
    for p in projects:
        if p.get("contract_value") is not None:
            cur = p.get("currency") or "USD"
            sums[cur] = sums.get(cur, 0.0) + p["contract_value"]
    return sorted(({"currency": k, "total": round(v, 2)} for k, v in sums.items()), key=lambda r: (-r["total"], r["currency"]))


def _main_currency(projects: list[dict[str, Any]]) -> str:
    """The currency most of these projects' contract values are in (USD with none; ties: the bigger total)."""
    counts: dict[str, list[float]] = {}
    for p in projects:
        if p.get("contract_value") is not None:
            c = counts.setdefault(p.get("currency") or "USD", [0, 0.0])
            c[0] += 1
            c[1] += p["contract_value"]
    return max(counts, key=lambda k: (counts[k][0], counts[k][1], k == "USD")) if counts else "USD"


def money(v: Optional[float], currency: Optional[str]) -> str:
    """"USD 7,000" / "INR 1,250.50" - plain text for the files."""
    if v is None:
        return ""
    text = f"{v:,.2f}"
    return f"{currency or 'USD'} {text[:-3] if text.endswith('.00') else text}"


def _local_stamp(iso: Optional[str]) -> str:
    dt = _parse(iso)
    return dt.astimezone(timeutil.local_tz()).strftime("%Y-%m-%d %H:%M") if dt else ""


# ------------------------------------------------------------------ dashboard

def _in_scope(projects: list[dict[str, Any]], scope: str, client_id: Optional[int]) -> list[dict[str, Any]]:
    """The projects the dashboard looks at: not General, not hidden, on the chosen side, for the chosen client."""
    return [p for p in projects if not db.is_general_project(p) and not p.get("hidden")
            and (scope == "all" or p.get("category") == scope) and (client_id is None or p.get("client_id") == client_id)]


def _row(p: dict[str, Any], client_names: dict[int, str], last: dict[int, str], today: date) -> dict[str, Any]:
    """A project as the dashboard lists it."""
    keep = ("id", "name", "category", "status", "priority", "health", "due_date", "start_date", "contract_value",
            "currency", "client_id", "color", "image", "owner")
    sched = crm.schedule(p, today)
    return {**{k: p.get(k) for k in keep}, "stage": crm.stage_of(p), "client_name": client_names.get(p.get("client_id")),
            "days_left": sched["days_left"], "due_state": sched["due_state"], "progress": crm.progress(p["id"])["percent"],
            "last_activity_at": last.get(p["id"])}


def _followups_in_scope(scope: str, client_id: Optional[int], projects: list[dict[str, Any]],
                        all_projects: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Open follow-ups the dashboard counts. On a project: when the project is in view. On a client or
    one of its contacts: when the client is the one picked or, with no client picked, when All is on
    or the client has a project on this side. A contact with no client only shows under All."""
    project_ids = {p["id"] for p in projects}
    if client_id is not None:
        client_ids = {client_id}
    elif scope == "all":
        client_ids = {c["id"] for c in db.list_clients()}
    else:
        client_ids = {p["client_id"] for p in all_projects if p.get("client_id") and p["id"] in project_ids}
    contact_client = {c["id"]: c.get("client_id") for c in db.list_contacts()}
    out = []
    for f in activities.list_followups(["open"], now):
        if f.get("project_id"):
            keep = f["project_id"] in project_ids
        elif f.get("client_id"):
            keep = f["client_id"] in client_ids
        elif f.get("contact_id"):
            owner = contact_client.get(f["contact_id"])
            keep = owner in client_ids if owner else (scope == "all" and client_id is None)
        else:
            keep = False
        if keep:
            out.append(f)
    return out


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def dashboard(scope: str = "all", client_id: Optional[int] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """Everything the CRM page shows, for one side (scope: all | work | personal) and maybe one client."""
    if scope not in SCOPES and scope not in db.project_categories():
        raise ReportError(f"scope must be one of: {', '.join(('all',) + db.project_categories())}")
    if client_id is not None and not db.get_client(client_id):
        raise ReportError("That client doesn't exist (any more)")
    now = _local_now(now)
    today = now.date()
    all_projects = db.list_projects()
    projects = _in_scope(all_projects, scope, client_id)
    client_list = db.list_clients()
    names = {c["id"]: c["name"] for c in client_list}
    last = activities.last_activity()
    rows = {p["id"]: _row(p, names, last, today) for p in projects}
    open_ = [p for p in projects if is_open(p)]
    open_rows = [rows[p["id"]] for p in open_]
    by_due = lambda r: (r["due_date"] or "9999", r["name"].lower())

    overdue = sorted((r for r in open_rows if r["due_state"] == "overdue"), key=by_due)
    due_week = sorted((r for r in open_rows if r["days_left"] is not None and 0 <= r["days_left"] <= DUE_WEEK_DAYS), key=by_due)
    due_month = sorted((r for r in open_rows if r["days_left"] is not None and DUE_WEEK_DAYS < r["days_left"] <= DUE_MONTH_DAYS), key=by_due)
    at_risk = sorted((r for r in open_rows if r["health"] in RISK_ORDER),
                     key=lambda r: (RISK_ORDER[r["health"]], r["due_date"] or "9999", r["name"].lower()))

    fus = _followups_in_scope(scope, client_id, projects, all_projects, now)
    fu_overdue = [f for f in fus if f["overdue"]]
    fu_today = [f for f in fus if not f["overdue"] and f["days_left"] == 0]

    pipeline = []
    for stage in crm.STAGES:
        in_stage = [p for p in projects if crm.stage_of(p) == stage]
        pipeline.append({"stage": stage, "label": activities.STAGE_LABELS[stage], "count": len(in_stage), "value": _money_rows(in_stage)})

    # Active value by client: work under way (clients.ACTIVE_STAGES), per client, biggest first.
    groups: dict[Optional[int], list[dict[str, Any]]] = {}
    for p in projects:
        groups.setdefault(p.get("client_id") if p.get("client_id") in names else None, []).append(p)
    by_client = []
    for cid, ps in groups.items():
        active = [p for p in ps if crm.stage_of(p) in clients.ACTIVE_STAGES]
        value = [{"currency": v["currency"], "active": v["active"]} for v in clients.value(ps) if v["active"]]
        if not active and not value:
            continue
        by_client.append({"client_id": cid, "client_name": names.get(cid) if cid else None, "active_projects": len(active),
                          "open_projects": sum(1 for p in ps if is_open(p)), "value": value})
    # Amounts in different currencies don't add up, so rows are ranked (and the page's bars drawn) in one
    # currency: the one most of these projects' values are in.
    currency = _main_currency(projects)
    amount = lambda r: next((v["active"] for v in r["value"] if v["currency"] == currency), 0.0)
    by_client.sort(key=lambda r: (-amount(r), -r["active_projects"], r["client_id"] is None, (r["client_name"] or "").lower()))

    # Gone quiet: open projects whose last activity (or, with none ever, their creation) is STALE_DAYS+ ago.
    stale = []
    for p in open_:
        since = _parse(last.get(p["id"])) or _parse(p.get("created_at"))
        if not since:
            continue
        idle = (now - since).days
        if idle >= STALE_DAYS:
            stale.append({**rows[p["id"]], "idle_days": idle})
    stale.sort(key=lambda r: (-r["idle_days"], r["name"].lower()))

    # Workload by due week: open projects and open follow-ups due each week, starting this one; anything
    # overdue sits in its own bucket in front.
    start = _week_start(today)
    weeks = [{"start": (start + timedelta(weeks=i)).isoformat(), "end": (start + timedelta(weeks=i, days=6)).isoformat(),
              "projects": [], "followups": 0} for i in range(WORKLOAD_WEEKS)]
    late = {"projects": [r for r in overdue], "followups": len(fu_overdue)}
    for r in open_rows:
        if r["due_date"] and r["due_state"] != "overdue":
            i = (date.fromisoformat(r["due_date"]) - start).days // 7
            if 0 <= i < WORKLOAD_WEEKS:
                weeks[i]["projects"].append(r)
    for f in fus:
        if not f["overdue"]:
            i = (date.fromisoformat(f["due_date"]) - start).days // 7
            if 0 <= i < WORKLOAD_WEEKS:
                weeks[i]["followups"] += 1
    for w in weeks:
        w["projects"].sort(key=by_due)
        w["value"] = _money_rows(w["projects"])
        w["projects"] = [{k: r[k] for k in ("id", "name", "due_date", "stage", "client_name", "color")} for r in w["projects"]]
    late["projects"] = [{k: r[k] for k in ("id", "name", "due_date", "stage", "client_name", "color")} for r in late["projects"]]

    return {
        "scope": scope, "client_id": client_id, "today": today.isoformat(), "stale_days": STALE_DAYS,
        "counts": {"projects": len(projects), "open": len(open_), "overdue": len(overdue), "due_week": len(due_week),
                   "due_month": len(due_month), "at_risk": len(at_risk), "followups_overdue": len(fu_overdue),
                   "followups_today": len(fu_today), "followups_due": len(fu_overdue) + len(fu_today), "stale": len(stale)},
        "overdue": overdue, "due_week": due_week, "due_month": due_month, "at_risk": at_risk,
        "followups": {"overdue": fu_overdue, "today": fu_today},
        "pipeline": pipeline, "by_client": by_client, "currency": currency, "stale": stale,
        "workload": {"overdue": late, "weeks": weeks},
    }


# ------------------------------------------------------------------ reports

def _stage_history(acts: list[dict[str, Any]], project_names: dict[int, str]) -> list[dict[str, Any]]:
    """Pipeline moves, oldest first."""
    out = []
    for a in acts:
        if a["kind"] != "stage":
            continue
        data = a.get("data") or {}
        out.append({"at": a["occurred_at"], "project_id": a.get("project_id"), "project": project_names.get(a.get("project_id")),
                    "from": data.get("from"), "to": data.get("to"), "summary": a["summary"], "by": data.get("by")})
    return sorted(out, key=lambda x: x["at"])


def _activity_rows(acts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Logged activities (not stage changes), newest first."""
    names = activities._Names()
    return [{k: v for k, v in activities.activity_view(a, names).items()
             if k in ("id", "kind", "kind_label", "occurred_at", "summary", "outcome", "people", "subject", "subject_name")}
            for a in acts if a["kind"] != "stage"]


def _followup_rows(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    names = activities._Names()
    views = [activities.followup_view(f, now, names) for f in rows]
    keep = ("id", "title", "notes", "due_date", "due_time", "status", "done_at", "overdue", "subject", "subject_name")
    return [{k: f.get(k) for k in keep} for f in sorted(views, key=lambda f: (f["status"] != "open", f["due_date"]))]


def _details(p: dict[str, Any], client_name: Optional[str]) -> dict[str, Any]:
    keep = ("id", "name", "category", "status", "priority", "health", "start_date", "due_date", "completed_date",
            "contract_value", "currency", "billing_type", "owner", "tags", "links", "description", "created_at")
    return {**{k: p.get(k) for k in keep}, "stage": crm.stage_of(p), "client_name": client_name}


def project_report(project_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """One project's summary: details, value, progress, contacts, stage history, activities, follow-ups."""
    p = db.get_project(project_id)
    if not p or db.is_general_project(p):
        raise ReportError("Project not found")
    now = _local_now(now)
    client = db.get_client(p["client_id"]) if p.get("client_id") else None
    acts = db.list_crm_activities([project_id], [], [], REPORT_LIMIT)
    return {
        "kind": "project", "generated_at": now.isoformat(timespec="minutes"), "name": p["name"],
        "project": _details(p, client["name"] if client else None),
        "schedule": crm.schedule(p, now.date()), "progress": crm.progress(project_id),
        "last_activity_at": activities.last_activity().get(project_id),
        "contacts": [{k: c.get(k) for k in ("name", "role", "email", "phone", "project_role")} for c in db.project_contacts(project_id)],
        "stage_history": _stage_history(acts, {p["id"]: p["name"]}),
        "activities": _activity_rows(acts),
        "followups": _followup_rows(db.list_followups(None, [project_id], [], []), now),
    }


def client_report(client_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """One client's summary: details, value, its projects (stage, due, value, progress), contacts, the
    stage history of its projects, and the activities and follow-ups on it, its projects and contacts."""
    client = db.get_client(client_id)
    if not client:
        raise ReportError("Client not found")
    now = _local_now(now)
    projects = db.client_projects(client_id)
    contacts = db.list_contacts(client_id)
    pids, cids = [p["id"] for p in projects], [c["id"] for c in contacts]
    acts = db.list_crm_activities(pids, [client_id], cids, REPORT_LIMIT)
    last = activities.last_activity()
    rows = []
    for p in projects:
        sched = crm.schedule(p, now.date())
        rows.append({**{k: p.get(k) for k in ("id", "name", "status", "health", "priority", "due_date", "completed_date", "contract_value", "currency", "billing_type")},
                     "stage": crm.stage_of(p), "due_state": sched["due_state"], "days_left": sched["days_left"],
                     "progress": crm.progress(p["id"])["percent"], "last_activity_at": last.get(p["id"])})
    keep = ("id", "name", "status", "industry", "website", "address", "notes", "created_at")
    return {
        "kind": "client", "generated_at": now.isoformat(timespec="minutes"), "name": client["name"],
        "client": {k: client.get(k) for k in keep},
        "value": clients.value(projects),
        "projects": rows,
        "contacts": [{k: c.get(k) for k in ("name", "role", "email", "phone", "preferred_method")} for c in contacts],
        "stage_history": _stage_history(acts, {p["id"]: p["name"] for p in projects}),
        "activities": _activity_rows(acts),
        "followups": _followup_rows(db.list_followups(None, pids, [client_id], cids), now),
    }


# ------------------------------------------------------------------ files

def _label(v: Optional[str]) -> str:
    return (v or "").replace("_", " ").capitalize()


def _people(a: dict[str, Any]) -> str:
    return ", ".join(x["name"] for x in a.get("people") or [])


def _fu_when(f: dict[str, Any]) -> str:
    return f["due_date"] + (f" {f['due_time']}" if f.get("due_time") else "")


def _fu_state(f: dict[str, Any]) -> str:
    return "Done" if f["status"] == "done" else "Overdue" if f.get("overdue") else "Open"


def _details_pairs(r: dict[str, Any]) -> list[tuple[str, str]]:
    """The "Details" section of either report, as (label, value) - empty values left out."""
    if r["kind"] == "project":
        p, prog = r["project"], r["progress"]
        pairs = [("Project", p["name"]), ("Client", p["client_name"] or ""), ("Side", _label(p["category"])),
                 ("Stage", activities.STAGE_LABELS.get(p["stage"], p["stage"])), ("Status", _label(p["status"])),
                 ("Priority", _label(p["priority"])), ("Health", _label(p["health"])), ("Owner", p["owner"] or ""),
                 ("Start date", p["start_date"] or ""), ("Due date", p["due_date"] or ""), ("Completed", p["completed_date"] or ""),
                 ("Contract value", money(p["contract_value"], p["currency"])), ("Billing", _label(p["billing_type"])),
                 ("Progress", f"{prog['percent']}%" if prog["percent"] is not None else ""),
                 ("To-dos done", f"{prog['todos_done']} of {prog['todos_total']}" if prog["todos_total"] else ""),
                 ("Phases done", f"{prog['phases_done']} of {prog['phases_total']}" if prog["phases_total"] else ""),
                 ("Tags", ", ".join(p["tags"] or [])), ("Last activity", _local_stamp(r["last_activity_at"]))]
        if r["schedule"]["due_state"] == "overdue":
            pairs.insert(10, ("Overdue by", f"{-r['schedule']['days_left']} days"))
    else:
        c = r["client"]
        pairs = [("Client", c["name"]), ("Status", _label(c["status"])), ("Industry", c["industry"] or ""),
                 ("Website", c["website"] or ""), ("Address", (c["address"] or "").replace("\n", ", ")),
                 ("Projects", str(len(r["projects"]))), ("Contacts", str(len(r["contacts"]))),
                 ("Total value", " + ".join(money(v["total"], v["currency"]) for v in r["value"])),
                 ("Active value", " + ".join(money(v["active"], v["currency"]) for v in r["value"] if v["active"]))]
    return [(k, v) for k, v in pairs if v]


def _md_cell(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).replace("|", "\\|").strip()


def _md_table(head: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(_md_cell(x) for x in row) + " |" for row in rows]
    return "\n".join(lines)


def _sections(r: dict[str, Any]) -> list[tuple[str, list[str], list[list[Any]]]]:
    """The tables both formats share: (title, header, rows)."""
    out = []
    if r["kind"] == "client":
        out.append(("Projects", ["Project", "Stage", "Status", "Health", "Due", "Value", "Progress"],
                    [[p["name"], activities.STAGE_LABELS.get(p["stage"], p["stage"]), _label(p["status"]), _label(p["health"]),
                      p["due_date"] or "", money(p["contract_value"], p["currency"]),
                      f"{p['progress']}%" if p["progress"] is not None else ""] for p in r["projects"]]))
    if r["contacts"]:
        if r["kind"] == "project":
            out.append(("Contacts", ["Name", "Role", "On the project", "Email", "Phone"],
                        [[c["name"], c["role"] or "", "Key contact" if c.get("project_role") == "key" else "Stakeholder", c["email"] or "", c["phone"] or ""] for c in r["contacts"]]))
        else:
            out.append(("Contacts", ["Name", "Role", "Email", "Phone", "Prefers"],
                        [[c["name"], c["role"] or "", c["email"] or "", c["phone"] or "", _label(c["preferred_method"])] for c in r["contacts"]]))
    hist_head = ["When", "Project", "From", "To"] if r["kind"] == "client" else ["When", "From", "To"]
    out.append(("Stage history", hist_head,
                [[_local_stamp(h["at"]), *([h["project"] or ""] if r["kind"] == "client" else []),
                  activities.STAGE_LABELS.get(h["from"], h["from"] or ""), activities.STAGE_LABELS.get(h["to"], h["to"] or "")] for h in r["stage_history"]]))
    out.append(("Activities", ["When", "Kind", "On", "Summary", "Outcome", "People"],
                [[_local_stamp(a["occurred_at"]), a["kind_label"], a["subject_name"] or "", a["summary"], a.get("outcome") or "", _people(a)] for a in r["activities"]]))
    out.append(("Follow-ups", ["Due", "Follow-up", "On", "State", "Done at"],
                [[_fu_when(f), f["title"], f["subject_name"] or "", _fu_state(f), _local_stamp(f.get("done_at"))] for f in r["followups"]]))
    return out


def to_markdown(r: dict[str, Any]) -> str:
    title = f"{'Client' if r['kind'] == 'client' else 'Project'} report: {r['name']}"
    parts = [f"# {title}", f"_Generated {r['generated_at'][:16].replace('T', ' ')} from Tushar's Workspace._", "## Details",
             "\n".join(f"- **{k}:** {_md_cell(v)}" for k, v in _details_pairs(r))]
    desc = (r.get("project") or {}).get("description") or (r.get("client") or {}).get("notes")
    if desc:
        parts += ["## Description" if r["kind"] == "project" else "## Notes", desc.strip()]
    links = (r.get("project") or {}).get("links") or []
    if links:
        parts += ["## Links", "\n".join(f"- [{_md_cell(x['label'])}]({x['url']})" for x in links)]
    for title_, head, rows in _sections(r):
        parts += [f"## {title_}", _md_table(head, rows) if rows else "_None yet._"]
    return "\n\n".join(parts) + "\n"


def _csv_cell(v: Any) -> Any:
    """Spreadsheets run a cell starting with = + - @ as a formula: keep text as text."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


def to_csv(r: dict[str, Any]) -> str:
    """One file, a block per section: a title row, a header row, the rows, then a blank line."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{'Client' if r['kind'] == 'client' else 'Project'} report", r["name"]])
    w.writerow(["Generated", r["generated_at"][:16].replace("T", " ")])
    w.writerow([])
    w.writerow(["Details"])
    w.writerow(["Field", "Value"])
    for k, v in _details_pairs(r):
        w.writerow([k, _csv_cell(v)])
    if r["kind"] == "project" and r["project"].get("contract_value") is not None:
        w.writerow(["Contract value (number)", r["project"]["contract_value"]])
    for v in r.get("value") or []:
        w.writerow([f"Total value {v['currency']} (number)", v["total"]])
        w.writerow([f"Active value {v['currency']} (number)", v["active"]])
    for title, head, rows in _sections(r):
        w.writerow([])
        w.writerow([title])
        w.writerow(head)
        for row in rows:
            w.writerow([_csv_cell(x) for x in row])
    return buf.getvalue()


def filename(r: dict[str, Any], fmt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-") or r["kind"]
    return f"{slug}-{r['kind']}-report-{r['generated_at'][:10]}.{fmt}"
