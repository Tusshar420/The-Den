"""
Read-only views for The Den 2.0 dashboard (static/den/): data already in the
database, shaped the way a design page shows it, so a page needs one request
instead of stitching many together in the browser.

- /den/activity: the Activity page - every run, approval, routing decision
  and scheduled run in the last N days, grouped by day, plus the 14-day
  done/failed chart and the stat cards.
"""

import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import accounts
import db
import router
from assistant import timeutil

MAX_EVENTS = 400
STEPS_PER_RUN = 5


def _ts(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return timeutil.parse_iso(iso)
    except (TypeError, ValueError):
        return None


def _model_label(model_id: Optional[str], lane: Optional[str]) -> str:
    if lane == "agent":
        return "Claude Code"
    m = router.get_model(model_id) if model_id else None
    return (m or {}).get("label") or model_id or (lane or "API")


def _dur(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    s = round(ms / 1000)
    if s < 1:
        return "<1s"
    if s < 60:
        return f"{s}s"
    m = round(s / 60)
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60}m"


def _money(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:.2f}"


def _short(text: Optional[str], n: int = 90) -> str:
    line = (text or "").strip().split("\n")[0]
    return line if len(line) <= n else line[: n - 1] + "…"


def activity(days: int = 14, now: Optional[datetime] = None, project_id: Optional[int] = None) -> dict[str, Any]:
    tz = timeutil.local_tz()       # looked up once: it reads the zone from disk each call
    now = (now or timeutil.now()).astimezone(tz)
    days = max(1, min(days, 90))
    since = (now - timedelta(days=days)).astimezone(timezone.utc).isoformat()
    aliases = {a["id"]: (a.get("alias") or a.get("label") or "") for a in accounts.public_list()}
    with db._lock:
        conn = db.get_conn()
        tasks = [db._row_to_dict(r) for r in conn.execute(
            "SELECT t.*, p.name AS project_name, COALESCE(r.title, t.title) AS chat_title FROM tasks t "
            "JOIN projects p ON p.id = t.project_id LEFT JOIN tasks r ON r.id = t.thread_id "
            "WHERE t.status IN ('running', 'done', 'error', 'needs_review') AND COALESCE(t.finished_at, t.started_at, t.created_at) >= ? "
            "ORDER BY COALESCE(t.finished_at, t.started_at, t.created_at) DESC LIMIT ?", (since, MAX_EVENTS)).fetchall()]
        ids = [t["id"] for t in tasks]
        events: dict[int, list[dict[str, Any]]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(f"SELECT task_id, kind, text, created_at FROM task_events WHERE task_id IN ({','.join('?' * len(chunk))}) "
                                  "ORDER BY id", chunk).fetchall():
                events.setdefault(r["task_id"], []).append(dict(r))
        approvals = [db._row_to_dict(r) for r in conn.execute(
            "SELECT a.*, t.project_id, t.title AS task_title, t.prompt AS task_prompt, p.name AS project_name FROM approvals a "
            "JOIN tasks t ON t.id = a.task_id JOIN projects p ON p.id = t.project_id WHERE a.created_at >= ? ORDER BY a.created_at DESC LIMIT 200",
            (since,)).fetchall()]
        routing = [db._row_to_dict(r) for r in conn.execute(
            "SELECT r.id, r.created_at, r.source, r.request, r.status, r.category, r.model_id, r.project_id, r.task_id, r.decided_by, "
            "p.name AS project_name FROM routing r LEFT JOIN projects p ON p.id = r.project_id "
            "WHERE r.created_at >= ? ORDER BY r.created_at DESC LIMIT 200", (since,)).fetchall()]

    out: list[dict[str, Any]] = []
    for t in tasks:
        failed = t["status"] == "error" or (t["status"] == "needs_review" and t.get("error_message"))
        k = "running" if t["status"] == "running" else "failed" if failed else "done"
        at = t.get("finished_at") or t.get("started_at") or t["created_at"]
        who = _model_label(t.get("model_id"), t.get("lane"))
        if t.get("lane") == "agent" and aliases.get(t.get("claude_account") or ""):
            who += " · " + aliases[t["claude_account"]]
        elif t.get("lane") != "agent":
            who += " · API"
        steps = [{"text": _short(e["text"], 120), "at": e["created_at"]} for e in events.get(t["id"], []) if e["kind"] in ("tool", "note")][-STEPS_PER_RUN:]
        if k == "done" and t.get("result_summary"):
            steps.append({"text": _short(t["result_summary"], 120), "at": t.get("finished_at")})
        if t["status"] == "needs_review" and not failed:
            steps.append({"text": "Marked for review", "at": t.get("finished_at")})
        out.append({"key": f"t{t['id']}", "k": k, "at": at, "task_id": t["id"], "root_id": t.get("thread_id") or t["id"],
                    "id": f"#{t.get('thread_id') or t['id']}", "title": t.get("chat_title") or t.get("title") or _short(t.get("prompt"), 60),
                    "project": t["project_name"], "project_id": t["project_id"], "who": who, "lane": t.get("lane"),
                    "dur": _dur(t.get("duration_ms")) if k != "running" else _dur(int((now - _ts(t["started_at"])).total_seconds() * 1000)) if t.get("started_at") else "—",
                    # Claude Code runs on the subscription: no spend (its cost_usd is only the API-price equivalent).
                    "cost": "$0.00" if t.get("lane") == "agent" else _money(t.get("cost_usd")),
                    "err": t.get("error_message") or "", "steps": steps, "status": t["status"]})
    for a in approvals:
        cmd = risk_command(a)
        decided = a["status"] != "pending"
        verb = {"approved": "Approved", "denied": "Denied", "timed_out": "Timed out"}.get(a["status"], "Asked to run")
        out.append({"key": f"a{a['id']}", "k": "approval", "at": a.get("decided_at") or a["created_at"], "task_id": a["task_id"], "root_id": a["task_id"],
                    "id": f"#{a['task_id']}", "title": f"{verb}: {_short(cmd, 70)}", "project": a["project_name"], "project_id": a["project_id"],
                    "who": "You" if a["status"] in ("approved", "denied") else "auto" if decided else "waiting on you",
                    "dur": _dur(int(((_ts(a["decided_at"]) or now) - _ts(a["created_at"])).total_seconds() * 1000)) if _ts(a["created_at"]) else "—",
                    "cost": "—", "err": a.get("note") or "", "steps": [{"text": f"{a['tool_name']}: {_short(cmd, 110)}", "at": a["created_at"]}]
                    + ([{"text": verb, "at": a["decided_at"]}] if decided else []), "status": a["status"]})
    for r in routing:
        sched = r.get("source") in ("schedule", "recipe")
        model = _model_label(r.get("model_id"), "agent" if r.get("model_id") == "claude-code" else None) if r.get("model_id") else "no model"
        out.append({"key": f"r{r['id']}", "k": "schedule" if sched else "routing", "at": r["created_at"], "task_id": r.get("task_id"), "root_id": r.get("task_id"),
                    "id": f"#{r['task_id']}" if r.get("task_id") else f"R-{r['id']}", "title": f"{_short(r['request'], 60)} → {model}",
                    "project": r.get("project_name") or "General", "project_id": r.get("project_id"), "who": "Scheduler" if sched else "Router",
                    "dur": "<1s", "cost": "—", "err": "",
                    "steps": [{"text": f"{(r.get('category') or 'request').replace('_', ' ')} · {r['status']} · decided by {r.get('decided_by') or 'Octopus'}", "at": r["created_at"]}],
                    "status": r["status"], "routing_id": r["id"]})
    if project_id is not None:
        out = [e for e in out if e.get("project_id") == project_id]
    out.sort(key=lambda e: e["at"] or "", reverse=True)
    out = out[:MAX_EVENTS]

    # day labels in local time
    today = now.date()
    for e in out:
        d = (_ts(e["at"]) or now).astimezone(tz)
        delta = (today - d.date()).days
        e["day"] = "Today" if delta == 0 else "Yesterday" if delta == 1 else d.strftime("%a %d %b")
        e["time"] = d.strftime("%H:%M")
        for s in e["steps"]:
            st = _ts(s.get("at"))
            s["t"] = st.astimezone(tz).strftime("%H:%M") if st else ""

    # 14-day chart and stat cards, from every finished task (replies included)
    chart_since = (now - timedelta(days=14)).astimezone(timezone.utc).isoformat()
    with db._lock:
        rows = [dict(r) for r in db.get_conn().execute(
            "SELECT status, error_message, finished_at, started_at, created_at, duration_ms, cost_usd, lane FROM tasks "
            "WHERE COALESCE(finished_at, created_at) >= ? AND status IN ('done', 'error', 'needs_review', 'running', 'queued', 'cancelled')",
            (chart_since,)).fetchall()]
    bars = []
    for i in range(13, -1, -1):
        day = today - timedelta(days=i)
        done = failed = 0
        for r in rows:
            f = _ts(r["finished_at"])
            if not f or f.astimezone(tz).date() != day:
                continue
            if r["status"] == "error" or (r["status"] == "needs_review" and r["error_message"]):
                failed += 1
            elif r["status"] in ("done", "needs_review"):
                done += 1
        bars.append({"date": day.isoformat(), "done": done, "failed": failed})
    started = lambda r: _ts(r["started_at"] or r["created_at"])  # noqa: E731
    today_n = sum(1 for r in rows if started(r) and started(r).astimezone(tz).date() == today)
    avg_day = sum(b["done"] + b["failed"] for b in bars[:-1]) / 13
    week = bars[-7:]
    w_done, w_failed = sum(b["done"] for b in week), sum(b["failed"] for b in week)
    week_since = now - timedelta(days=7)
    agent_ms = sorted(r["duration_ms"] for r in rows if r["lane"] == "agent" and r["duration_ms"] and (_ts(r["finished_at"]) or now) >= week_since)
    spend = sum(r["cost_usd"] or 0 for r in rows if r["lane"] != "agent" and (_ts(r["finished_at"]) or now) >= week_since)
    stats = {"today": today_n, "vs_avg": round(today_n - avg_day), "success_7d": round(w_done / (w_done + w_failed) * 100, 1) if w_done + w_failed else None,
             "failed_7d": w_failed, "avg_run_ms": agent_ms[len(agent_ms) // 2] if agent_ms else None, "spend_7d": round(spend, 2)}
    return {"events": out, "bars": bars, "stats": stats}


def usage(days: int = 30) -> dict[str, Any]:
    """The Token usage page in one go: every run in the window as a short row
    (id, when, project, model, account, tokens, minutes, API cost), spend per day,
    today's spend per hour, and the budget caps."""
    days = max(1, min(days, 90))
    tz = timeutil.local_tz()
    now = timeutil.now().astimezone(tz)
    start = (now - timedelta(days=days - 1)).date().isoformat()
    raw = db.token_usage(start, now.date().isoformat(), None, None, None, None, accounts.active_id())
    aliases = {a["id"]: (a.get("alias") or a.get("label") or "") for a in accounts.public_list()}
    runs = []
    for it in raw["items"]:
        if it.get("lane") in ("control", "background"):
            continue
        runs.append({"id": it.get("chat_id") or it["id"], "task_id": it["id"], "at": it["at"], "project_id": it.get("project_id"), "project": it.get("project_name") or "General",
                     "model_id": it.get("model_id"), "model": _model_label(it.get("model_id"), it.get("lane")), "lane": it.get("lane"),
                     "account": aliases.get(it.get("account") or "", "") if it.get("lane") == "agent" else "API",
                     "title": (it.get("title") or "")[:80], "tokens": it.get("total_tokens") or 0, "dur_ms": it.get("duration_ms") or 0,
                     "cost": 0.0 if it.get("lane") == "agent" else round(it.get("cost_usd") or 0, 6),
                     "estimate": round(it.get("cost_usd") or 0, 4) if it.get("lane") == "agent" else None})
    hours = [0.0] * 24
    today = now.date()
    for r in runs:
        t = _ts(r["at"])
        if t and t.astimezone(tz).date() == today:
            hours[t.astimezone(tz).hour] += r["cost"]
    return {"days": days, "runs": runs, "by_day": [{"date": d["date"], "spend": round(d["spend_usd"] or 0, 4)} for d in raw["by_day"]],
            "today_by_hour": [round(h, 4) for h in hours], "totals": raw["totals"],
            "caps": {"daily": db.get_setting("daily_budget_usd"), "monthly": db.get_setting("monthly_budget_usd"),
                     "warn": db.get_setting("budget_warn", True), "pause": db.get_setting("budget_pause", True)},
            "spend": {k: v for k, v in db.spend_summary().items() if k in ("today_usd", "month_usd")}}


def provider_health() -> dict[str, Any]:
    """AI Providers cards: per provider over the last 24 hours - median reply time, error
    rate, runs per hour (the little chart) and what was spent today."""
    import providers as prov
    tz = timeutil.local_tz()
    now = timeutil.now()
    since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    by_model = {m["id"]: m.get("provider") for m in router.registry().get("models", [])}
    with db._lock:
        rows = [dict(r) for r in db.get_conn().execute(
            "SELECT model_id, lane, status, error_message, duration_ms, cost_usd, started_at, finished_at FROM tasks "
            "WHERE COALESCE(finished_at, started_at) >= ? AND lane IN ('api', 'agent')", (since,)).fetchall()]
    today = now.astimezone(tz).date()
    out: dict[str, Any] = {}
    for pid in sorted({p for p in by_model.values() if p}):
        mine = [r for r in rows if by_model.get(r["model_id"]) == pid or (pid == "claude_code" and r["lane"] == "agent")]
        durs = sorted(r["duration_ms"] for r in mine if r["duration_ms"])
        failed = sum(1 for r in mine if r["status"] == "error" or (r["status"] == "needs_review" and r["error_message"]))
        hours = [0] * 24
        for r in mine:
            t = _ts(r["finished_at"] or r["started_at"])
            if t:
                h = int((now - t).total_seconds() // 3600)
                if 0 <= h < 24:
                    hours[23 - h] += 1
        spent = sum((r["cost_usd"] or 0) for r in mine if r["lane"] != "agent" and _ts(r["finished_at"]) and _ts(r["finished_at"]).astimezone(tz).date() == today)
        ready, why = prov.provider_ready(pid)
        out[pid] = {"runs": len(mine), "p50_ms": durs[len(durs) // 2] if durs else None, "error_rate": round(failed / len(mine) * 100, 1) if mine else 0.0,
                    "hours": hours, "spent_today": round(spent, 4), "ready": ready, "not_ready_reason": why}
    return out


def provider_check(provider_id: str) -> dict[str, Any]:
    """Run health check: one tiny request to the provider's first usable model, timed."""
    import time
    import providers as prov
    model = next((m for m in router.registry().get("models", []) if m.get("provider") == provider_id and router.lane_for(m) == "api"
                  and m.get("modality") != "image" and router.availability(m)[0]), None)
    if not model:
        ready, why = prov.provider_ready(provider_id)
        return {"ok": False, "error": why or "No usable model for this provider"}
    t0 = time.monotonic()
    try:
        out = prov.complete(model, [{"role": "user", "content": "Reply with the single word OK."}], None, 30)
    except prov.ProviderError as e:
        return {"ok": False, "model": model.get("label", model["id"]), "error": str(e)[:300], "ms": int((time.monotonic() - t0) * 1000)}
    db.log_usage("health-check", model["id"], out.input_tokens, out.output_tokens, prov.estimate_cost(model, out.input_tokens, out.output_tokens))
    return {"ok": True, "model": model.get("label", model["id"]), "ms": int((time.monotonic() - t0) * 1000), "reply": out.text.strip()[:40]}


def model_test(model_id: Optional[str] = None, provider: Optional[str] = None, model: Optional[str] = None,
               base_url: Optional[str] = None, key: Optional[str] = None) -> dict[str, Any]:
    """Send test prompt / Test connection: one tiny request to an existing model (model_id),
    or to a model not saved yet (provider + model + base_url + key)."""
    import time
    import api_keys
    import providers as prov
    t0 = time.monotonic()
    try:
        if model_id:
            cfg = router.get_model(model_id)
            if not cfg:
                return {"ok": False, "error": "Unknown model"}
            out = prov.complete(cfg, [{"role": "user", "content": "Reply with the single word OK."}], None, 30)
        elif provider == "openai_compat":
            out = prov.compat_complete({"id": "test", "label": "The server", "model": model, "base_url": base_url or ""},
                                       [{"role": "user", "content": "Reply with the single word OK."}], None, 30, key=key or "")
        elif provider in ("openai", "gemini"):
            if key:
                api_keys._check(provider, key)
                return {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "reply": "key accepted"}
            out = prov.complete({"id": model, "provider": provider, "model": model}, [{"role": "user", "content": "Reply with the single word OK."}], None, 30)
        else:
            return {"ok": False, "error": "Pick a provider"}
    except (prov.ProviderError, ValueError) as e:
        return {"ok": False, "error": str(e)[:300], "ms": int((time.monotonic() - t0) * 1000)}
    return {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "reply": (out.text or "").strip()[:40]}


def risk_command(a: dict[str, Any]) -> str:
    import risk
    return risk.command_of(a["tool_name"], a["tool_input"])


# ------------------------------------------------------------------ system health (Den 2.1 status page)

LANE_STALE_SECONDS = 180          # a lane loop that hasn't gone round in 3 minutes has stopped
LOG_FILE = Path.home() / "Library" / "Logs" / "TheDen" / "stderr.log"
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|(?i:(api[_-]?key|token|secret|password)\s*[=:]\s*)\S+)")


def _tail(path: Path, n: int = 30) -> list[str]:
    """The last lines of the service log, with anything that looks like a key or token blanked out."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []
    key = os.environ.get("API_KEY") or ""
    return [_SECRET_RE.sub("•••", l.replace(key, "•••") if key else l)[:300] for l in lines]


def system_health() -> dict[str, Any]:
    """Everything the Status page's health list shows: the server (it answered), the database, each worker
    lane, each Claude account, each AI provider and the disk - with an overall state."""
    import worker
    items: list[dict[str, Any]] = [{"id": "server", "label": "Den server", "ok": True, "detail": "Answering"}]
    try:
        with db._lock:
            db.get_conn().execute("SELECT 1").fetchone()
        size = Path(db.DB_PATH).stat().st_size
        items.append({"id": "db", "label": "Database", "ok": True, "detail": f"{size / 1e6:.1f} MB"})
    except Exception as e:
        items.append({"id": "db", "label": "Database", "ok": False, "detail": str(e)[:120]})
    now = time.time()
    for lane, label in (("agent", "Task runner (Claude Code)"), ("api", "API lane")):
        at = worker.LAST_TICK.get(lane)
        ok = bool(at) and now - at < LANE_STALE_SECONDS
        items.append({"id": "lane:" + lane, "label": label, "ok": ok,
                      "detail": ("Paused by you" if db.get_setting("queue_paused") else "Running") if ok else ("Not started" if not at else f"Stopped {int(now - at)}s ago")})
    try:
        for a in accounts.public_list():
            items.append({"id": "acct:" + str(a.get("id")), "label": "Claude · " + str(a.get("name") or a.get("id")), "ok": True,
                          "detail": "Active" if a.get("id") == accounts.active_id() else "Standby"})
    except Exception:
        pass
    for pid, h in provider_health().items():
        items.append({"id": "prov:" + pid, "label": pid.replace("_", " ").title(), "ok": bool(h["ready"]) and h["error_rate"] < 50,
                      "detail": (f"{h['error_rate']}% errors · 24h" if h["ready"] else (h["not_ready_reason"] or "Not set up"))})
    du = shutil.disk_usage(str(Path(db.DB_PATH).parent))
    free = du.free / du.total
    items.append({"id": "disk", "label": "Disk", "ok": free > 0.05, "detail": f"{du.free / 1e9:.0f} GB free of {du.total / 1e9:.0f} GB"})
    runner_down = not all(i["ok"] for i in items if i["id"].startswith("lane:"))
    held = [t["id"] for t in db.list_tasks(statuses=["queued", "rate_limited"])]
    return {"state": "down" if runner_down else "degraded" if not all(i["ok"] for i in items) else "ok",
            "since": None, "waiting": len(held), "items": items, "log": _tail(LOG_FILE)}


# ------------------------------------------------------------------ chat details (Home's chat panel)

class ChatTitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)


def _root(task_id: int) -> dict[str, Any]:
    t = db.get_task(task_id)
    if not t or t["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Chat not found")
    return db.get_task(t["thread_id"]) if t.get("thread_id") else t


def rename_chat(task_id: int, title: str) -> dict[str, Any]:
    """The owner's name for a chat; the auto-namer never overwrites it (it only renames title_by='text')."""
    root = _root(task_id)
    db.set_thread_title(root["id"], " ".join(title.split()), "user")
    return db.get_task(root["id"])


def export_chat(task_id: int) -> str:
    """The whole chat as Markdown: each message, what came back, and when."""
    root = _root(task_id)
    p = db.get_project(root["project_id"]) or {}
    lines = [f"# {root.get('title') or 'Chat #' + str(root['id'])}", "", f"Project: {p.get('name') or 'General'} · Chat #{root['id']}", ""]
    for t in db.get_thread(root["id"]):
        lines += [f"## You · {(t.get('created_at') or '')[:16].replace('T', ' ')}", "", (t.get("prompt") or "").strip(), ""]
        reply = t.get("result_summary") or t.get("error_message")
        if reply:
            who = "Octopus" + (f" ({t['model_id']})" if t.get("model_id") else "")
            lines += [f"## {who} · {(t.get('finished_at') or '')[:16].replace('T', ' ')}", "", reply.strip(), ""]
    return "\n".join(lines)


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/health")
    def den_health():
        return system_health()

    @r.post("/den/tasks/{task_id}/settled")
    def task_settled(task_id: int):
        """It's done: Octopus ended on a question, but nothing more is needed - the chat stops waiting on the owner."""
        root = _root(task_id)
        for t in db.get_thread(root["id"]):
            if t.get("awaiting_reply"):
                db.set_awaiting_reply(t["id"], False)
        return {"ok": True}

    @r.put("/den/chats/{task_id}/title")
    def chat_title(task_id: int, body: ChatTitleIn):
        return rename_chat(task_id, body.title)

    @r.get("/den/chats/{task_id}/export")
    def chat_export(task_id: int):
        return Response(content=export_chat(task_id), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="chat-{task_id}.md"'})

    @r.get("/den/providers/health")
    def den_provider_health():
        return provider_health()

    @r.post("/den/providers/{provider_id}/check")
    def den_provider_check(provider_id: str):
        return provider_check(provider_id)

    class ModelTestIn(BaseModel):
        model_id: Optional[str] = None
        provider: Optional[str] = None
        model: Optional[str] = None
        base_url: Optional[str] = None
        key: Optional[str] = None

    @r.post("/den/models/test")
    def den_model_test(body: ModelTestIn):
        return model_test(**body.model_dump())

    @r.get("/den/usage")
    def den_usage(days: int = 30):
        return usage(days)

    @r.get("/den/activity")
    def den_activity(days: int = 14, project_id: Optional[int] = None):
        return activity(days, project_id=project_id)

    return r
