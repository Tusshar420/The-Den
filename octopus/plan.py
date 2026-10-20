"""
The plan: what Octopus decided to do with a request, and why.

    request -> classify (router.classify)
            -> project (named in the request / the chat's / asked for)
            -> agent (agents.json: the job description for this category)
            -> model (scored among the enabled candidates: capability, cost, speed,
                      history, availability, and how scarce the resource is)
            -> tools (tools.json: only the ones this request needs)
            -> permissions (assistant/capabilities.py, at the current autonomy level)
            -> show the plan first? (risky, or you asked to always see it)

build_plan() only decides - nothing is queued. queue_plan() turns an accepted
plan into a task on the existing queue. Every plan is a plain dict so it can be
stored (db.routing), shown on the plan card and the Routing page, and replayed.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db
import router
from assistant import capabilities as cap
from octopus import registry

ROOT = Path(__file__).resolve().parent.parent
GENERAL_PROJECT_NAME = "General"
GENERAL_PROJECT_PATH = ROOT / "workspace" / "general"

# Where the weight goes when models are compared, per category "optimize" value.
# Each row adds up to 100. Override with routing.weights in models.json.
WEIGHTS: dict[str, dict[str, float]] = {
    "cost":     {"quality": 20, "cost": 35, "speed": 20, "history": 10, "preference": 15},
    "balanced": {"quality": 30, "cost": 25, "speed": 15, "history": 15, "preference": 15},
    "quality":  {"quality": 45, "cost": 10, "speed": 5,  "history": 20, "preference": 20},
}
SCARCE_PENALTY = 12          # spending a limited subscription on work that doesn't need it
SCARCE_BUSY_PENALTY = 25     # ... while its usage window is nearly used up
BUSY_ABOVE = 0.85
MIN_HISTORY = 3              # runs before a model's track record counts for or against it
CODE_CATEGORIES = ("coding", "salesforce", "system")

# What a request's wording says it will touch, beyond what its tools always do.
# Only counted for an agent that may exercise the capability at all.
RISK_WORDS: list[tuple[str, list[str]]] = [
    (r"\b(deploy(ing|ment)?|publish|release|ship it|go live)\b", ["DEPLOY"]),
    (r"\b(production|prod)\b|\blive (site|server|database)\b", ["PRODUCTION"]),
    (r"\bpush\b|\bforce[- ]?push\b|\bopen (a )?(pull request|pr)\b", ["GIT_PUSH"]),
    (r"\brm -rf\b|\bdrop (the )?(table|database|schema)\b|\bwipe\b|\berase\b|reset --hard|--force\b|"
     r"\bdelete (all|every|the (whole |entire )?(database|table|branch|folder|directory|repo(sitory)?|logs?))\b", ["DESTRUCTIVE"]),
    (r"\bsudo\b|\blaunchctl\b|\bsystem settings\b", ["SYSTEM_ADMIN"]),
    (r"\b(npm|pnpm|yarn|pip3?|brew|gem|cargo) (install|add|update|upgrade)\b|\binstall (a |the )?(package|dependenc\w+|library)\b|"
     r"\bupgrade (the )?dependenc\w+", ["PACKAGE_INSTALL"]),
    (r"\bcommit\b|\bmerge\b|\brebase\b|\b(create|new|switch to) (a )?branch\b", ["GIT_WRITE"]),
]
# "Check why the form is failing, but don't change anything yet."
READ_ONLY_RE = re.compile(
    r"\bdon.?t (change|modify|edit|touch|fix|write|commit)\b|\bdo not (change|modify|edit|touch|fix|write|commit)\b|"
    r"\bwithout (changing|modifying|editing|touching|fixing)\b|\bread[- ]only\b|\bno changes\b|"
    r"\b(just|only) (look|check|investigate|explain|tell me|diagnose|analy[sz]e|review)\b|\binvestigate only\b", re.IGNORECASE)


# ------------------------------------------------------------------ projects

def general_project() -> dict[str, Any]:
    """Home for requests that don't belong to a registered repo. Its folder is
    only used if such a request ends up in Claude Code."""
    GENERAL_PROJECT_PATH.mkdir(parents=True, exist_ok=True)
    return db.get_project_by_name(GENERAL_PROJECT_NAME) or db.add_project(GENERAL_PROJECT_NAME, str(GENERAL_PROJECT_PATH))


def real_projects() -> list[dict[str, Any]]:
    return [p for p in db.list_projects(active_only=True) if not db.is_general_project(p)]


def _name_pattern(name: str) -> Optional[re.Pattern]:
    words = [w for w in re.split(r"[\s_\-./]+", name.strip().lower()) if w]
    if not words or len("".join(words)) < 3:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[\s_\-./]?".join(re.escape(w) for w in words) + r"(?![a-z0-9])")


def find_project(text: str, projects: Optional[list[dict[str, Any]]] = None) -> Optional[dict[str, Any]]:
    """The registered project a request names ("KodeAura7", "kode-aura 7"), else
    None. The longest matching name wins, so "app" never beats "app-backend"."""
    lowered = text.lower()
    best: Optional[dict[str, Any]] = None
    for p in (projects if projects is not None else real_projects()):
        pat = _name_pattern(p["name"])
        if pat and pat.search(lowered) and (best is None or len(p["name"]) > len(best["name"])):
            best = p
    return best


# -------------------------------------------------------------------- models

def claude_window() -> dict[str, Any]:
    """How full Claude Code's 5-hour window is: {utilization, resets_at, busy}."""
    w = ((db.get_setting("claude_usage") or {}).get("windows") or {}).get("five_hour") or {}
    util, resets = w.get("utilization"), w.get("resets_at")
    busy = False
    if util is not None and resets:
        try:
            busy = util > BUSY_ABOVE and datetime.fromisoformat(resets.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except ValueError:
            pass
    return {"utilization": util, "resets_at": resets, "busy": busy}


def _weights(optimize: str) -> dict[str, float]:
    custom = (router.registry().get("routing", {}).get("weights") or {}).get(optimize)
    return {**WEIGHTS.get(optimize, WEIGHTS["balanced"]), **(custom or {})}


def _quality(m: dict[str, Any], quality_from: str) -> float:
    rating = m.get(quality_from) or m.get("reasoning") or 0
    return rating / 5 if rating else 0.5   # image / tool models have no rating: neutral


def choose_model(category: str, text: str, agent: Optional[dict[str, Any]] = None,
                 candidate_ids: Optional[list[str]] = None, vision: bool = False) -> dict[str, Any]:
    """Pick the model for a category. Returns
    {model, candidates: [...scored...], skipped: [...], waiting: str, optimize}.

    Candidates come from the category in models.json (or `candidate_ids`, e.g.
    an agent picked by hand). Ones that are disabled, unready, rate-limited, too
    small for the request, or unable to do what the category needs are skipped
    with the reason. The rest are scored - quality, cost, speed, track record,
    your configured order, and how scarce the resource is - and the best wins.
    A category marked non-transferable never hops models: its first choice is
    kept, and if that one is rate-limited the task simply waits for it.
    vision=True means the request has pictures attached, so models that can't
    read them are skipped (Claude Code can: it opens the files)."""
    cat = router.registry().get("routing", {}).get("categories", {}).get(category) or {}
    ids = candidate_ids or cat.get("candidates") or (agent or {}).get("preferred_models") or []
    cfgs = [m for m in (router.get_model(i) for i in ids) if m]
    optimize = cat.get("optimize", "balanced")
    weights = _weights(optimize)
    quality_from = cat.get("quality_from", "reasoning")
    stats = db.model_category_stats()
    window = claude_window()
    need = cat.get("requires")
    est_tokens = max(1, len(text) // 3)

    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    waiting = ""
    keep_first = not cat.get("transferable", True)
    for i, m in enumerate(cfgs):
        ok, why, until = router.availability(m)
        row = {"id": m["id"], "label": m.get("label", m["id"]), "provider": m.get("provider"), "lane": router.lane_for(m),
               "available": ok, "reason": why, "score": None, "parts": {}, "cost": m.get("cost"), "speed": m.get("speed"),
               "scarce": bool(m.get("scarce")), "rate_limited_until": until}
        if ok and need and not (m.get(need) or m.get("provider") == "claude_code"):
            row.update(available=False, reason=f"can't {need.replace('_', ' ')}")
        if ok and vision and not (m.get("vision") or m.get("provider") == "claude_code"):
            row.update(available=False, reason="can't read pictures")
        if ok and m.get("context_tokens") and est_tokens > m["context_tokens"] * 0.8:
            row.update(available=False, reason=f"request needs about {est_tokens:,} tokens, more than it can hold")
        rows.append(row)
        if not row["available"]:
            if i == 0 and until and keep_first:
                waiting = why
            skipped.append(f"{row['label']} ({row['reason']})")

    usable = [r for r in rows if r["available"]]
    if keep_first and rows and not rows[0]["available"] and waiting:
        usable = [rows[0]]                    # wait for it rather than move to a model without the same tools
    if not usable:
        return {"model": None, "candidates": rows, "skipped": skipped, "waiting": waiting, "optimize": optimize}

    n = len(cfgs)
    for r in usable:
        m = router.get_model(r["id"])
        stat = stats.get(f"{m['id']}|{category}")
        history = (stat["done"] / stat["n"]) if stat and stat["n"] >= MIN_HISTORY else 0.5
        parts = {
            "quality": _quality(m, quality_from) * weights["quality"],
            "cost": (5 - (m.get("cost") or 3)) / 4 * weights["cost"],
            "speed": (m.get("speed") or 3) / 5 * weights["speed"],
            "history": history * weights["history"],
            "preference": ((n - 1 - ids.index(m["id"])) / (n - 1) if n > 1 else 1) * weights["preference"],
        }
        if m.get("scarce") and category not in CODE_CATEGORIES:
            parts["scarcity"] = -SCARCE_PENALTY - (SCARCE_BUSY_PENALTY if window["busy"] else 0)
        r["parts"] = {k: round(v, 1) for k, v in parts.items()}
        r["score"] = round(sum(parts.values()), 1)
        r["history"] = {"n": stat["n"], "done": stat["done"]} if stat else None

    best = usable[0] if keep_first else max(usable, key=lambda r: (r["score"], -ids.index(r["id"])))
    return {"model": router.get_model(best["id"]), "candidates": rows, "skipped": skipped, "waiting": waiting,
            "optimize": optimize}


def _why_model(choice: dict[str, Any], category: str) -> str:
    m, rows = choice["model"], [r for r in choice["candidates"] if r["available"]]
    label = m.get("label", m["id"])
    if choice["waiting"]:
        return f"{label} is the configured choice for {category}; waiting for it ({choice['waiting']})"
    if len(rows) <= 1:
        others = [s for s in choice["skipped"]]
        return (f"{label} is the only usable model for {category}" if others or len(rows) == 1 else label) + \
               (f" - skipped {'; '.join(others)}" if others else "")
    best = next(r for r in rows if r["id"] == m["id"])
    top = sorted(((k, v) for k, v in best["parts"].items() if v > 0), key=lambda kv: -kv[1])[:2]
    runner = sorted((r for r in rows if r["id"] != m["id"]), key=lambda r: -r["score"])[0]
    how = {"cost": "low cost", "quality": "strong at it", "speed": "fast", "history": "good track record", "preference": "your preferred order"}
    return (f"{label} scored {best['score']:.0f} ({', '.join(how[k] for k, _ in top)}) "
            f"vs {runner['label']} {runner['score']:.0f}" + (" · saves Claude Code usage" if runner.get("scarce") else ""))


def estimate_cost(model_cfg: Optional[dict[str, Any]], text: str, category: str) -> Optional[float]:
    """A rough dollar figure for an api-lane request; None when it can't be known
    (Claude Code is covered by your subscription, free tiers have no price)."""
    if not model_cfg or router.lane_for(model_cfg) != "api":
        return None
    if model_cfg.get("modality") == "image":
        return model_cfg.get("price_per_image")
    price = model_cfg.get("price_per_mtok")
    if not price:
        return None
    m = re.search(r"\b(\d{2,5})[- ]?words?\b", text)
    out_tokens = int(int(m.group(1)) * 1.4) if m else {"writing": 1500, "research": 1200}.get(category, 500)
    return round((len(text) / 3 * price.get("input", 0) + out_tokens * price.get("output", 0)) / 1_000_000, 4)


# ---------------------------------------------------------------------- plan

def _short(text: str, n: int = 70) -> str:
    line = " ".join(text.split())
    return line if len(line) <= n else line[:n].rsplit(" ", 1)[0] + "…"


def _permission_rows(caps: list[str], flagged: set[str], mode: str) -> list[dict[str, Any]]:
    rows = []
    for c in caps:
        meta = cap.CAPABILITIES[c]
        rows.append({"capability": c, "label": meta["label"], "risk": meta["risk"], "protected": meta["protected"],
                     "policy": cap.effective_policy_for(c, mode), "flagged": c in flagged})
    return sorted(rows, key=lambda r: (-cap.RISK_ORDER[r["risk"]], r["capability"]))


def _reason(category: str, agent: dict[str, Any], tools: list[dict[str, Any]], project: Optional[dict[str, Any]],
            read_only: bool, why: str) -> str:
    needs = {"filesystem": "project files", "git": "git", "terminal": "running commands and tests", "github": "GitHub",
             "salesforce": "Salesforce tooling", "web": "the web", "image_generation": "image generation",
             "gmail": "your email", "calendar": "your calendar", "reminders": "reminders", "workspace": "Octopus's queue and projects"}
    used = [needs[t["id"]] for t in tools if t["id"] in needs]
    if used:
        base = "Needs " + (", ".join(used[:-1]) + " and " + used[-1] if len(used) > 1 else used[0]) + "."
    else:
        base = "Everything needed is in the message - no project or tools required."
    if read_only:
        base += " You asked for no changes, so it will only investigate."
    return base if not why else f"{base} ({why})"


def build_plan(text: str, *, project_id: Optional[int] = None, force_model: Optional[str] = None,
               force_agent: Optional[str] = None, thread_root: Optional[dict[str, Any]] = None,
               source: str = "api", images: Optional[list[str]] = None) -> dict[str, Any]:
    """Decide how a request will be handled. Nothing is queued or run.
    images: ids of attached pictures (already checked) - they ride along in the
    plan so a plan that waits for Run still has them, and they steer the model
    choice toward one that can read them."""
    text = text.strip()
    autonomy = cap.autonomy()
    overrides: dict[str, str] = {}
    control = None
    intent = None

    # ---- project (explicit > this chat's > named in the request)
    project: Optional[dict[str, Any]] = None
    project_from = ""
    if project_id is not None:
        project = db.get_project(project_id)
        if not project:
            raise ValueError("Project not found")
        project_from = "you chose it"
    elif thread_root:
        project = db.get_project(thread_root["project_id"])
        project_from = "same conversation"
    else:
        project = find_project(text)
        project_from = "named in the request" if project else ""
    if project and db.is_general_project(project) and not thread_root:
        project, project_from = None, ""

    # ---- classification (a chat continues as it started)
    if thread_root and not force_agent:
        category = thread_root.get("category") or "general"
        classified = {"category": category, "reason": "continues the conversation", "routed_by": "thread"}
    elif force_agent:
        agent0 = registry.get_agent(force_agent)
        if not agent0:
            raise ValueError(f"Unknown agent '{force_agent}'")
        overrides["agent"] = force_agent
        category = (agent0.get("categories") or ["general"])[0]
        classified = {"category": category, "reason": f"you picked the {agent0.get('label', force_agent)}", "routed_by": "manual"}
    else:
        classified = router.classify(text, project["name"] if project else None, in_thread=False)
        category = classified["category"]
    control, intent = classified.get("control"), classified.get("intent")

    agent = registry.get_agent(force_agent) if force_agent else (
        registry.get_agent(thread_root.get("agent_id")) if thread_root and thread_root.get("agent_id") else None
    ) or registry.agent_for_category(category)
    if not agent:
        raise ValueError(f"No agent handles '{category}' - add one to agents.json")
    exe = agent.get("execution") or {}
    ctx = agent.get("context") or {}

    # ---- model
    candidate_ids = None
    if force_model and force_model != "auto":
        forced = router.get_model(force_model)
        if not forced:
            raise ValueError(f"Unknown model '{force_model}'")
        overrides["model"] = forced["id"]
        candidate_ids = [forced["id"]]
    elif thread_root and (db.get_thread_model(thread_root["id"]) or thread_root.get("model_id")):
        # The model the chat was last on - it may have been switched since it started.
        candidate_ids = [db.get_thread_model(thread_root["id"]) or thread_root["model_id"]]
    elif force_agent:
        candidate_ids = agent.get("preferred_models")
    else:
        ruled = router.rule_model(text, project["name"] if project else None, category, bool(images))
        if ruled:                                  # a routing rule / default model from the AI Providers page
            candidate_ids = [ruled[0]]
            overrides["rule"] = ruled[1]
    choice = choose_model(category, text, agent, candidate_ids, vision=bool(images))
    model = choice["model"]
    if not model and thread_root and thread_root.get("model_id"):       # the chat's model is unavailable: use the category's
        choice = choose_model(category, text, agent, vision=bool(images))
        model = choice["model"]
    lane = router.lane_for(model) if model else exe.get("lane", "api")

    # ---- tools and permissions
    tools = registry.select_tools(agent, category, text)
    tool_caps = [c for t in tools for c in t.get("capabilities", []) if c in cap.CAPABILITIES]
    allowed = set(agent.get("permissions") or [])
    read_only = bool(READ_ONLY_RE.search(text)) and lane == "agent"
    flagged: set[str] = set()
    if allowed:
        for pattern, caps in RISK_WORDS:
            if re.search(pattern, text, re.IGNORECASE):
                flagged.update(c for c in caps if c in allowed)
    if read_only:
        flagged = set()
    expected = [c for c in dict.fromkeys(tool_caps + sorted(flagged))
                if (c in allowed or not allowed) and (not cap.CAPABILITIES[c]["protected"] or c in flagged)]
    if read_only:
        expected = [c for c in expected if cap.CAPABILITIES[c]["readonly"]]
    if intent and intent.get("capabilities"):
        expected = list(dict.fromkeys(intent["capabilities"]))
    permissions = _permission_rows(expected, flagged, autonomy)
    risk = cap.risk_of(expected)
    # The personal-assistant lane has its own gate (an approval card per action, and a plain
    # refusal for a denied one), so its plans are never held up here.
    blocked = [p["label"] for p in permissions if p["policy"] == cap.DENY] if lane != "assistant" else []
    asks = [p["label"] for p in permissions if p["policy"] == cap.ASK]

    # ---- project needed?
    needs_input = None
    if ctx.get("needs_project") and not project:
        options = real_projects()
        if options:
            needs_input = {"kind": "project", "question": "Which project is this for?",
                           "options": [{"id": p["id"], "name": p["name"]} for p in options]}
        else:
            project, project_from = general_project(), "no projects registered - using the General workspace"
    elif not project and lane in ("api", "assistant", "control"):
        project = general_project()

    # ---- permission mode for Claude Code (how much it may do unattended)
    modes = exe.get("permission_mode") or {}
    permission_mode = None
    if lane == "agent":
        permission_mode = "plan" if read_only else modes.get(autonomy) or db.DEFAULT_PERMISSION_MODE

    # ---- show the plan first?
    review, review_why = False, ""
    setting = db.get_setting("plan_review")
    show_mode = exe.get("show_plan", "risky")     # risky | always | never (agents.json)
    if needs_input:
        review, review_why = True, "Octopus needs one answer first"
    elif blocked:
        review, review_why = True, "blocked by your permission settings"
    elif not model:
        review, review_why = True, "no model is available for it right now"
    elif show_mode != "never" and (risk == "high" or flagged):
        review, review_why = True, "touches something risky: " + ", ".join(sorted(cap.CAPABILITIES[c]["label"] for c in flagged) or ["high-risk actions"])
    elif show_mode == "always" and not thread_root and setting != "never":
        review, review_why = True, f"the {agent.get('label', agent['id'])} always shows its plan"
    elif setting == "always" and lane != "control":
        review, review_why = True, "you asked to see every plan"
    elif autonomy == "assist" and lane == "agent" and not thread_root and not read_only and setting != "never":
        review, review_why = True, "Assist mode: Octopus proposes before acting"
    est = estimate_cost(model, text, category)
    if not review and est is not None and exe.get("max_cost_usd") and est > exe["max_cost_usd"]:
        review, review_why = True, f"estimated ${est:.2f} is over this agent's ${exe['max_cost_usd']:.2f} limit"

    why_model = _why_model(choice, category) if model else "; ".join(choice["skipped"]) or "no usable model"
    reason = _reason(category, agent, tools, project, read_only, classified.get("reason", ""))
    route_reason = f"{category}: {classified.get('reason', '')}".rstrip(": ") + (f" · {why_model}" if model else "")
    if overrides:
        route_reason += " · " + ", ".join(f"you picked {k} {v}" for k, v in overrides.items())

    return {
        "request": text,
        "images": list(images or []),
        "title": _short(text, 60),
        "source": source,
        "category": category,
        "classified_by": classified.get("routed_by", "rules"),
        "classification_reason": classified.get("reason", ""),
        "project": {"id": project["id"], "name": project["name"], "is_general": db.is_general_project(project),
                    "how": project_from} if project else None,
        "agent": {"id": agent["id"], "label": agent.get("label", agent["id"])},
        "model": ({"id": model["id"], "label": model.get("label", model["id"]), "provider": model.get("provider"),
                   "lane": lane, "why": why_model} if model else None),
        "lane": lane,
        "unavailable": None if model else why_model,
        "candidates": choice["candidates"],
        "optimize": choice["optimize"],
        "tools": [registry.tool_summary(t) for t in tools],
        "permissions": permissions,
        "risk": risk,
        "asks_first": asks,
        "blocked": blocked,
        "permission_mode": permission_mode,
        "read_only": read_only,
        "autonomy": autonomy,
        "est_cost_usd": est,
        "reason": reason,
        "route_reason": route_reason[:600],
        "overrides": overrides,
        "needs_input": needs_input,
        "show_plan": review,
        "show_plan_reason": review_why,
        "intent": intent,
        "control": control,
        "thread_id": (thread_root.get("thread_id") or thread_root["id"]) if thread_root else None,
        "requirements": {
            "project": bool(ctx.get("needs_project")),
            "files": any(t["id"] == "filesystem" for t in tools),
            "terminal": any(t["id"] == "terminal" for t in tools),
            "git": any(t["id"] in ("git", "github") for t in tools),
            "mcp": any(t.get("kind") == "mcp" for t in tools),
            "web": any(t["id"] == "web" for t in tools),
            "approval": bool(asks or blocked),
        },
    }


def with_project(plan: dict[str, Any], project_id: int) -> dict[str, Any]:
    """The same request once the user has said which project - rebuilt, so the
    permissions, review decision and Claude Code mode all follow."""
    return build_plan(plan["request"], project_id=project_id, force_model=plan["overrides"].get("model"),
                      force_agent=plan["overrides"].get("agent"), source=plan.get("source", "api"),
                      images=plan.get("images")) | ({"account": plan["account"]} if plan.get("account") else {})


def decision_of(plan: dict[str, Any]) -> dict[str, Any]:
    """The compact shape older clients (and the Ask box's toast) read."""
    m = plan.get("model") or {}
    out = {"category": plan["category"], "model_id": m.get("id"), "model_label": m.get("label"), "lane": plan["lane"],
           "routed_by": plan["classified_by"], "reason": plan["route_reason"], "agent": plan["agent"]["label"],
           "agent_id": plan["agent"]["id"], "tools": [t["label"] for t in plan["tools"]], "risk": plan["risk"]}
    if plan.get("intent"):
        out["intent"] = plan["intent"]
    return out


def queue_plan(plan: dict[str, Any], priority: int = 0, routing_id: Optional[int] = None,
               permission_mode: Optional[str] = None) -> dict[str, Any]:
    """Turn an accepted plan into a task on the existing queue."""
    if plan.get("blocked"):
        raise PermissionError("Blocked by your permission settings: " + ", ".join(plan["blocked"]))
    if plan.get("needs_input") or not plan.get("model"):
        raise ValueError("The plan isn't complete yet - " + ((plan.get("needs_input") or {}).get("question") or "no model is available"))
    project_id = (plan.get("project") or general_project())["id"]
    mode = permission_mode or plan.get("permission_mode")
    return db.add_task(project_id, plan["request"], priority, plan.get("thread_id"), lane=plan["lane"],
                       model_id=plan["model"]["id"], category=plan["category"], route_reason=plan["route_reason"],
                       routed_by=plan["classified_by"], permission_mode=mode if plan["lane"] == "agent" else None,
                       agent_id=plan["agent"]["id"], tools=[t["id"] for t in plan["tools"]], routing_id=routing_id,
                       source=plan.get("source"), risk=plan["risk"], images=plan.get("images") or None,
                       claude_account=plan.get("account") if plan["lane"] == "agent" else None)


def log_events(task: dict[str, Any], plan: dict[str, Any], decided_by: str = "auto") -> None:
    """The first lines of a task's Activity timeline: what Octopus understood and
    decided, in plain words, before any work starts."""
    def note(text: str) -> None:
        db.add_task_event(task["id"], "note", text)
    via = {"voice": " by voice", "dashboard": "", "api": "", "shortcut": " from a Shortcut"}.get(plan.get("source") or "", "")
    if plan.get("thread_id"):
        note(f"Received a follow-up{via} - continuing the conversation")
    else:
        note(f"Received request{via}")
        note(f"Classified as a {plan['category']} task" + (f" ({plan['classification_reason']})" if plan.get("classification_reason") else ""))
    if plan.get("project") and not plan["project"]["is_general"]:
        note(f"Selected {plan['project']['name']}" + (f" ({plan['project']['how']})" if plan["project"].get("how") else ""))
    m = plan.get("model") or {}
    note(f"Chose the {plan['agent']['label']} on {m.get('label', m.get('id', 'a model'))}"
         + (" - you picked it" if decided_by == "user" else ""))
    if plan.get("tools"):
        note("Tools: " + ", ".join(t["label"] for t in plan["tools"]))
    if plan.get("read_only"):
        note("Read-only: you asked for no changes, so it will only investigate")
    if decided_by == "user" and plan.get("show_plan"):
        note("You approved the plan")
