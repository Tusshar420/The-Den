"""
Product-usage analytics: how the Den is actually used, with no content at all.

The dashboard (den.js) quietly notes which section is open and for how long (active seconds only,
while the window is in front), and how many times the Den was opened (sessions). It sends those
counts here every so often. This module keeps daily totals in its own small table and hands them
back for the Analytics page. It never sees or stores anything a person wrote - no chats, no project
or client names, no email text. Just section names (like "projects", "crm") and seconds.

Two commands (POST /modules/commands/<name>, {"arg": "<json>"}):
  den-usage-track  {"events":[{"section","opens","seconds"}], "new_session":bool}  the dashboard calls this
  den-usage        {"days":30}                                                     the report for the owner

Its own table, made on load (no restart, no change to db.py):
  den_usage_daily(day TEXT, section TEXT, opens INTEGER, seconds INTEGER, PRIMARY KEY(day, section))
Sessions are kept as the reserved section "__session__" (opens = number of times opened that day).

A module, so a change here needs POST /modules/den_usage/reload, never a restart.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import db

SESSION = "__session__"
MAX_EVENTS = 200
MAX_SECONDS_PER_EVENT = 24 * 3600
MAX_OPENS_PER_EVENT = 10000
_SECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,59}$")

# The main sections, for the "taken up / not yet tried" list and friendly names. Unknown ids still
# count; they just show their id tidied up.
LABELS = {
    "home": "Den Home", "projects": "Projects", "clients": "Clients", "contacts": "Contacts",
    "team": "Team", "crm": "CRM", "crmx": "CRM records", "recipes": "Recipes & schedules",
    "review": "Review runs", "activity": "Activity", "email": "Email", "notes": "Notes",
    "connections": "Connections", "devices": "Smart devices", "briefing": "Morning briefing",
    "calendar": "Calendar", "usage": "Token usage", "accounts": "AI accounts", "providers": "AI providers",
    "routing": "Routing", "voice": "Voice", "settings": "Settings", "bin": "Recycle bin",
    "messages": "Message center", "about": "About", "setup": "Setup guide", "guides": "Guides & tours",
    "updates": "Updates", "analytics": "Analytics",
}
CATALOG = list(LABELS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def label(section: str) -> str:
    return LABELS.get(section, section.replace("_", " ").replace("-", " ").strip().title() or section)


def _arg(text: str) -> dict[str, Any]:
    if not (text or "").strip():
        return {}
    try:
        v = json.loads(text)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        self._ensure_table()
        ctx.register_command("den-usage-track", self.track, "Record which section is open and for how long")
        ctx.register_command("den-usage", self.report, "Product-usage report for the owner")

    def _ensure_table(self):
        with db._lock:
            conn = db.get_conn()
            conn.execute("CREATE TABLE IF NOT EXISTS den_usage_daily ("
                         "day TEXT NOT NULL, section TEXT NOT NULL, opens INTEGER NOT NULL DEFAULT 0, "
                         "seconds INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, section))")
            conn.commit()

    # ------------------------------------------------------------ record
    def track(self, arg_text: str) -> dict[str, Any]:
        a = _arg(arg_text)
        events = a.get("events") if isinstance(a.get("events"), list) else []
        day = _today()
        rows: list[tuple] = []
        for e in events[:MAX_EVENTS]:
            if not isinstance(e, dict):
                continue
            section = e.get("section")
            if not (isinstance(section, str) and _SECTION_RE.match(section)) or section == SESSION:
                continue
            opens = e.get("opens") if isinstance(e.get("opens"), int) else 0
            seconds = e.get("seconds") if isinstance(e.get("seconds"), int) else 0
            opens = max(0, min(opens, MAX_OPENS_PER_EVENT))
            seconds = max(0, min(seconds, MAX_SECONDS_PER_EVENT))
            if opens or seconds:
                rows.append((day, section, opens, seconds))
        if a.get("new_session"):
            rows.append((day, SESSION, 1, 0))
        if rows:
            with db._lock:
                conn = db.get_conn()
                conn.executemany(
                    "INSERT INTO den_usage_daily (day, section, opens, seconds) VALUES (?,?,?,?) "
                    "ON CONFLICT(day, section) DO UPDATE SET opens = opens + excluded.opens, "
                    "seconds = seconds + excluded.seconds", rows)
                conn.commit()
        return {"ok": True, "recorded": len(rows)}

    # ------------------------------------------------------------ report
    def report(self, arg_text: str) -> dict[str, Any]:
        days = _arg(arg_text).get("days", 30)
        try:
            days = max(1, min(int(days), 365))
        except (TypeError, ValueError):
            days = 30
        since = (_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with db._lock:
            conn = db.get_conn()
            rows = conn.execute("SELECT day, section, opens, seconds FROM den_usage_daily WHERE day >= ?",
                                (since,)).fetchall()

        by_section: dict[str, dict[str, int]] = {}
        per_day: dict[str, dict[str, int]] = {}
        sessions = 0
        for r in rows:
            day, section = r["day"], r["section"]
            d = per_day.setdefault(day, {"opens": 0, "seconds": 0, "sessions": 0})
            if section == SESSION:
                sessions += r["opens"]
                d["sessions"] += r["opens"]
                continue
            b = by_section.setdefault(section, {"opens": 0, "seconds": 0})
            b["opens"] += r["opens"]
            b["seconds"] += r["seconds"]
            d["opens"] += r["opens"]
            d["seconds"] += r["seconds"]

        total_opens = sum(b["opens"] for b in by_section.values())
        total_seconds = sum(b["seconds"] for b in by_section.values())
        sections = sorted(
            ({"section": s, "label": label(s), "opens": b["opens"], "seconds": b["seconds"],
              "minutes": round(b["seconds"] / 60, 1),
              "share_time": round(b["seconds"] / total_seconds * 100) if total_seconds else 0,
              "share_opens": round(b["opens"] / total_opens * 100) if total_opens else 0}
             for s, b in by_section.items()),
            key=lambda x: (x["seconds"], x["opens"]), reverse=True)

        today = _now().date()
        trend = []
        for i in range(days - 1, -1, -1):
            dstr = (today - timedelta(days=i)).isoformat()
            d = per_day.get(dstr, {})
            trend.append({"day": dstr, "opens": d.get("opens", 0),
                          "minutes": round(d.get("seconds", 0) / 60, 1), "sessions": d.get("sessions", 0)})

        active_days = sum(1 for d in per_day.values() if d.get("opens") or d.get("seconds"))
        used = {s["section"] for s in sections if s["opens"]}
        adoption = [{"section": s, "label": label(s), "used": s in used} for s in CATALOG]
        last_active = max((d for d in per_day), default=None)

        return {
            "days": days, "generated_at": _now().isoformat(),
            "totals": {
                "opens": total_opens, "minutes": round(total_seconds / 60, 1),
                "hours": round(total_seconds / 3600, 1), "sessions": sessions,
                "active_days": active_days, "last_active": last_active,
                "avg_session_min": round(total_seconds / 60 / sessions, 1) if sessions else 0,
                "sections_used": len(used), "sections_total": len(CATALOG),
            },
            "by_section": sections,
            "trend": trend,
            "adoption": adoption,
            "top": sections[0]["label"] if sections else None,
        }
