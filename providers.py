"""
Plain model calls for Octopus's api lane - anything that isn't Claude Code
running inside a project folder.

Deliberately stdlib-only (urllib), matching the rest of this project on
the Mac's Python 3.9: one small function per provider, all returning the
same Completion shape so worker.py and router.py don't care which provider
answered. Add a provider by writing another *_complete function and
registering it in COMPLETERS.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import uploads


class ProviderError(Exception):
    """rate_limited=True means "try again later" (retry_after seconds if the
    provider said); anything else is a real failure - bad key, no quota,
    bad request - that retrying won't fix."""

    def __init__(self, message: str, rate_limited: bool = False, retry_after: Optional[int] = None):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.retry_after = retry_after


# Provider-side overload/outage ("high demand, try again later") - not the
# request's fault, so it's retried like a short rate limit rather than
# marked as failed.
TRANSIENT_HTTP_CODES = (500, 502, 503, 504)
TRANSIENT_RETRY_SECONDS = 30


class Completion:
    def __init__(self, text: str, input_tokens: Optional[int] = None, output_tokens: Optional[int] = None):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def provider_ready(provider: str) -> tuple[bool, str]:
    """Whether this provider can be called at all right now (key present etc.)."""
    if provider in ("claude_code", "tools", "control"):
        return True, ""
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY is not set"
        return True, ""
    if provider == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            return False, "GEMINI_API_KEY is not set"
        return True, ""
    return False, f"unknown provider '{provider}'"


def _pictures(message: dict[str, Any]) -> list[tuple[str, str]]:
    """(mime, base64) for each picture attached to a message (its "images" upload ids)."""
    out = []
    for image_id in message.get("images") or []:
        try:
            out.append(uploads.read_base64(image_id))
        except OSError:
            raise ProviderError("An attached picture is no longer on this Mac - attach it again")
    return out


def _openai_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        pics = _pictures(m)
        content: Any = m["content"]
        if pics:
            content = [{"type": "input_text", "text": m["content"]}] + [
                {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"} for mime, b64 in pics]
        out.append({"role": m["role"], "content": content})
    return out


def _gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": "model" if m["role"] == "assistant" else "user",
         "parts": [{"text": m["content"]}] + [{"inlineData": {"mimeType": mime, "data": b64}} for mime, b64 in _pictures(m)]}
        for m in messages
    ]


# ------------------------------------------------------------------ openai

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")


def openai_complete(model: str, messages: list[dict[str, str]], instructions: Optional[str] = None,
                    timeout: int = 120, web: bool = False) -> Completion:
    """OpenAI Responses API. `messages` is [{"role": "user"|"assistant", "content": str}, ...].
    web=True lets the model search the web (the Research Agent's tool)."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError("OPENAI_API_KEY is not set")

    body: dict[str, Any] = {"model": model, "input": _openai_input(messages)}
    if instructions:
        body["instructions"] = instructions
    if web:
        body["tools"] = [{"type": "web_search"}]

    req = urllib.request.Request(
        f"{OPENAI_BASE_URL}/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(raw).get("error") or {}
        except ValueError:
            err = {}
        msg = err.get("message") or raw[:500] or str(e)
        # 429 is both "slow down" and "out of credit" - only the first is
        # worth waiting out; an empty balance needs a human.
        if e.code == 429 and err.get("code") != "insufficient_quota":
            retry_after = e.headers.get("retry-after")
            raise ProviderError(f"OpenAI rate limit: {msg}", rate_limited=True,
                                retry_after=int(float(retry_after)) if retry_after else None)
        if e.code in TRANSIENT_HTTP_CODES:
            raise ProviderError(f"OpenAI temporarily unavailable ({e.code}): {msg}", rate_limited=True,
                                retry_after=TRANSIENT_RETRY_SECONDS)
        raise ProviderError(f"OpenAI error {e.code}: {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Could not reach OpenAI: {e}")

    text = data.get("output_text") or ""
    if not text:
        parts = []
        for item in data.get("output") or []:
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        parts.append(c.get("text", ""))
        text = "".join(parts)
    if not text and data.get("status") == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason", "unknown")
        raise ProviderError(f"OpenAI response incomplete ({reason})")

    usage = data.get("usage") or {}
    return Completion(text, usage.get("input_tokens"), usage.get("output_tokens"))


# ------------------------------------------------------------------ gemini

GEMINI_BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")


def _gemini_retry_after(err: dict[str, Any]) -> Optional[int]:
    # 429s carry a google.rpc.RetryInfo detail like {"retryDelay": "33s"}.
    for d in err.get("details") or []:
        delay = str(d.get("retryDelay") or "")
        if delay.endswith("s"):
            try:
                return int(float(delay[:-1]))
            except ValueError:
                pass
    return None


def gemini_complete(model: str, messages: list[dict[str, str]], instructions: Optional[str] = None,
                    timeout: int = 120, web: bool = False) -> Completion:
    """Gemini API generateContent. Same `messages` shape as openai_complete;
    Gemini calls the assistant role "model". web=True grounds the answer in a
    Google search and lists the sources it used."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ProviderError("GEMINI_API_KEY is not set")

    body: dict[str, Any] = {"contents": _gemini_contents(messages)}
    if instructions:
        body["systemInstruction"] = {"parts": [{"text": instructions}]}
    if web:
        body["tools"] = [{"google_search": {}}]

    req = urllib.request.Request(
        f"{GEMINI_BASE_URL}/models/{model}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(raw).get("error") or {}
        except ValueError:
            err = {}
        msg = err.get("message") or raw[:500] or str(e)
        # The free tier answers 429 both per-minute and once the day's
        # quota is used up - either way, wait it out and let the router
        # use the next model meanwhile.
        if e.code == 429:
            raise ProviderError(f"Gemini rate limit: {msg}", rate_limited=True,
                                retry_after=_gemini_retry_after(err))
        if e.code in TRANSIENT_HTTP_CODES:
            raise ProviderError(f"Gemini temporarily unavailable ({e.code}): {msg}", rate_limited=True,
                                retry_after=_gemini_retry_after(err) or TRANSIENT_RETRY_SECONDS)
        raise ProviderError(f"Gemini error {e.code}: {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Could not reach Gemini: {e}")

    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
        raise ProviderError(f"Gemini returned no answer ({reason})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    # Thinking models can include thought-summary parts - only the answer counts.
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise ProviderError(f"Gemini returned an empty answer ({candidates[0].get('finishReason', 'unknown')})")
    if web:
        seen, sources = set(), []
        for chunk in (candidates[0].get("groundingMetadata") or {}).get("groundingChunks") or []:
            w = chunk.get("web") or {}
            if w.get("uri") and w["uri"] not in seen:
                seen.add(w["uri"])
                sources.append(f"- {w.get('title') or w['uri']}: {w['uri']}")
        if sources:
            text += "\n\nSources:\n" + "\n".join(sources[:8])

    usage = data.get("usageMetadata") or {}
    output = usage.get("candidatesTokenCount")
    if output is not None:
        output += usage.get("thoughtsTokenCount") or 0  # thinking is billed as output
    return Completion(text, usage.get("promptTokenCount"), output)


# ------------------------------------------------------------- claude code

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_TEXT_CWD = Path(__file__).parent / "workspace" / "assistant"
_LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "hit your limit")


def claude_cli_complete(model: Optional[str], messages: list[dict[str, str]], instructions: Optional[str] = None,
                        timeout: int = 120) -> Completion:
    """A plain text completion through the local Claude Code CLI, for when no
    API-key model is configured. Every tool is switched off (--tools ""), so
    this can read and write nothing: it is a chat model here, not the agent.
    Runs from an empty folder so no project context leaks in, and without a
    saved session."""
    if any(m.get("images") for m in messages):
        raise ProviderError("Claude Code can't read pictures in a plain answer - send it as a coding task or pick a vision model")
    parts = []
    for m in messages:
        parts.append(("Owner" if m["role"] == "user" else "You") + ":\n" + m["content"])
    prompt = "\n\n".join(parts) if len(messages) > 1 else messages[0]["content"]
    cmd = [CLAUDE_BIN, "-p", prompt, "--tools", "", "--output-format", "json", "--no-session-persistence",
           "--disable-slash-commands", "--strict-mcp-config"]
    if instructions:
        cmd += ["--system-prompt", instructions]
    if model:
        cmd += ["--model", model]
    CLAUDE_TEXT_CWD.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(cmd, cwd=str(CLAUDE_TEXT_CWD), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise ProviderError(f"Could not find '{CLAUDE_BIN}'. Set CLAUDE_BIN to its full path.")
    except subprocess.TimeoutExpired:
        raise ProviderError("Claude Code took too long to answer", rate_limited=True, retry_after=60)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        data = {}
    text = str(data.get("result") or "")
    if r.returncode != 0 or data.get("is_error"):
        detail = (text or r.stderr or r.stdout)[-400:]
        if any(w in detail.lower() for w in _LIMIT_WORDS):
            raise ProviderError(f"Claude usage limit: {detail}", rate_limited=True, retry_after=900)
        raise ProviderError(f"Claude Code failed: {detail}")
    if not text:
        raise ProviderError("Claude Code returned an empty answer")
    usage = data.get("usage") or {}
    return Completion(text, (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                      + (usage.get("cache_creation_input_tokens") or 0) if usage else None, usage.get("output_tokens"))


COMPLETERS = {
    "openai": openai_complete,
    "gemini": gemini_complete,
    "claude_code": claude_cli_complete,
}


def complete(model_cfg: dict[str, Any], messages: list[dict[str, str]], instructions: Optional[str] = None,
             timeout: int = 120, web: bool = False) -> Completion:
    fn = COMPLETERS.get(model_cfg.get("provider"))
    if not fn:
        raise ProviderError(f"No api-lane support for provider '{model_cfg.get('provider')}'")
    # claude_code with no "model" means the CLI's own default - its id ("claude-code") is not a model name.
    model = model_cfg.get("model") if model_cfg.get("provider") == "claude_code" else (model_cfg.get("model") or model_cfg["id"])
    if web and model_cfg.get("provider") in ("openai", "gemini"):
        return fn(model, messages, instructions, timeout, web=True)
    return fn(model, messages, instructions, timeout)


def estimate_cost(model_cfg: dict[str, Any], input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    if model_cfg.get("modality") == "image":
        return model_cfg.get("price_per_image")
    price = model_cfg.get("price_per_mtok")
    if not price or input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * price.get("input", 0) + output_tokens * price.get("output", 0)) / 1_000_000, 6)


# ------------------------------------------------------------------ images
# The Creative Agent's models. A picture is saved under workspace/images and the
# answer is a markdown image pointing at it (served by GET /octopus/images/{name}).

IMAGES_DIR = Path(__file__).parent / "workspace" / "images"


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int, who: str) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(raw).get("error") or {}
        except ValueError:
            err = {}
        msg = err.get("message") or raw[:500] or str(e)
        if e.code == 429 and err.get("code") != "insufficient_quota":
            raise ProviderError(f"{who} rate limit: {msg}", rate_limited=True, retry_after=_gemini_retry_after(err))
        if e.code in TRANSIENT_HTTP_CODES:
            raise ProviderError(f"{who} temporarily unavailable ({e.code}): {msg}", rate_limited=True,
                                retry_after=TRANSIENT_RETRY_SECONDS)
        raise ProviderError(f"{who} error {e.code}: {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Could not reach {who}: {e}")


def _save_image(b64: str, mime: str, prompt: str) -> str:
    import base64
    import re
    import time
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:40].strip("-") or "image"
    name = f"{int(time.time())}-{slug}.{ext}"
    (IMAGES_DIR / name).write_bytes(base64.b64decode(b64))
    return name


def generate_image(model_cfg: dict[str, Any], prompt: str, timeout: int = 180) -> Completion:
    """Make a picture with an image model (openai or gemini)."""
    provider, model = model_cfg.get("provider"), model_cfg.get("model") or model_cfg["id"]
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set")
        data = _post_json(f"{OPENAI_BASE_URL}/images/generations", {"model": model, "prompt": prompt, "size": "1024x1024", "n": 1},
                          {"Authorization": f"Bearer {key}"}, timeout, "OpenAI")
        item = (data.get("data") or [{}])[0]
        b64, mime = item.get("b64_json"), "image/png"
    elif provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ProviderError("GEMINI_API_KEY is not set")
        data = _post_json(f"{GEMINI_BASE_URL}/models/{model}:generateContent",
                          {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                           "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
                          {"x-goog-api-key": key}, timeout, "Gemini")
        b64, mime = None, "image/png"
        for part in ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                b64, mime = inline["data"], inline.get("mimeType") or inline.get("mime_type") or mime
                break
    else:
        raise ProviderError(f"No image support for provider '{provider}'")
    if not b64:
        raise ProviderError(f"{model_cfg.get('label', model)} returned no image")
    name = _save_image(b64, mime, prompt)
    return Completion(f"![{prompt[:80]}](/octopus/images/{name})\n\nMade with {model_cfg.get('label', model)}.")
