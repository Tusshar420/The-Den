"""
Recipes & schedules for The Den 2.0: a recipe is a trigger plus steps.

Triggers
  schedule   chosen weekdays at a time (local), once per day it fits
  event      something that happens - EVENTS below, checked by the scheduler
  manual     only when you press Run (or from the command palette)

Steps run top to bottom; each one's output is handed to the next as context.
  ask        an answer-only model does what the step says (with the context)
  run        "Project · prompt" - queued as a task in that project ("…backlog…"
             queues the project's to-dos instead)
  email      "to · subject" - the context goes out as the body
  notify     a push to your phone with the step's text and the context
  approve    stops until you Approve / Deny it like any other approval
  cond       "If…": the run stops here when the condition doesn't hold

The recipes and schedules made before this (db recipes / schedules) are shown
too, as one-step "run" recipes; editing one that stays that simple keeps it a
plain schedule, so nothing about how they run changes. Anything with more
steps, or a trigger they can't express, becomes a recipe here.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db
from assistant import timeutil

log = logging.getLogger("den.recipes")

STEP_KINDS = ("ask", "run", "email", "notify", "approve", "cond")
TRIGGERS = ("schedule", "event", "manual")
EVENTS = ("New email from a client", "A task fails", "Approval waits 10 minutes", "Usage passes 80%", "A project goes idle")
MAX_STEPS = 12
WAIT_APPROVAL_HOURS = 24
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ------------------------------------------------------------------ storage

def _ensure() -> None:
    with db._lock:
        conn = db.get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS den_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            trig TEXT NOT NULL DEFAULT 'manual',
            days TEXT NOT NULL DEFAULT '[0,0,0,0,0,0,0]',
            time TEXT NOT NULL DEFAULT '09:00',
            event TEXT NOT NULL DEFAULT '',
            steps TEXT NOT NULL DEFAULT '[]',
            icon TEXT,
            last_run_at TEXT,
            fired_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS den_recipe_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            -- running | waiting (on an approval) | done | stopped (an If… didn't hold) | failed | denied
            status TEXT NOT NULL DEFAULT 'running',
            step INTEGER NOT NULL DEFAULT 0,
            context TEXT NOT NULL DEFAULT '',
            log TEXT NOT NULL DEFAULT '[]',
            approval_id INTEGER,
            trigger_text TEXT
        )""")
        conn.commit()


def _row(r) -> dict[str, Any]:
    d = dict(r)
    d["days"] = json.loads(d.get("days") or "[0,0,0,0,0,0,0]")
    d["steps"] = json.loads(d.get("steps") or "[]")
    d["enabled"] = bool(d["enabled"])
    return d


def get(recipe_id: int) -> Optional[dict[str, Any]]:
    _ensure()
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM den_recipes WHERE id = ?", (recipe_id,)).fetchone()
    return _row(r) if r else None


def list_own() -> list[dict[str, Any]]:
    _ensure()
    with db._lock:
        return [_row(r) for r in db.get_conn().execute("SELECT * FROM den_recipes ORDER BY id").fetchall()]


# ------------------------------------------------------------------ the combined list

def _project_label(pid: Optional[int]) -> str:
    p = db.get_project(pid) if pid else None
    return p["name"] if p else "every project"


def _legacy_schedule(sc: dict[str, Any]) -> dict[str, Any]:
    cad = sc["cadence"]
    days = [1] * 7 if cad == "daily" else [1, 1, 1, 1, 1, 0, 0] if cad == "weekdays" else \
        [1 if i == (sc.get("weekday") or 0) else 0 for i in range(7)] if cad == "weekly" else [0] * 7
    return {"id": f"s{sc['id']}", "source": "schedule", "name": sc.get("label") or sc["prompt"][:50], "enabled": bool(sc["enabled"]),
            "trig": "schedule" if cad != "once" else "manual", "days": days, "time": sc.get("time_of_day") or "09:00", "event": "",
            "steps": [["run", f"{_project_label(sc.get('project_id'))} · {sc['prompt']}"]], "icon": None,
            "last_run_at": sc.get("last_run_at"), "next_run_at": sc.get("next_run_at"),
            "note": f"once on {sc.get('on_date') or (sc.get('next_run_at') or '')[:10]}" if cad == "once" else ""}


def _legacy_recipe(r: dict[str, Any]) -> dict[str, Any]:
    target = r.get("target") or "any"
    where = "every project" if target == "all" else _project_label(int(target)) if str(target).isdigit() else "the project you pick"
    return {"id": f"r{r['id']}", "source": "recipe", "name": r["name"], "enabled": True, "trig": "manual", "days": [0] * 7, "time": "09:00",
            "event": "", "steps": [["run", f"{where} · {r['prompt']}"]], "icon": None, "last_run_at": None, "next_run_at": None, "note": ""}


def list_all() -> list[dict[str, Any]]:
    own = [{**r, "id": f"d{r['id']}", "source": "den", "note": ""} for r in list_own()]
    return own + [_legacy_schedule(s) for s in db.list_schedules()] + [_legacy_recipe(r) for r in db.list_recipes()]


class RecipeIn(BaseModel):
    name: str = Field("New recipe", max_length=120)
    enabled: bool = False
    trig: str = "manual"
    days: list[int] = Field(default_factory=lambda: [0] * 7)
    time: str = Field("09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    event: str = ""
    steps: list[list[str]] = Field(default_factory=list)
    icon: Optional[str] = Field(None, max_length=400)


def _check(body: RecipeIn) -> None:
    if body.trig not in TRIGGERS:
        raise HTTPException(status_code=400, detail="trigger must be schedule, event or manual")
    if len(body.days) != 7:
        raise HTTPException(status_code=400, detail="days needs seven entries (Mon-Sun)")
    if body.trig == "event" and body.event not in EVENTS:
        raise HTTPException(status_code=400, detail="Pick one of the listed events")
    if len(body.steps) > MAX_STEPS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_STEPS} steps")
    for s in body.steps:
        if len(s) != 2 or s[0] not in STEP_KINDS:
            raise HTTPException(status_code=400, detail="Each step is [kind, text]")


def _project_from(arg: str) -> tuple[Optional[dict[str, Any]], str]:
    """'Project · prompt' -> (project or None, prompt)."""
    if "·" in arg:
        name, prompt = (x.strip() for x in arg.split("·", 1))
        low = name.lower()
        p = next((p for p in db.list_projects() if p["name"].lower() == low), None) or \
            next((p for p in db.list_projects() if p["name"].lower().startswith(low)), None) if low else None
        if p or low in ("every project", "all projects"):
            return p, prompt
    return None, arg.strip()


def _as_legacy(body: RecipeIn) -> Optional[dict[str, Any]]:
    """The schedule fields when this recipe is just 'run one prompt on a schedule', else None."""
    if body.trig != "schedule" or len(body.steps) != 1 or body.steps[0][0] != "run":
        return None
    days = [1 if d else 0 for d in body.days]
    if days == [1] * 7:
        cad, wd = "daily", None
    elif days == [1, 1, 1, 1, 1, 0, 0]:
        cad, wd = "weekdays", None
    elif sum(days) == 1:
        cad, wd = "weekly", days.index(1)
    else:
        return None
    p, prompt = _project_from(body.steps[0][1])
    if not prompt:
        return None
    return {"label": body.name, "prompt": prompt, "project_id": p["id"] if p else None, "cadence": cad, "time_of_day": body.time, "weekday": wd}


def create(body: RecipeIn) -> dict[str, Any]:
    _check(body)
    _ensure()
    now = db._now()
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute("INSERT INTO den_recipes (name, enabled, trig, days, time, event, steps, icon, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (body.name.strip() or "New recipe", 1 if body.enabled else 0, body.trig, json.dumps([1 if d else 0 for d in body.days]),
                            body.time, body.event, json.dumps(body.steps), body.icon, now, now))
        conn.commit()
    return {**get(cur.lastrowid), "id": f"d{cur.lastrowid}", "source": "den"}


def update(rid: str, body: RecipeIn) -> dict[str, Any]:
    _check(body)
    kind, num = rid[:1], int(rid[1:]) if rid[1:].isdigit() else None
    if num is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    if kind == "s" and db.get_schedule(num):
        legacy = _as_legacy(body)
        if legacy:   # still a plain schedule - keep it one
            db.update_schedule(num, {**legacy, "enabled": 1 if body.enabled else 0})
            return _legacy_schedule(db.get_schedule(num))
        made = create(body)          # grew beyond a schedule: becomes a recipe, the old row goes to the bin
        _bin_legacy("schedule", num)
        return made
    if kind == "r" and db.get_recipe(num):
        if body.trig == "manual" and len(body.steps) == 1 and body.steps[0][0] == "run":
            p, prompt = _project_from(body.steps[0][1])
            with db._lock:
                conn = db.get_conn()
                conn.execute("UPDATE recipes SET name = ?, prompt = ?, target = ? WHERE id = ?",
                             (body.name, prompt, str(p["id"]) if p else "any", num))
                conn.commit()
            return _legacy_recipe(db.get_recipe(num))
        made = create(body)
        _bin_legacy("recipe", num)
        return made
    if kind == "d" and get(num):
        with db._lock:
            conn = db.get_conn()
            conn.execute("UPDATE den_recipes SET name = ?, enabled = ?, trig = ?, days = ?, time = ?, event = ?, steps = ?, icon = ?, updated_at = ? WHERE id = ?",
                         (body.name.strip() or "Recipe", 1 if body.enabled else 0, body.trig, json.dumps([1 if d else 0 for d in body.days]), body.time,
                          body.event, json.dumps(body.steps), body.icon, db._now(), num))
            conn.commit()
        return {**get(num), "id": f"d{num}", "source": "den"}
    raise HTTPException(status_code=404, detail="Recipe not found")


def _bin_legacy(kind: str, num: int) -> None:
    import den_bin
    row = db.get_schedule(num) if kind == "schedule" else db.get_recipe(num)
    if not row:
        return
    row = {k: v for k, v in row.items() if k not in ("project_name", "unread")}
    den_bin.snapshot(kind, row, (row.get("label") if kind == "schedule" else row.get("name")) or "Recipe", "Recipes & schedules", row.get("prompt") or "")
    (db.delete_schedule if kind == "schedule" else db.delete_recipe)(num)


def set_enabled(rid: str, on: bool) -> dict[str, Any]:
    kind, num = rid[:1], int(rid[1:])
    if kind == "s":
        db.update_schedule(num, {"enabled": 1 if on else 0})
        return _legacy_schedule(db.get_schedule(num))
    if kind == "d" and get(num):
        with db._lock:
            conn = db.get_conn()
            conn.execute("UPDATE den_recipes SET enabled = ?, updated_at = ? WHERE id = ?", (1 if on else 0, db._now(), num))
            conn.commit()
        return {**get(num), "id": rid, "source": "den"}
    raise HTTPException(status_code=400, detail="This recipe runs when you press Run")


def delete(rid: str) -> dict[str, Any]:
    kind, num = rid[:1], int(rid[1:]) if rid[1:].isdigit() else -1
    if kind in ("s", "r"):
        _bin_legacy("schedule" if kind == "s" else "recipe", num)
        return {"ok": True}
    r = get(num)
    if not r:
        raise HTTPException(status_code=404, detail="Recipe not found")
    import den_bin
    with db._lock:
        raw = dict(db.get_conn().execute("SELECT * FROM den_recipes WHERE id = ?", (num,)).fetchone())
    den_bin.snapshot("den_recipe", raw, r["name"], "Recipes & schedules", "\n".join(f"{k}: {a}" for k, a in r["steps"]))
    with db._lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM den_recipes WHERE id = ?", (num,))
        conn.commit()
    return {"ok": True}


# ------------------------------------------------------------------ running

def _log(run_id: int, text: str, ok: Any = True) -> None:
    with db._lock:
        conn = db.get_conn()
        r = conn.execute("SELECT log FROM den_recipe_runs WHERE id = ?", (run_id,)).fetchone()
        entries = json.loads(r["log"]) if r else []
        entries.append({"at": db._now(), "text": text[:600], "ok": ok})
        conn.execute("UPDATE den_recipe_runs SET log = ? WHERE id = ?", (json.dumps(entries), run_id))
        conn.commit()


def _set_run(run_id: int, **fields: Any) -> None:
    with db._lock:
        conn = db.get_conn()
        conn.execute(f"UPDATE den_recipe_runs SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", [*fields.values(), run_id])
        conn.commit()


def start(recipe: dict[str, Any], trigger_text: str = "") -> int:
    """Begin a run of an own (den_recipes) recipe; returns the run id. Steps run in continue_run."""
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute("INSERT INTO den_recipe_runs (recipe_id, started_at, trigger_text) VALUES (?, ?, ?)",
                           (recipe["id"], db._now(), trigger_text[:4000]))
        conn.execute("UPDATE den_recipes SET last_run_at = ? WHERE id = ?", (db._now(), recipe["id"]))
        conn.commit()
    run_id = cur.lastrowid
    _log(run_id, "Trigger: " + (trigger_text[:200] or recipe["trig"]))
    continue_run(run_id)
    return run_id


def _model_answer(instruction: str, context: str) -> str:
    import providers
    import router
    model = router.cheapest_api_model()
    if not model:
        raise RuntimeError("No answer-only model is available")
    content = instruction + (f"\n\nContext:\n{context[:12000]}" if context else "")
    out = providers.complete(model, [{"role": "user", "content": content}],
                             "You are Octopus, running one step of an automated recipe for the user. Do exactly what the step asks; "
                             "reply with the result only.", 90)
    db.log_usage("recipe", model["id"], out.input_tokens, out.output_tokens, providers.estimate_cost(model, out.input_tokens, out.output_tokens))
    return out.text.strip()


def _condition(text: str, context: str) -> bool:
    low = text.lower()
    if "idle" in low:
        return not db.list_tasks(statuses=["running"])
    if "fail" in low:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        return any((t.get("finished_at") or "") >= since for t in db.list_tasks(statuses=["error"]))
    if "approval" in low:
        return bool(db.list_pending_approvals())
    ans = _model_answer(f"Answer only yes or no: is this true right now? {text}", context)
    return ans.strip().lower().startswith("y")


def _send_email(to: str, subject: str, body: str) -> None:
    import mailer
    from assistant.providers.google import GmailProvider
    sender = mailer.sender()
    if not sender:
        raise RuntimeError("No Google account can send email - connect one in Connections")
    GmailProvider(sender).send([to or mailer.recipient()], subject or "From the Den", body or "(empty)")


def _step(run_id: int, recipe: dict[str, Any], kind: str, arg: str, context: str) -> tuple[str, str]:
    """Do one step. Returns (status, new context); status is 'next', 'stop', 'wait' or 'fail'."""
    import worker
    if kind == "ask":
        out = _model_answer(arg or "Summarize this.", context)
        _log(run_id, "Ask: " + (arg or "")[:80] + " → " + out[:200])
        return "next", out
    if kind == "run":
        from octopus import plan as planner
        p, prompt = _project_from(arg)
        if p:
            targets = [p]
        elif "every project" in arg.lower():
            targets = [x for x in db.list_projects(active_only=True) if db.has_folder(x) and not db.is_general_project(x)]
        else:
            targets = [planner.general_project()]
        queued = []
        for t in targets:
            if "backlog" in prompt.lower():
                pending = [x for x in db.list_todos(t["id"]) if x["status"] == "pending"]
                if pending:
                    import phases
                    db.run_checked_todos(t["id"], decisions=phases.todo_decisions(t, pending), only_ids=[x["id"] for x in pending])
                    queued.append(f"{len(pending)} backlog item(s) in {t['name']}")
                else:
                    queued.append(f"nothing on {t['name']}'s backlog")
            else:
                full = prompt + (f"\n\nContext from the recipe:\n{context[:6000]}" if context else "")
                task = db.add_task(t["id"], full, 0, None, routed_by="recipe", route_reason=f"Recipe: {recipe['name']}", source="recipe")
                queued.append(f"#{task['id']} in {t['name']}")
        _log(run_id, "Run: queued " + ", ".join(queued))
        return "next", context
    if kind == "email":
        to, _, subject = (x.strip() for x in arg.partition("·"))
        _send_email(to, subject or recipe["name"], context)
        _log(run_id, f"Email sent to {to or 'you'}")
        return "next", context
    if kind == "notify":
        msg = (arg + ("\n" + context[:300] if context else "")).strip() or recipe["name"]
        worker.notify_phone(recipe["name"], msg, "info", open_hash="#/recipes")
        _log(run_id, "Notified: " + msg[:120])
        return "next", context
    if kind == "approve":
        from octopus import plan as planner
        g = planner.general_project()
        task = db.add_task(g["id"], f"Recipe “{recipe['name']}” is waiting for your OK: {arg}", 0, None, lane="control",
                           model_id="octopus-control", routed_by="recipe", route_reason="Recipe approval", source="recipe",
                           title=f"{recipe['name']}: {arg}"[:60])
        db.mark_running(task["id"])
        ap = db.create_approval(task["id"], "recipe", json.dumps({"describe": f"{recipe['name']}: {arg}", "command": arg}))
        _set_run(run_id, approval_id=ap["id"])
        _log(run_id, "Waiting for your approval: " + arg)
        return "wait", context
    if kind == "cond":
        if _condition(arg, context):
            _log(run_id, "If " + arg + " — yes")
            return "next", context
        _log(run_id, "If " + arg + " — no, stopping here", None)
        return "stop", context
    return "fail", context


def continue_run(run_id: int) -> None:
    with db._lock:
        run = dict(db.get_conn().execute("SELECT * FROM den_recipe_runs WHERE id = ?", (run_id,)).fetchone())
    recipe = get(run["recipe_id"])
    if not recipe:
        _set_run(run_id, status="failed", finished_at=db._now())
        return
    context = run["context"] or run.get("trigger_text") or ""
    i = run["step"]
    steps = recipe["steps"]
    while i < len(steps):
        kind, arg = steps[i]
        try:
            status, context = _step(run_id, recipe, kind, arg, context)
        except Exception as e:   # one step failing stops the run - never the scheduler
            log.warning("Recipe %s step %s failed: %s", recipe["id"], i, e)
            _log(run_id, f"Step {i + 1} failed: {e}", False)
            _set_run(run_id, status="failed", step=i, context=context, finished_at=db._now())
            return
        if status == "wait":
            _set_run(run_id, status="waiting", step=i + 1, context=context)
            return
        if status == "stop":
            _set_run(run_id, status="stopped", step=i + 1, context=context, finished_at=db._now())
            return
        i += 1
    _log(run_id, "Done")
    _set_run(run_id, status="done", step=i, context=context, finished_at=db._now())


def resume_waiting() -> None:
    """Runs parked on an approval: carry on once it's approved, stop when denied or stale."""
    _ensure()
    with db._lock:
        rows = [dict(r) for r in db.get_conn().execute(
            "SELECT r.*, a.status AS ap_status, a.task_id AS ap_task FROM den_recipe_runs r LEFT JOIN approvals a ON a.id = r.approval_id "
            "WHERE r.status = 'waiting'").fetchall()]
    for r in rows:
        if r["ap_status"] == "approved":
            db.mark_done(r["ap_task"], "Approved")
            _log(r["id"], "Approved")
            _set_run(r["id"], status="running")
            continue_run(r["id"])
        elif r["ap_status"] in ("denied", "timed_out") or (r["started_at"] < (datetime.now(timezone.utc) - timedelta(hours=WAIT_APPROVAL_HOURS)).isoformat()):
            if r["ap_task"]:
                db.mark_done(r["ap_task"], "Denied" if r["ap_status"] == "denied" else "Not answered")
            _log(r["id"], "Denied — stopped" if r["ap_status"] == "denied" else "Nobody answered — stopped", False)
            _set_run(r["id"], status="denied", finished_at=db._now())


def runs(recipe_id: int, limit: int = 10) -> list[dict[str, Any]]:
    _ensure()
    with db._lock:
        rows = db.get_conn().execute("SELECT * FROM den_recipe_runs WHERE recipe_id = ? ORDER BY id DESC LIMIT ?", (recipe_id, limit)).fetchall()
    return [{**dict(r), "log": json.loads(r["log"])} for r in rows]


def run_now(rid: str) -> dict[str, Any]:
    kind, num = rid[:1], int(rid[1:]) if rid[1:].isdigit() else -1
    if kind == "s":
        sc = db.get_schedule(num)
        if not sc:
            raise HTTPException(status_code=404, detail="Recipe not found")
        import worker
        if sc["project_id"]:
            targets = [db.get_project(sc["project_id"])]
        else:
            targets = [p for p in db.list_projects(active_only=True) if db.has_folder(p) and not db.is_general_project(p)]
        return {"queued": sum(1 for p in targets if p and worker.queue_prompt(p, sc["prompt"], sc["priority"], sc["model"]))}
    if kind == "r":
        raise HTTPException(status_code=400, detail="Pick a project for this recipe - use POST /recipes/{id}/run")
    r = get(num)
    if not r:
        raise HTTPException(status_code=404, detail="Recipe not found")
    run_id = start(r, "Run by hand")
    return {"run_id": run_id, "runs": runs(num, 1)}


# ------------------------------------------------------------------ triggers (the scheduler calls tick())

def _local_now() -> datetime:
    return timeutil.now().astimezone(timeutil.local_tz())


def _fire_once(recipe: dict[str, Any], key: str, text: str) -> None:
    if recipe.get("fired_key") and key in (recipe["fired_key"] or "").split("|"):
        return
    keys = [k for k in (recipe.get("fired_key") or "").split("|") if k][-40:] + [key]
    with db._lock:
        conn = db.get_conn()
        conn.execute("UPDATE den_recipes SET fired_key = ? WHERE id = ?", ("|".join(keys), recipe["id"]))
        conn.commit()
    start(get(recipe["id"]), text)


def tick(now: Optional[datetime] = None) -> None:
    _ensure()
    local = (now or _local_now()).astimezone(timeutil.local_tz())
    resume_waiting()
    for r in [x for x in list_own() if x["enabled"]]:
        try:
            if r["trig"] == "schedule":
                h, m = (int(x) for x in r["time"].split(":"))
                at = local.replace(hour=h, minute=m, second=0, microsecond=0)
                if r["days"][local.weekday()] and at <= local < at + timedelta(minutes=30):
                    _fire_once(r, "day:" + local.date().isoformat(), f"Scheduled · {DAYS[local.weekday()]} {r['time']}")
            elif r["trig"] == "event":
                for key, text in _events(r["event"], local):
                    _fire_once(r, key, text)
        except Exception:
            log.exception("Recipe %s trigger check failed", r["id"])


def _events(event: str, local: datetime) -> list[tuple[str, str]]:
    """(once-key, what happened) for each new occurrence of `event`."""
    since = (local - timedelta(hours=2)).astimezone(timezone.utc).isoformat()
    if event == "A task fails":
        return [(f"task:{t['id']}", f"#{t['id']} {t.get('title') or t['prompt'][:60]} failed: {(t.get('error_message') or '')[:300]}")
                for t in db.list_tasks(statuses=["error"]) if (t.get("finished_at") or "") >= since]
    if event == "Approval waits 10 minutes":
        old = (local - timedelta(minutes=10)).astimezone(timezone.utc).isoformat()
        return [(f"ap:{a['id']}", f"Approval waiting since {a['created_at'][11:16]}: {a.get('task_title') or ''}")
                for a in db.list_pending_approvals() if a["created_at"] <= old]
    if event == "Usage passes 80%":
        u = (db.get_setting("claude_usage") or {}).get("windows", {}).get("five_hour") or {}
        util = u.get("utilization") or 0
        util = util if util <= 1 else util / 100
        if util >= 0.8:
            return [(f"usage:{u.get('resets_at')}", f"The 5-hour usage window is at {round(util * 100)}%")]
        return []
    if event == "A project goes idle":
        out = []
        for p in db.list_projects(active_only=True):
            ts = db.list_tasks(project_id=p["id"])
            if any(t["status"] in ("queued", "running", "rate_limited") for t in ts):
                continue
            last = max((t.get("finished_at") or "" for t in ts), default="")
            if last >= since:
                out.append((f"idle:{p['id']}:{last}", f"{p['name']} has nothing left to do"))
        return out
    if event == "New email from a client":
        return _client_emails(local)
    return []


def _client_emails(local: datetime) -> list[tuple[str, str]]:
    emails = [c["email"] for c in db.list_contacts() if c.get("email")]
    if not emails:
        return []
    last = db.get_setting("recipes_mail_checked") or ""
    if last and last > (local - timedelta(minutes=5)).astimezone(timezone.utc).isoformat():
        return []
    db.set_setting("recipes_mail_checked", local.astimezone(timezone.utc).isoformat())
    try:
        import mailer
        from assistant.providers.google import GmailProvider
        sender = mailer.sender()
        if not sender:
            return []
        found = GmailProvider(sender).search("newer_than:1d from:(" + " OR ".join(emails[:40]) + ")", 10)
    except Exception as e:
        log.warning("Client email check failed: %s", e)
        return []
    return [(f"mail:{m['id']}", f"Email from {m.get('from', '')}: {m.get('subject', '')}\n{(m.get('snippet') or '')[:500]}") for m in found]


# ------------------------------------------------------------------ routes

def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/recipes")
    def recipes_list():
        return {"recipes": list_all(), "events": list(EVENTS), "steps": list(STEP_KINDS)}

    r.post("/den/recipes")(create)

    @r.put("/den/recipes/{rid}")
    def recipes_update(rid: str, body: RecipeIn):
        return update(rid, body)

    class OnIn(BaseModel):
        enabled: bool

    @r.post("/den/recipes/{rid}/enabled")
    def recipes_enable(rid: str, body: OnIn):
        return set_enabled(rid, body.enabled)

    @r.delete("/den/recipes/{rid}")
    def recipes_delete(rid: str):
        return delete(rid)

    @r.post("/den/recipes/{rid}/run")
    def recipes_run(rid: str):
        return run_now(rid)

    @r.get("/den/recipes/{rid}/runs")
    def recipes_runs(rid: str):
        return runs(int(rid[1:])) if rid[:1] == "d" and rid[1:].isdigit() else []

    return r
