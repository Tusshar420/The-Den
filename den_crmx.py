"""
CRM records of The Den 2.0 (static/den/DenCRMPlus.dc.html): Activities, Follow-ups, Table and
Reports & exports, across every project, client and contact at once.

Deals (projects with a stage, crm.py), contacts (clients.py), activities and follow-ups
(activities.py) already live elsewhere and are created / changed through their own endpoints.
This module only adds what the page needs on top of them:
- one log of every activity (/crm/feed answers for one project, client or contact only);
- the channel a follow-up goes out on (Email / Call / WhatsApp / Meeting), kept in its own
  table so the follow-ups table stays as activities.py knows it;
- when each won deal was won (its last move into a won stage), for the won-revenue chart;
- the four datasets as CSV, XLSX or PDF - written with the standard library, since the Den
  has no spreadsheet or PDF package;
- the "weekly report by email" switch. Only the choice is stored: nothing in the Den sends a
  scheduled report yet (see weekly()).

Nothing here sends mail, moves a deal or deletes anything.
"""

import csv
import io
import re
import zipfile
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

import activities
import crm
import crm_reports
import db
import mailer
from assistant import timeutil

CHANNELS = ("Email", "Call", "WhatsApp", "Meeting")
DATASETS = ("deals", "contacts", "activities", "followups")
FORMATS = ("csv", "xlsx", "pdf")
# The pipeline stages that count as won (crm.STAGES): signed and running, paused, or finished.
WON_STAGES = ("active", "on_hold", "done")
# How likely a deal at each stage is to close - the "PROB." column and the weighted value.
# The same weights the Pipeline board (DenCRM.dc.html) uses.
PROBABILITY = {"lead": 10, "proposal": 40, "negotiation": 70, "active": 100, "on_hold": 100, "done": 100, "lost": 0}
WEEKLY_KEY = "crmx_weekly_report"
WEEKLY_DEFAULT = {"on": False, "day": "mon", "time": "09:00"}
ACTIVITY_LIMIT = 1000

_ready = False


def _init() -> None:
    global _ready
    if _ready:
        return
    with db._lock:
        conn = db.get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS crmx_followup_channel (followup_id INTEGER PRIMARY KEY, channel TEXT NOT NULL)")
        conn.commit()
    _ready = True


# ------------------------------------------------------------------ activities and channels

def all_activities(limit: int = 300) -> list[dict[str, Any]]:
    """Every activity logged, on any project, client or contact, newest first - each as
    activities.activity_view shows it (what it's on, who was involved)."""
    limit = max(1, min(int(limit), ACTIVITY_LIMIT))
    with db._lock:
        conn = db.get_conn()
        rows = conn.execute("SELECT * FROM activities ORDER BY occurred_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        rows = db._activity_rows(conn, rows)
    names = activities._Names()
    return [activities.activity_view(a, names) for a in rows]


def channels() -> dict[int, str]:
    """{follow-up id: channel} for the follow-ups that still exist."""
    _init()
    with db._lock:
        rows = db.get_conn().execute("SELECT c.followup_id, c.channel FROM crmx_followup_channel c "
                                     "JOIN followups f ON f.id = c.followup_id").fetchall()
    return {r["followup_id"]: r["channel"] for r in rows}


def set_channel(followup_id: int, channel: str) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise HTTPException(status_code=400, detail=f"Channel must be one of: {', '.join(CHANNELS)}")
    if not db.get_followup(followup_id):
        raise HTTPException(status_code=404, detail="That follow-up is gone")
    _init()
    with db._lock:
        conn = db.get_conn()
        conn.execute("INSERT INTO crmx_followup_channel (followup_id, channel) VALUES (?, ?) "
                     "ON CONFLICT(followup_id) DO UPDATE SET channel = excluded.channel", (followup_id, channel))
        conn.commit()
    return {"id": followup_id, "channel": channel}


# ------------------------------------------------------------------ deals

def _deals() -> list[dict[str, Any]]:
    """The projects the CRM shows as deals: not General, not hidden."""
    return [p for p in db.list_projects() if not db.is_general_project(p) and not p.get("hidden")]


def _local_day(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        return timeutil.parse_iso(iso).astimezone(timeutil.local_tz()).date().isoformat()
    except ValueError:
        return None


def won_dates() -> dict[int, str]:
    """{project id: the local date it was won} for every deal in a won stage. Won = the last
    stage change into a won stage from one that isn't; a deal won before stage changes were
    logged falls back to its completed date, start date or the day it was added."""
    won = {p["id"]: p for p in _deals() if crm.stage_of(p) in WON_STAGES}
    if not won:
        return {}
    entered: dict[int, str] = {}
    with db._lock:
        conn = db.get_conn()
        rows = conn.execute(f"SELECT * FROM activities WHERE kind = 'stage' AND project_id IN ({','.join('?' * len(won))}) "
                            "ORDER BY occurred_at", list(won)).fetchall()
        rows = db._activity_rows(conn, rows)
    for a in rows:
        d = a.get("data") or {}
        if d.get("to") in WON_STAGES and d.get("from") not in WON_STAGES:
            entered[a["project_id"]] = _local_day(a["occurred_at"]) or entered.get(a["project_id"])
    return {pid: entered.get(pid) or p.get("completed_date") or p.get("start_date") or _local_day(p.get("created_at")) or ""
            for pid, p in won.items()}


def _next_steps() -> dict[int, str]:
    """{project id: the title of its soonest open follow-up}."""
    out: dict[int, str] = {}
    for f in db.list_followups(["open"]):          # soonest due first
        if f.get("project_id") and f["project_id"] not in out:
            out[f["project_id"]] = f["title"]
    return out


# ------------------------------------------------------------------ datasets

def _people(ids: list[int], contacts: dict[int, dict[str, Any]]) -> str:
    return ", ".join(contacts[i]["name"] for i in ids if i in contacts)


def dataset(name: str) -> tuple[str, list[str], list[list[Any]]]:
    """(title, header, rows) of one dataset - plain values (numbers stay numbers) for the files."""
    names = activities._Names()
    clients_by_id, contacts = names.clients, names.contacts
    if name == "deals":
        last = activities.last_activity()
        nxt = _next_steps()
        by_project: dict[int, str] = {}
        for c in sorted(contacts.values(), key=lambda c: c["name"].lower()):
            for pid in c.get("project_ids") or []:
                by_project.setdefault(pid, c["name"])
        rows = []
        for p in sorted(_deals(), key=lambda p: p["name"].lower()):
            stage = crm.stage_of(p)
            rows.append([p["id"], p["name"], (clients_by_id.get(p.get("client_id")) or {}).get("name") or "",
                         by_project.get(p["id"], ""), activities.STAGE_LABELS.get(stage, stage), p.get("contract_value"),
                         p.get("currency") or "USD", PROBABILITY.get(stage, 0), p.get("owner") or "", nxt.get(p["id"], ""),
                         p.get("due_date") or "", crm_reports._local_stamp(last.get(p["id"]))])
        return "Deals", ["ID", "Deal", "Client", "Contact", "Stage", "Value", "Currency", "Probability %", "Owner",
                         "Next step", "Due date", "Last activity"], rows
    if name == "contacts":
        projects = names.projects
        rows = [[c["name"], c.get("role") or "", c.get("email") or "", c.get("phone") or "",
                 (clients_by_id.get(c.get("client_id")) or {}).get("name") or "",
                 ", ".join(projects[i]["name"] for i in c.get("project_ids") or [] if i in projects),
                 c.get("preferred_method") or ""] for c in sorted(contacts.values(), key=lambda c: c["name"].lower())]
        return "Contacts", ["Name", "Role", "Email", "Phone", "Client", "Projects", "Preferred"], rows
    if name == "activities":
        rows = [[crm_reports._local_stamp(a["occurred_at"]), a["kind_label"], a.get("subject_name") or "",
                 _people(a.get("contact_ids") or [], contacts), a.get("summary") or "", a.get("outcome") or ""]
                for a in all_activities(ACTIVITY_LIMIT)]
        return "Activities", ["When", "Type", "On", "People", "Summary", "Outcome"], rows
    if name == "followups":
        ch = channels()
        rows = []
        for f in db.list_followups(None):
            v = activities.followup_view(f, names=names)
            rows.append([f["due_date"], f.get("due_time") or "", f["title"], ch.get(f["id"], ""), v.get("subject_name") or "",
                         "Done" if f["status"] == "done" else ("Overdue" if v["overdue"] else "Open"),
                         crm_reports._local_stamp(f.get("done_at")), f.get("notes") or ""])
        return "Follow-ups", ["Due", "Time", "Follow-up", "Channel", "On", "Status", "Done at", "Notes"], rows
    raise HTTPException(status_code=404, detail=f"No dataset '{name}' - one of: {', '.join(DATASETS)}")


# ------------------------------------------------------------------ file formats

def to_csv(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        # A leading = + - @ would make a spreadsheet run the cell as a formula.
        w.writerow(["'" + v if isinstance(v, str) and v[:1] in "=+-@" else ("" if v is None else v) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")      # the BOM makes Excel read UTF-8


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def to_xlsx(title: str, header: list[str], rows: list[list[Any]]) -> bytes:
    """A one-sheet .xlsx (bold header row, text as inline strings) - the smallest file Excel,
    Numbers and Google Sheets all open."""
    def cell(ref: str, v: Any, bold: bool = False) -> str:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f'<c r="{ref}"><v>{v}</v></c>'
        text = escape(_XML_BAD.sub("", "" if v is None else str(v)))
        style = ' s="1"' if bold else ""
        return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'

    lines = []
    for n, r in enumerate([header, *rows], start=1):
        lines.append(f'<row r="{n}">' + "".join(cell(f"{_col(i)}{n}", v, n == 1) for i, v in enumerate(r)) + "</row>")
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             f'<sheetData>{"".join(lines)}</sheetData></worksheet>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(title[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '</styleSheet>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


# PDF: A4 landscape, the built-in Helvetica (so no font is embedded). Helvetica only has the
# Western European letters (cp1252); anything else prints as "?".
_PAGE_W, _PAGE_H, _MARGIN = 842, 595, 36
_SIZE, _LEAD = 8, 13
_PDF_SWAP = {"→": "->", "←": "<-", "₹": "Rs "}


def _plain(v: Any) -> str:
    """A cell as one line of text that Helvetica can print."""
    s = "" if v is None else (f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))
    return re.sub(r"\s+", " ", "".join(_PDF_SWAP.get(ch, ch) for ch in s)).strip()


def _pdf_text(s: str) -> bytes:
    b = s.encode("cp1252", errors="replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _fit(s: str, width: float) -> str:
    """Cut a cell to its column (Helvetica averages about half the font size per character)."""
    most = max(1, int(width / (_SIZE * 0.52)))
    return s if len(s) <= most else s[:most - 3].rstrip() + "..."


def to_pdf(title: str, header: list[str], rows: list[list[Any]], subtitle: str = "") -> bytes:
    """A plain table as a PDF: title, a bold header row repeated on every page, one line per row."""
    usable = _PAGE_W - 2 * _MARGIN
    sample = [header, *rows[:200]]
    weight = [min(40, max(4, max(len(str(r[i]) if i < len(r) and r[i] is not None else "") for r in sample))) for i in range(len(header))]
    widths = [usable * w / sum(weight) for w in weight]
    per_page = int((_PAGE_H - 2 * _MARGIN - 40) / _LEAD) - 1
    chunks = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]

    def line(cells: list[Any], y: float, font: str) -> bytes:
        out, x = [], _MARGIN
        for i, w in enumerate(widths):
            v = cells[i] if i < len(cells) else ""
            out.append(b"BT /%s %d Tf %.1f %.1f Td (%s) Tj ET" % (font.encode(), _SIZE, x, y, _pdf_text(_fit(_plain(v), w - 6))))
            x += w
        return b"\n".join(out)

    streams = []
    for n, chunk in enumerate(chunks, start=1):
        y = _PAGE_H - _MARGIN - 14
        parts = [b"BT /F2 14 Tf %d %.1f Td (%s) Tj ET" % (_MARGIN, y, _pdf_text(_plain(title))),
                 b"BT /F1 8 Tf %d %.1f Td (%s) Tj ET" % (_MARGIN, y - 14, _pdf_text(_plain(f"{subtitle} - page {n} of {len(chunks)}".strip(" -"))))]
        y -= 40
        parts.append(line(header, y, "F2"))
        parts.append(b"0.8 G %d %.1f m %d %.1f l S 0 G" % (_MARGIN, y - 4, _PAGE_W - _MARGIN, y - 4))
        for r in chunk:
            y -= _LEAD
            parts.append(line(r, y, "F1"))
        if not rows:
            parts.append(line(["Nothing here yet"], y - _LEAD, "F1"))
        streams.append(b"\n".join(parts))

    # objects: 1 catalog, 2 pages, 3-4 fonts, then a page and its content per page
    objs: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                         b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    kids = []
    for s in streams:
        page_no, content_no = len(objs) + 1, len(objs) + 2
        kids.append(page_no)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                    b"/Contents %d 0 R >>" % (_PAGE_W, _PAGE_H, content_no))
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(s) + 1, s))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


MEDIA = {"csv": "text/csv; charset=utf-8", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf"}


def export(name: str, fmt: str) -> Response:
    if fmt not in FORMATS:
        raise HTTPException(status_code=400, detail=f"format must be one of: {', '.join(FORMATS)}")
    title, header, rows = dataset(name)
    if fmt == "csv":
        body = to_csv(header, rows)
    elif fmt == "xlsx":
        body = to_xlsx(title, header, rows)
    else:
        if name == "deals":                      # money reads better as "USD 4,800" on paper
            rows = [[*r[:5], crm_reports.money(r[5], r[6]), *r[6:]] for r in rows]
        body = to_pdf(f"The Den - {title}", header, rows, f"{len(rows)} rows - {timeutil.now().strftime('%d %b %Y %H:%M')}")
    stamp = timeutil.now().date().isoformat()
    return Response(content=body, media_type=MEDIA[fmt],
                    headers={"Content-Disposition": f'attachment; filename="den-{name}-{stamp}.{fmt}"'})


# ------------------------------------------------------------------ weekly report

def weekly() -> dict[str, Any]:
    """The weekly-report-by-email choice, who it would go to, and "scheduled": False - the Den
    has no scheduled report yet, so turning it on only remembers the choice."""
    pref = {**WEEKLY_DEFAULT, **(db.get_setting(WEEKLY_KEY) or {})}
    return {**pref, "to": mailer.recipient(), "scheduled": False}


def set_weekly(on: bool) -> dict[str, Any]:
    db.set_setting(WEEKLY_KEY, {**WEEKLY_DEFAULT, **(db.get_setting(WEEKLY_KEY) or {}), "on": bool(on)})
    return weekly()


# ------------------------------------------------------------------ routes

class ChannelIn(BaseModel):
    channel: str


class WeeklyIn(BaseModel):
    on: bool


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/crmx/activities")
    def list_activities(limit: int = 300):
        return all_activities(limit)

    @r.get("/den/crmx/channels")
    def list_channels():
        return {str(k): v for k, v in channels().items()}

    @r.put("/den/crmx/followups/{followup_id}/channel")
    def put_channel(followup_id: int, body: ChannelIn):
        return set_channel(followup_id, body.channel)

    @r.get("/den/crmx/won")
    def won():
        return {str(k): v for k, v in won_dates().items()}

    @r.get("/den/crmx/export/{name}")
    def export_dataset(name: str, format: str = "csv"):
        return export(name, format)

    @r.get("/den/crmx/weekly")
    def get_weekly():
        return weekly()

    @r.put("/den/crmx/weekly")
    def put_weekly(body: WeeklyIn):
        return set_weekly(body.on)

    return r
