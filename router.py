"""
Octopus routing: turn a natural-language request into (category, model).

    request -> classify (cheap model, keyword rules as backup)
            -> category's candidate list in models.json
            -> first candidate that is enabled, has its provider ready and
               isn't rate-limited (unless the category isn't transferable,
               in which case a rate-limited first choice is kept and the
               task just waits for it)

Everything tunable lives in models.json, which is re-read whenever it
changes on disk - so adding a model or reordering fallbacks never needs a
code change or a restart.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import db
import providers
from assistant import intent as assistant_intent

log = logging.getLogger("the-den.router")

REGISTRY_PATH = Path(__file__).parent / "models.json"

_cache: dict[str, Any] = {"mtime": None, "data": None}


# ---------------------------------------------------------------- registry

def registry() -> dict[str, Any]:
    mtime = REGISTRY_PATH.stat().st_mtime
    if _cache["mtime"] != mtime:
        _cache["data"] = json.loads(REGISTRY_PATH.read_text())
        _cache["mtime"] = mtime
    # "Enabled" can also be flipped from the AI Providers page; that lives in the
    # settings table so models.json never has to be rewritten by the app.
    overrides = db.get_setting("model_overrides", {}) or {}
    if not overrides:
        return _cache["data"]
    return {**_cache["data"], "models": [
        {**m, "enabled": overrides[m["id"]]["enabled"]} if isinstance(overrides.get(m["id"], {}).get("enabled"), bool) else m
        for m in _cache["data"].get("models", [])]}


def get_model(model_id: Optional[str]) -> Optional[dict[str, Any]]:
    for m in registry().get("models", []):
        if m["id"] == model_id:
            return m
    return None


def lane_for(model_cfg: dict[str, Any]) -> str:
    """agent = Claude Code in a project; assistant = the personal-assistant
    tools (email, calendar, reminders); control = instant commands about
    Octopus itself; api = a plain model call (text or image)."""
    provider = model_cfg.get("provider")
    return ("agent" if provider == "claude_code" else "assistant" if provider == "tools"
            else "control" if provider == "control" else "api")


def availability(model_cfg: dict[str, Any]) -> tuple[bool, str, Optional[str]]:
    """(usable_now, reason_if_not, rate_limited_until)."""
    if not model_cfg.get("enabled", True):
        return False, "disabled in models.json", None
    ready, why = providers.provider_ready(model_cfg.get("provider", ""))
    if not ready:
        return False, why, None
    until = db.model_rate_limited_until(model_cfg["id"])
    if until:
        return False, f"rate-limited until {until}", until
    return True, "", None


def cheapest_api_model() -> Optional[dict[str, Any]]:
    """The cheapest (then fastest) answer-only model usable right now - for
    small side jobs like polishing, naming and summarising."""
    candidates = [m for m in registry().get("models", []) if lane_for(m) == "api" and availability(m)[0]]
    return sorted(candidates, key=lambda m: (m.get("cost", 5), -m.get("speed", 0)))[0] if candidates else None


def models_status() -> list[dict[str, Any]]:
    out = []
    for m in registry().get("models", []):
        ok, why, until = availability(m)
        out.append({**m, "lane": lane_for(m), "available": ok, "unavailable_reason": why,
                    "rate_limited_until": until})
    return out


# ------------------------------------------------------------ classification

CODING_RE = re.compile(
    r"\b(fix|debug|refactor|implement|add (a )?(feature|test|endpoint|page|component)|"
    r"run (the )?(tests?|build|linter?)|compile|commit|merge conflict|rebase|"
    r"deploy|install|migrat\w*|repo(sitory)?|codebase|this project|in the project|"
    r"(edit|update|change|modify|rename|delete|create) (the |a |this )?(file|function|class|module|component)|"
    r"apex|lwc|salesforce|sfdx|sf project|pull request|\bpr\b|git)\b",
    re.IGNORECASE,
)
# "Why is the contact form failing?" is about code even with no verb like "fix" in it;
# and any look-into-it verb is about code when a project is on the table.
DIAGNOSTIC_RE = re.compile(
    r"\bwhy\b[^.?\n]{0,60}\b(fail\w*|crash\w*|broken|not working|errors?|erroring|slow|timing out|500)\b|"
    r"\b(bugs?|stack ?trace|exception|regression|failing (tests?|build)|not working)\b", re.IGNORECASE)
INVESTIGATE_RE = re.compile(
    r"\b(check|investigate|look (at|into)|review|audit|inspect|analy[sz]e|what(?:'s| is) wrong|status of|debug|"
    r"add|build|create|implement|update|change|modify|remove|delete|rename|make|improve|optimi[sz]e|migrate|upgrade|set up|"
    r"wire|hook up|style|tweak|move|extract|clean up|speed up|test|document)\b", re.IGNORECASE)
SIMPLE_MAX_CHARS = 160


IMAGE_RE = re.compile(
    r"\b(generate|create|make|draw|design|paint|render|produce|illustrate)\b[^.\n]{0,40}\b"
    r"(image|picture|photo|illustration|logo|icon|wallpaper|artwork|poster|sketch|portrait|avatar)s?\b", re.IGNORECASE)
SALESFORCE_RE = re.compile(r"\b(apex|lwc|sfdx|soql|salesforce|sf project|visualforce|flows?\b.*\borg|scratch org)\b", re.IGNORECASE)
SYSTEM_RE = re.compile(
    r"\b(disk space|free up (space|storage)|storage (is )?full|my mac\b|this mac\b|macbook|homebrew|brew (install|update|upgrade)|"
    r"launchctl|launchd|activity monitor|running (apps|processes)|cpu usage|memory usage|clean up (my )?(downloads|desktop))", re.IGNORECASE)
WRITING_RE = re.compile(
    r"\b(write|draft|compose|rewrite|rephrase|proofread|summari[sz]e|translate|paraphrase|edit)\b[^.\n]{0,60}\b"
    r"(essay|article|blog|post|story|poem|haiku|limerick|sonnet|lyrics|song|joke|riddle|slogan|tagline|tweet|thread|letter|speech|"
    r"paragraph|report|summary|caption|bio|cover letter|copy|description|newsletter|script|text|words?)\b|\b\d{2,5}[- ]words?\b", re.IGNORECASE)
RESEARCH_RE = re.compile(
    r"\b(research|look up|search (the )?(web|online|internet)|find out|latest|current (version|price|news)|news about|"
    r"compare\b.*\b(vs\.?|versus|and)\b|what.s new in|sources? for|pros and cons)\b", re.IGNORECASE)


def _rules_classify(text: str, has_project: bool) -> tuple[str, str]:
    if IMAGE_RE.search(text):
        return "image", "asks for a picture"
    if SALESFORCE_RE.search(text):
        return "salesforce", "mentions Salesforce work"
    if CODING_RE.search(text):
        return "coding", "mentions project/code actions"
    if DIAGNOSTIC_RE.search(text):
        return "coding", "asks why something in the code or app is failing"
    if has_project and INVESTIGATE_RE.search(text):
        return "coding", "asks to look into a project"
    if SYSTEM_RE.search(text):
        return "system", "is about this Mac itself"
    if WRITING_RE.search(text):
        return "writing", "asks for a piece of writing"
    if RESEARCH_RE.search(text):
        return "research", "asks for looked-up information"
    if len(text) <= SIMPLE_MAX_CHARS and "\n" not in text:
        return "simple", "short request with no project/code actions"
    return "general", "no project/code actions mentioned"


CLASSIFIER_INSTRUCTIONS = """You route requests for Octopus, a personal AI assistant. Pick exactly one category:
{categories}
Reply with only a JSON object: {{"category": "<one of: {names}>", "reason": "<max 12 words>"}}"""


def _llm_classify(text: str, project_name: Optional[str]) -> Optional[tuple[str, str]]:
    reg = registry()
    routing = reg.get("routing", {})
    cats = {n: c for n, c in routing.get("categories", {}).items() if c.get("classifiable", True)}
    model_cfg = get_model(routing.get("classifier"))
    if not model_cfg or not cats:
        return None
    ok, _, _ = availability(model_cfg)
    if not ok:
        return None

    instructions = CLASSIFIER_INSTRUCTIONS.format(
        categories="\n".join(f"- {name}: {c.get('description', '')}" for name, c in cats.items()),
        names=", ".join(cats),
    )
    context = f"Selected project: {project_name}\n" if project_name else "No project selected.\n"
    try:
        result = providers.complete(model_cfg, [{"role": "user", "content": context + "Request:\n" + text[:4000]}],
                                    instructions, timeout=20)
    except providers.ProviderError as e:
        log.warning("Classifier call failed, falling back to rules: %s", e)
        return None

    m = re.search(r"\{.*\}", result.text, re.DOTALL)
    try:
        parsed = json.loads(m.group(0)) if m else {}
    except ValueError:
        parsed = {}
    category = parsed.get("category")
    if category not in cats:
        log.warning("Classifier returned unusable output: %r", result.text[:200])
        return None
    return category, str(parsed.get("reason", ""))[:200]


# ------------------------------------------------------------------ routing

def classify(text: str, project_name: Optional[str] = None, in_thread: bool = False) -> dict[str, Any]:
    """What kind of request is this? {category, reason, routed_by[, intent]}.
    Rules first where they are certain (instant, free, and a personal-assistant
    or control request must never be mistaken for code), then the cheap
    classifier model, then keyword rules as the backup."""
    # Instant commands about Octopus itself: "what's happening", "stop that".
    from octopus import control
    found = control.match(text)
    if found:
        return {"category": found["category"], "reason": found["reason"], "routed_by": "rules", "control": found}

    assistant_model = get_model("octopus-tools")
    if assistant_model and assistant_model.get("enabled", True):
        pa = assistant_intent.classify(text, in_thread=in_thread)
        if pa.matched:
            return {"category": "assistant", "reason": pa.route_note(), "routed_by": "rules", "intent": pa.to_dict()}

    # A clear-cut image request needs no classifier call.
    if IMAGE_RE.search(text):
        return {"category": "image", "reason": "asks for a picture", "routed_by": "rules"}

    llm = _llm_classify(text, project_name)
    if llm:
        return {"category": llm[0], "reason": llm[1], "routed_by": "classifier"}
    category, why = _rules_classify(text, project_name is not None)
    return {"category": category, "reason": why, "routed_by": "rules"}


def route(text: str, project_name: Optional[str] = None, force_model: Optional[str] = None) -> dict[str, Any]:
    """Decide where a new request goes. Returns
    {category, model_id, lane, routed_by, reason}; raises ValueError if
    nothing in the registry can take it. (The plan builder in octopus/plan.py
    adds the agent, tools and permissions on top of the same decision.)"""
    if force_model and force_model != "auto":
        m = get_model(force_model)
        if not m:
            raise ValueError(f"Unknown model '{force_model}'")
        return {"category": "manual", "model_id": m["id"], "lane": lane_for(m),
                "routed_by": "manual", "reason": f"you picked {m.get('label', m['id'])}"}

    from octopus import plan
    found = classify(text, project_name)
    choice = plan.choose_model(found["category"], text)
    if not choice["model"]:
        raise ValueError(f"No available model for '{found['category']}': " + "; ".join(choice["skipped"]))
    note = f"{found['category']}: {found['reason']}" if found["reason"] else found["category"]
    if choice["skipped"]:
        note += " · fell back from " + "; ".join(choice["skipped"])
    elif choice["waiting"]:
        note += f" · waiting for {choice['model'].get('label', choice['model']['id'])} ({choice['waiting']})"
    out = {"category": found["category"], "model_id": choice["model"]["id"], "lane": lane_for(choice["model"]),
           "routed_by": found["routed_by"], "reason": note}
    if found.get("intent"):
        out["intent"] = found["intent"]
    return out
