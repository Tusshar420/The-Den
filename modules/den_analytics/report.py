"""
Builds the Analytics report from the Den's own data, read-only.

Everything here only reads: it opens a second, read-only connection to the Den's database, so it
can never change or slow down the work going on. It also reads the Claude accounts list and the
modules on disk. The report is a plain dict the dashboard draws; `build(days)` is the whole thing.

What it answers:
  - overview   : tasks run, finished, failed; emails sent; cost and tokens; accounts and providers in use
  - by_model   : which AI models did the work, and how well
  - by_account : which Claude accounts the work ran on
  - providers  : which AI providers and outside accounts (Google...) are connected and used
  - emails     : emails sent / read, by day
  - tools      : which assistant tools were used most
  - features   : which kinds of work (agents and lanes) were used most
  - modules    : which optional modules are installed and on
  - trend      : tasks per day, for a small chart
  - installs   : given-away Dens checking in (only on the owner's Den)
"""

import json
import sqlite3
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import db

MODULES_DIR = Path(__file__).resolve().parents[1]


def _ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _num(v: Any) -> float:
    return round(float(v), 4) if isinstance(v, (int, float)) else 0.0


def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _day_series(conn: sqlite3.Connection, sql: str, args: tuple, days: int) -> list[dict[str, Any]]:
    """Fill in every day in the window, even days with nothing, so a chart has no gaps."""
    got = {r["day"]: r for r in _rows(conn, sql, args)}
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        r = got.get(d, {})
        out.append({"day": d, "count": _int(r.get("count")), "cost": _num(r.get("cost"))})
    return out


def _account_labels() -> dict[str, dict[str, Any]]:
    try:
        import accounts
        return {a["id"]: a for a in accounts.public_list()}
    except Exception:
        return {}


def _installs() -> dict[str, Any]:
    raw = db.get_setting("module_state:den_updates:installs", [])
    rows = raw if isinstance(raw, list) else []
    now = datetime.now(timezone.utc)
    active = 0
    versions: dict[str, int] = {}
    clean = []
    for i in rows:
        last = i.get("last_seen")
        recent = False
        if last:
            try:
                recent = (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).days < 14
            except ValueError:
                recent = False
        active += 1 if recent else 0
        v = i.get("version") or "—"
        versions[v] = versions.get(v, 0) + 1
        clean.append({"name": i.get("name"), "version": v, "last_seen": last, "recent": recent})
    return {"total": len(rows), "active": active,
            "by_version": [{"version": k, "count": v} for k, v in sorted(versions.items(), reverse=True)],
            "list": sorted(clean, key=lambda x: (x.get("last_seen") or ""), reverse=True)}


def _modules() -> list[dict[str, Any]]:
    switches = db.get_setting("modules", {})
    switches = switches if isinstance(switches, dict) else {}
    out = []
    for folder in sorted(MODULES_DIR.iterdir()):
        mf = folder / "module.json"
        if not mf.is_file():
            continue
        try:
            man = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        name = man.get("name", folder.name)
        own = switches.get(name, {})
        enabled = own.get("enabled", man.get("enabled", True)) if isinstance(own, dict) else man.get("enabled", True)
        out.append({"name": name, "version": man.get("version", ""), "description": man.get("description", ""),
                    "enabled": bool(enabled)})
    return out


def build(days: int = 30) -> dict[str, Any]:
    days = max(1, min(int(30 if days is None else days), 365))
    since = _since(days)
    conn = _ro_conn()
    try:
        done = _rows(conn, "SELECT COALESCE(status,'?') s, COUNT(*) n, SUM(cost_usd) cost, "
                           "SUM(input_tokens) it, SUM(output_tokens) ot FROM tasks WHERE created_at>=? GROUP BY s", (since,))
        by_status = {r["s"]: r for r in done}
        total_tasks = sum(_int(r["n"]) for r in done)
        finished = _int(by_status.get("done", {}).get("n"))
        failed = sum(_int(by_status.get(s, {}).get("n")) for s in ("error", "needs_review"))
        cost = _num(sum(_num(r["cost"]) for r in done))
        tokens = sum(_int(r["it"]) + _int(r["ot"]) for r in done)

        emails = _rows(conn, "SELECT COALESCE(action,'?') a, COUNT(*) n, SUM(error IS NOT NULL AND error<>'') fail "
                             "FROM assistant_activity WHERE tool='email' AND created_at>=? GROUP BY a", (since,))
        emails_sent = sum(_int(r["n"]) for r in emails if r["a"] in ("send", "reply", "send_email"))

        labels = _account_labels()
        by_account = _rows(conn, "SELECT COALESCE(claude_account,'(default)') k, COUNT(*) n, SUM(cost_usd) cost, "
                                 "SUM(status='done') done FROM tasks WHERE created_at>=? GROUP BY k ORDER BY n DESC", (since,))
        for r in by_account:
            meta = labels.get(r["k"], {})
            r["label"] = meta.get("label") or meta.get("alias") or meta.get("email") or r["k"]
            r["cost"] = _num(r["cost"])

        providers_conn = _rows(conn, "SELECT provider, account, status FROM connections ORDER BY provider")
        model_use = _rows(conn, "SELECT COALESCE(model_id,'unknown') k, COUNT(*) n, SUM(cost_usd) cost "
                                "FROM usage_log WHERE created_at>=? GROUP BY k ORDER BY n DESC", (since,))

        summary = db.usage_summary(days)
        by_model = [{"model": r["key"], "tasks": _int(r["tasks"]), "done": _int(r["done"]),
                     "failed": _int(r["failed"]), "cost": _num(r["cost_usd"]),
                     "tokens": _int(r["input_tokens"]) + _int(r["output_tokens"]),
                     "avg_seconds": round(_num(r["avg_duration_ms"]) / 1000, 1) if r["avg_duration_ms"] else None}
                    for r in summary["by_model"]]

        tools = _rows(conn, "SELECT COALESCE(tool,'?') tool, COALESCE(action,'?') action, COUNT(*) n "
                            "FROM assistant_activity WHERE created_at>=? GROUP BY tool, action ORDER BY n DESC LIMIT 25", (since,))
        tool_totals: "OrderedDict[str, int]" = OrderedDict()
        for r in tools:
            tool_totals[r["tool"]] = tool_totals.get(r["tool"], 0) + _int(r["n"])

        features = _rows(conn, "SELECT COALESCE(agent_id,'(router)') k, COUNT(*) n FROM tasks "
                               "WHERE created_at>=? GROUP BY k ORDER BY n DESC", (since,))
        lanes = _rows(conn, "SELECT COALESCE(lane,'?') k, COUNT(*) n FROM tasks WHERE created_at>=? GROUP BY k ORDER BY n DESC", (since,))

        trend = _day_series(conn, "SELECT substr(created_at,1,10) day, COUNT(*) count, SUM(cost_usd) cost "
                                  "FROM tasks WHERE created_at>=? GROUP BY day", (since,), days)
        email_trend = _day_series(conn, "SELECT substr(created_at,1,10) day, COUNT(*) count, 0 cost "
                                        "FROM assistant_activity WHERE tool='email' AND created_at>=? GROUP BY day", (since,), days)

        return {
            "days": days, "generated_at": datetime.now(timezone.utc).isoformat(),
            "overview": {
                "tasks": total_tasks, "done": finished, "failed": failed,
                "success_rate": round(finished / total_tasks * 100) if total_tasks else None,
                "emails_sent": emails_sent, "cost_usd": cost, "tokens": tokens,
                "accounts_used": len([r for r in by_account if r["k"] != "(default)"]),
                "providers_connected": len([p for p in providers_conn if p["status"] == "connected"]),
                "modules_on": len([m for m in _modules() if m["enabled"]]),
                "installs_active": _installs()["active"],
            },
            "by_model": by_model,
            "model_calls": [{"model": r["k"], "calls": _int(r["n"]), "cost": _num(r["cost"])} for r in model_use],
            "by_account": [{"account": r["k"], "label": r["label"], "tasks": _int(r["n"]),
                            "done": _int(r["done"]), "cost": r["cost"]} for r in by_account],
            "providers": [{"provider": p["provider"], "account": p["account"], "status": p["status"]} for p in providers_conn],
            "emails": {"sent": emails_sent, "by_action": [{"action": r["a"], "count": _int(r["n"]),
                        "failed": _int(r["fail"])} for r in emails], "trend": email_trend},
            "tools": [{"tool": t, "count": n} for t, n in tool_totals.items()],
            "tool_actions": [{"tool": r["tool"], "action": r["action"], "count": _int(r["n"])} for r in tools],
            "features": [{"agent": r["k"], "count": _int(r["n"])} for r in features],
            "lanes": [{"lane": r["k"], "count": _int(r["n"])} for r in lanes],
            "modules": _modules(),
            "trend": trend,
            "installs": _installs(),
        }
    finally:
        conn.close()
