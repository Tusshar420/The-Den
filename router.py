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

log = logging.getLogger("claude-queue.router")

REGISTRY_PATH = Path(__file__).parent / "models.json"

_cache: dict[str, Any] = {"mtime": None, "data": None}


# ---------------------------------------------------------------- registry

def registry() -> dict[str, Any]:
    mtime = REGISTRY_PATH.stat().st_mtime
    if _cache["mtime"] != mtime:
        _cache["data"] = json.loads(REGISTRY_PATH.read_text())
        _cache["mtime"] = mtime
    return _cache["data"]


def get_model(model_id: Optional[str]) -> Optional[dict[str, Any]]:
    for m in registry().get("models", []):
        if m["id"] == model_id:
            return m
    return None


def lane_for(model_cfg: dict[str, Any]) -> str:
    return "agent" if model_cfg.get("provider") == "claude_code" else "api"


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
SIMPLE_MAX_CHARS = 160


def _rules_classify(text: str, has_project: bool) -> tuple[str, str]:
    if CODING_RE.search(text):
        return "coding", "mentions project/code actions"
    if len(text) <= SIMPLE_MAX_CHARS and "\n" not in text:
        return "simple", "short request with no project/code actions"
    return "general", "no project/code actions mentioned"


CLASSIFIER_INSTRUCTIONS = """You route requests for Octopus, a personal AI assistant. Pick exactly one category:
{categories}
Reply with only a JSON object: {{"category": "<one of: {names}>", "reason": "<max 12 words>"}}"""


def _llm_classify(text: str, project_name: Optional[str]) -> Optional[tuple[str, str]]:
    reg = registry()
    routing = reg.get("routing", {})
    cats = routing.get("categories", {})
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

def route(text: str, project_name: Optional[str] = None, force_model: Optional[str] = None) -> dict[str, Any]:
    """Decide where a new request goes. Returns
    {category, model_id, lane, routed_by, reason}; raises ValueError if
    nothing in the registry can take it."""
    if force_model and force_model != "auto":
        m = get_model(force_model)
        if not m:
            raise ValueError(f"Unknown model '{force_model}'")
        return {"category": "manual", "model_id": m["id"], "lane": lane_for(m),
                "routed_by": "manual", "reason": f"you picked {m.get('label', m['id'])}"}

    cats = registry().get("routing", {}).get("categories", {})
    llm = _llm_classify(text, project_name)
    if llm:
        category, why, routed_by = llm[0], llm[1], "classifier"
    else:
        category, why = _rules_classify(text, project_name is not None)
        routed_by = "rules"
    cat = cats.get(category) or {}

    candidates = [get_model(mid) for mid in cat.get("candidates", [])]
    candidates = [m for m in candidates if m]
    if not candidates:
        raise ValueError(f"No models configured for category '{category}' in models.json")

    skipped = []
    for i, m in enumerate(candidates):
        ok, reason, until = availability(m)
        if ok:
            note = f"{category}: {why}" if why else category
            if skipped:
                note += " · fell back from " + "; ".join(skipped)
            return {"category": category, "model_id": m["id"], "lane": lane_for(m),
                    "routed_by": routed_by, "reason": note}
        # A non-transferable category keeps its first choice through a rate
        # limit - the task queues and waits rather than moving to a model
        # that doesn't have the same tools/context.
        if i == 0 and until and not cat.get("transferable", True):
            return {"category": category, "model_id": m["id"], "lane": lane_for(m),
                    "routed_by": routed_by,
                    "reason": f"{category}: {why} · waiting for {m.get('label', m['id'])} ({reason})"}
        skipped.append(f"{m.get('label', m['id'])} ({reason})")

    raise ValueError(f"No available model for '{category}': " + "; ".join(skipped))
