"""
The app's AI models as a tool: the personal assistant can hand a piece of work
to the same models the rest of Octopus routes to - a long piece of writing, a
web-grounded answer, an image - instead of only planning tool calls itself.

Nothing here has its own list of models or keys. Which model answers is decided
the way the Ask box decides it: the category's candidates in models.json, picked
by octopus/plan.choose_model, skipping any that are switched off (also from the
AI Providers page), missing their API key, or rate-limited. A model the owner
names is used as asked or the call fails - it is never quietly swapped. The
answering "agent" (agents.json) supplies the instructions and the per-request
cost ceiling, and the project's monthly budget is honoured.

The prompt is written by the planner, so it can contain what the planner has read
(an email to summarise). It leaves this Mac for the model's provider, which is why
the capability (AI_MODEL_USE) exists and can be set to ask or deny. A model's answer
is derived from that prompt, so it is fenced as untrusted like an email body.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import db
import providers as llm
import router
from assistant.tools import ActionSpec, Prepared, Tool, ToolError, preview, register
from octopus import plan, registry

KINDS = {"general": "a question, explanation or analysis", "writing": "a piece of writing, rewrite, summary or translation",
         "research": "a web-grounded answer with sources"}
TEXT_TIMEOUT_SECONDS = 120
IMAGE_TIMEOUT_SECONDS = 180
MAX_PROMPT_CHARS = 12000
ANSWER_CHARS_FOR_MODEL = 5000
DEFAULT_INSTRUCTIONS = ("You are Octopus, Tushar's personal AI assistant. Answer directly and clearly. You cannot read files, "
                        "run commands or change anything.")


def _usable(m: dict[str, Any], image: bool) -> bool:
    is_image = m.get("modality") == "image"
    return (router.lane_for(m) == "api" and is_image == image) or (not image and m.get("provider") == "claude_code")


def _named(name: str, image: bool) -> dict[str, Any]:
    """The model the owner (via the planner) named: an id, a label, or an unambiguous part of one."""
    wanted = name.strip().lower()
    models = [m for m in router.registry().get("models", []) if _usable(m, image)]
    exact = [m for m in models if wanted in (m["id"].lower(), str(m.get("label", "")).lower())]
    found = exact or [m for m in models if wanted and (wanted in m["id"].lower() or wanted in str(m.get("label", "")).lower())]
    if len(found) == 1:
        return found[0]
    ids = ", ".join(m["id"] for m in models) or "none"
    raise ToolError(f"I couldn't tell which {'image ' if image else ''}model you mean by '{name}'. Available: {ids}.")


def model_rows() -> list[dict[str, Any]]:
    """Every model the assistant could ask, usable or not - shared with GET /assistant/status,
    so what the dashboard reports is what the assistant sees."""
    out = []
    for m in router.models_status():
        if m["lane"] != "api" and m.get("provider") != "claude_code":
            continue
        out.append({"id": m["id"], "label": m.get("label", m["id"]), "for": "images" if m.get("modality") == "image" else "text",
                    "available": m["available"], "why_not": m["unavailable_reason"] or None,
                    "web_search": bool(m.get("web_search")), "cost": m.get("cost"), "speed": m.get("speed")})
    return out


def _instructions(kind: str) -> str:
    agent = registry.agent_for_category(kind) or registry.agent_for_category("general") or {}
    return agent.get("instructions") or DEFAULT_INSTRUCTIONS


def _cost_ceiling(kind: str) -> Optional[float]:
    agent = registry.agent_for_category(kind) or {}
    return (agent.get("execution") or {}).get("max_cost_usd")


def _check_budget(ctx) -> None:
    """The same monthly project budget the api lane stops at. Read-only here: the worker owns pausing projects."""
    project_id = (ctx.task or {}).get("project_id")
    if not project_id:
        return
    budget = db.get_project_settings(project_id)["budget_usd"]
    if budget and budget > 0 and db.project_month_spend(project_id) >= budget:
        raise ToolError("This project has reached its monthly AI budget, so I haven't called a model. "
                        "Raise the budget on the dashboard to continue.")


def _pick(kind: str, prompt: str, wanted: Optional[str], image: bool) -> dict[str, Any]:
    if wanted:
        model = _named(wanted, image)
        ok, why, _ = router.availability(model)
        if not ok:
            raise ToolError(f"{model.get('label', model['id'])} isn't available right now ({why}).")
        return model
    choice = plan.choose_model(kind, prompt)
    if not choice["model"]:
        raise ToolError(f"No model is available for {kind} work right now: " + ("; ".join(choice["skipped"]) or "none configured") + ".")
    return choice["model"]


def _guard_cost(model: dict[str, Any], prompt: str, kind: str) -> Optional[float]:
    est = plan.estimate_cost(model, prompt, kind)
    ceiling = _cost_ceiling(kind)
    if est is not None and ceiling and est > ceiling:
        raise ToolError(f"That would cost about ${est:.2f} on {model.get('label', model['id'])}, over the ${ceiling:.2f} limit for {kind} "
                        "requests. Ask for something shorter, or run it from the Ask box where you can review the plan.")
    return est


def _card(what: str, model: dict[str, Any], kind: str, prompt: str, est: Optional[float], web: bool) -> dict[str, Any]:
    fields = [("Model", model.get("label", model["id"])), ("Kind", kind), ("Prompt", prompt),
              ("Estimated cost", f"about ${est:.3f}" if est else "free or unknown")]
    warnings = ["The prompt is sent to the model's provider."] + (["It will search the web."] if web else [])
    return preview("ai", "AI MODEL APPROVAL", what, fields, "Send", warnings, blocks=("Prompt",))


class AiTool(Tool):
    id = "ai"
    label = "AI models"
    actions = {
        "models": ActionSpec("models", "Which of the app's AI models can be used right now, and what each is for."),
        "ask": ActionSpec("ask", "Send a self-contained prompt to one of the app's AI models and get its answer. Use it for long or "
                          "specialist writing, translation, analysis, or a web-grounded answer with sources - not for the owner's own "
                          "email, calendar or queue (use those tools). The model sees only this prompt, so include everything it needs.",
                          {"prompt": "the complete request", "kind?": "general (default) | writing | research (searches the web)",
                           "model?": "a model id or name from ai.models; leave out to let Octopus choose the best available"},
                          reads_untrusted=True),
        "image": ActionSpec("image", "Generate a picture with one of the app's image models.",
                            {"prompt": "what the picture should show", "model?": "an image model id from ai.models; leave out to let Octopus choose"}),
    }

    def prepare_models(self, args, ctx):
        return Prepared({}, ["AI_MODEL_USE"], "models", "Checking which AI models are available")

    def run_models(self, args, ctx):
        out = model_rows()
        ready = [m for m in out if m["available"]]
        if not ready:
            return {"text": "No AI models are available right now. Check the AI Providers page.", "data": {"models": out}}
        lines = ["Available now:"] + [f"• {m['label']} ({m['id']}) - {m['for']}{', web search' if m['web_search'] else ''}" for m in ready]
        down = [m for m in out if not m["available"]]
        if down:
            lines += ["Not available:"] + [f"• {m['label']} - {m['why_not']}" for m in down]
        return {"text": "\n".join(lines), "data": {"models": out}}

    # ------------------------------------------------------------------ ask

    def prepare_ask(self, args, ctx):
        prompt = str(args["prompt"]).strip()
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ToolError(f"That prompt is too long to send ({len(prompt):,} characters; the limit is {MAX_PROMPT_CHARS:,}).")
        kind = str(args.get("kind") or "general").strip().lower()
        if kind not in KINDS:
            raise ToolError(f"kind must be one of: {', '.join(KINDS)}")
        _check_budget(ctx)
        model = _pick(kind, prompt, args.get("model"), image=False)
        est = _guard_cost(model, prompt, kind)
        web = kind == "research" and bool(model.get("web_search"))
        label = model.get("label", model["id"])
        caps = ["AI_MODEL_USE"] + (["WEB_ACCESS"] if web else [])
        return Prepared({"prompt": prompt, "kind": kind, "model_id": model["id"], "web": web}, caps, f"{model['id']} ({kind})",
                        f"Ask {label} ({kind})", _card("Ask an AI model", model, kind, prompt, est, web))

    def run_ask(self, args, ctx):
        model = router.get_model(args["model_id"])
        ok, why, _ = router.availability(model) if model else (False, "it was removed from models.json", None)
        if not ok:
            raise ToolError(f"{(model or {}).get('label', args['model_id'])} isn't available any more ({why}).")
        label = model.get("label", model["id"])
        try:
            done = llm.complete(model, [{"role": "user", "content": args["prompt"]}], _instructions(args["kind"]),
                                TEXT_TIMEOUT_SECONDS, **({"web": True} if args["web"] else {}))
        except llm.ProviderError as e:
            if e.rate_limited and model["id"] != "claude-code":  # claude-code's limit is derived from its own tasks
                until = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after or 60)
                db.set_model_rate_limited(model["id"], until.isoformat(), str(e))
            raise ToolError(f"{label} couldn't answer: {e}")
        ctx.add_usage(model["id"], done.input_tokens, done.output_tokens, llm.estimate_cost(model, done.input_tokens, done.output_tokens))
        answer = done.text.strip()
        shown = answer[:ANSWER_CHARS_FOR_MODEL]
        return {"text": f"{answer}\n\n— {label}", "ref": model["id"],
                "data": {"model": model["id"], "answer": shown, "shortened": len(answer) > len(shown)}}

    # ---------------------------------------------------------------- image

    def prepare_image(self, args, ctx):
        prompt = str(args["prompt"]).strip()
        if len(prompt) > 4000:
            raise ToolError("That image description is too long (4,000 characters at most).")
        _check_budget(ctx)
        model = _pick("image", prompt, args.get("model"), image=True)
        est = _guard_cost(model, prompt, "image")
        return Prepared({"prompt": prompt, "model_id": model["id"]}, ["AI_MODEL_USE", "IMAGE_GENERATE"], f"{model['id']} (image)",
                        f"Make an image with {model.get('label', model['id'])}",
                        _card("Generate an image", model, "image", prompt, est, False))

    def run_image(self, args, ctx):
        model = router.get_model(args["model_id"])
        ok, why, _ = router.availability(model) if model else (False, "it was removed from models.json", None)
        if not ok:
            raise ToolError(f"{(model or {}).get('label', args['model_id'])} isn't available any more ({why}).")
        try:
            done = llm.generate_image(model, args["prompt"], IMAGE_TIMEOUT_SECONDS)
        except llm.ProviderError as e:
            if e.rate_limited:
                until = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after or 60)
                db.set_model_rate_limited(model["id"], until.isoformat(), str(e))
            raise ToolError(f"{model.get('label', model['id'])} couldn't make that image: {e}")
        ctx.add_usage(model["id"], None, None, llm.estimate_cost(model, None, None))
        link = re.search(r"\(([^)]+)\)", done.text)
        return {"text": done.text, "ref": model["id"], "data": {"model": model["id"], "image": link.group(1) if link else None}}


register(AiTool())
