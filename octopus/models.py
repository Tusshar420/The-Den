"""
Models added on the AI Providers page. They live in the settings table
("custom_models"), never in models.json, and router.registry() folds them in
- so they route, fall back and show up exactly like the models.json ones.

Only providers there is code for can be added (openai, gemini, and Claude
Code with a different --model). A model joins the categories in its
"use_for"; which categories make sense depends on the kind of model:
pictures only draw, API models can't work inside a project, and the
assistant / instant categories belong to Octopus's own tools.
"""

import re
from typing import Any, Optional

import db
import router

PROVIDERS = {
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "claude_code": "Claude Code",
    # Any server that speaks OpenAI's chat completions: Mistral, a local Ollama / LM Studio,
    # OpenRouter... Each model carries its own base_url (and its own key, in api_keys.json).
    "openai_compat": "OpenAI-compatible",
}
BASE_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$")
COMPAT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}$")
CODE_CATEGORIES = ("coding", "salesforce", "system")
OWN_TOOL_PROVIDERS = ("tools", "control")
# A name the provider's API knows it by. No "/" (it goes into a URL path) and
# no leading "-" (Claude Code gets it as a --model argument).
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


class ModelError(ValueError):
    pass


def _stored() -> list[dict[str, Any]]:
    return list(db.get_setting("custom_models", []) or [])


def is_custom(model_id: str) -> bool:
    return any(m.get("id") == model_id for m in _stored())


def categories_for(provider: str, modality: str) -> list[str]:
    """The categories a model of this kind can be added to, in models.json order."""
    reg = router.registry()
    own = {m["id"] for m in reg.get("models", []) if m.get("provider") in OWN_TOOL_PROVIDERS}
    out = []
    for name, cat in reg.get("routing", {}).get("categories", {}).items():
        cands = cat.get("candidates") or []
        if cands and all(c in own for c in cands):
            continue                         # email/calendar, instant commands
        if modality == "image":
            ok = name == "image"
        elif provider == "claude_code":
            ok = name != "image"
        else:
            ok = name != "image" and name not in CODE_CATEGORIES
        if ok:
            out.append(name)
    return out


def choices() -> dict[str, Any]:
    """What the Add a model form offers."""
    return {"providers": [{"id": p, "label": label, "modalities": ["text"] if p == "claude_code" else ["text", "image"],
                           "categories": {mod: categories_for(p, mod) for mod in (["text"] if p == "claude_code" else ["text", "image"])}}
                          for p, label in PROVIDERS.items()]}


def _rating(body: dict[str, Any], key: str, default: int, low: int) -> int:
    v = body.get(key, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= 5:
        raise ModelError(f"{key} must be a whole number from {low} to 5")
    return v


def _price(v: Any, what: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ModelError(f"{what} must be a number") from None
    if not 0 <= f <= 10000:
        raise ModelError(f"{what} must be between 0 and 10000")
    return f


def _clean(body: dict[str, Any]) -> dict[str, Any]:
    """A models.json-shaped entry from the form, or ModelError saying what's wrong."""
    label = str(body.get("label") or "").strip()
    if not label or len(label) > 60:
        raise ModelError("Give the model a name (up to 60 characters)")
    provider = body.get("provider")
    if provider not in PROVIDERS:
        raise ModelError("Pick a provider: " + ", ".join(PROVIDERS.values()))
    modality = body.get("modality") or "text"
    image = modality == "image"
    if modality not in ("text", "image") or (modality == "image" and provider == "claude_code"):
        raise ModelError("Claude Code can only be a text model" if provider == "claude_code" else "Kind must be text or image")
    model = str(body.get("model") or "").strip() or None
    if model is None and provider != "claude_code":
        raise ModelError(f"Enter the model's name as {PROVIDERS[provider]} knows it, e.g. " + ("gpt-6-nova" if provider == "openai" else "gemini-3.8-pro"))
    name_ok = (COMPAT_NAME_RE if provider == "openai_compat" else MODEL_NAME_RE).match(model) if model is not None else True
    if not name_ok:
        raise ModelError("The model name can only have letters, numbers, dots, dashes, underscores and colons")
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    if provider == "openai_compat":
        if image:
            raise ModelError("An OpenAI-compatible model can only be a text model here")
        if not BASE_URL_RE.match(base_url):
            raise ModelError("Give the server's address, e.g. http://localhost:11434/v1 or https://api.mistral.ai/v1")
    allowed = categories_for(provider, modality)
    use_for = list(dict.fromkeys(body.get("use_for") or []))
    bad = [c for c in use_for if c not in allowed]
    if bad:
        raise ModelError(f"This kind of model can't be used for {', '.join(bad)} - it can go in: {', '.join(allowed)}")
    image = modality == "image"
    context = body.get("context_tokens")
    if context in (None, ""):
        context = 0 if image else 200000 if provider == "claude_code" else 128000
    if isinstance(context, bool) or not isinstance(context, int) or not 0 <= context <= 10_000_000:
        raise ModelError("Context must be a whole number of tokens up to 10,000,000")
    note = str(body.get("note") or "").strip()[:200]
    web = bool(body.get("web_search")) and provider != "claude_code" and not image
    vision = not image and (provider == "claude_code" or bool(body.get("vision")))
    out: dict[str, Any] = {
        "label": label, "provider": provider, "model": model, "modality": modality,
        "enabled": body.get("enabled", True) is not False,
        "use_for": use_for,
        "coding": 0 if image else _rating(body, "coding", 3, 0),
        "reasoning": 0 if image else _rating(body, "reasoning", 3, 0),
        "cost": _rating(body, "cost", 3, 1), "speed": _rating(body, "speed", 3, 1),
        "vision": vision, "web_search": web, "context_tokens": context,
        "capabilities": ["image_generation"] if image else
            (["coding", "filesystem", "terminal", "git"] if provider == "claude_code" else []) + ["writing", "reasoning", "general"]
            + (["vision"] if vision else []) + (["web_search"] if web else []),
    }
    if provider == "claude_code":
        out["scarce"] = True                 # the same subscription window as Claude Code itself
    if provider == "openai_compat":
        out["base_url"] = base_url
    if image:
        out["price_per_image"] = _price(body.get("price_per_image"), "Price per picture")
    elif provider != "claude_code":
        pin, pout = _price((body.get("price_per_mtok") or {}).get("input"), "Input price"), _price((body.get("price_per_mtok") or {}).get("output"), "Output price")
        out["price_per_mtok"] = {"input": pin, "output": pout} if pin is not None and pout is not None else None
    if note:
        out["_note"] = note
    return out


def _new_id(model: dict[str, Any]) -> str:
    base = re.sub(r"[^a-z0-9.]+", "-", (model.get("model") or model["label"]).lower()).strip("-.")[:50] or "model"
    if model["provider"] == "claude_code":
        base = "claude-code-" + base if not base.startswith("claude-code") else base
    taken = {m["id"] for m in router.registry().get("models", [])}
    mid, n = base, 2
    while mid in taken:
        mid, n = f"{base}-{n}", n + 1
    return mid


def add(body: dict[str, Any]) -> dict[str, Any]:
    m = _clean(body)
    m = {"id": _new_id(m), **m}
    db.set_setting("custom_models", _stored() + [m])
    return m


def update(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    stored = _stored()
    for i, old in enumerate(stored):
        if old.get("id") == model_id:
            m = {"id": model_id, **_clean({**old, "note": old.get("_note"), **body})}
            stored[i] = m
            db.set_setting("custom_models", stored)
            return m
    raise KeyError(model_id)


def remove(model_id: str) -> None:
    stored = _stored()
    left = [m for m in stored if m.get("id") != model_id]
    if len(left) == len(stored):
        raise KeyError(model_id)
    db.set_setting("custom_models", left)
    over = db.get_setting("model_overrides", {}) or {}
    if model_id in over:
        over.pop(model_id)
        db.set_setting("model_overrides", over)
