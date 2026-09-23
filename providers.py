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
import urllib.error
import urllib.request
from typing import Any, Optional


class ProviderError(Exception):
    """rate_limited=True means "try again later" (retry_after seconds if the
    provider said); anything else is a real failure - bad key, no quota,
    bad request - that retrying won't fix."""

    def __init__(self, message: str, rate_limited: bool = False, retry_after: Optional[int] = None):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.retry_after = retry_after


class Completion:
    def __init__(self, text: str, input_tokens: Optional[int] = None, output_tokens: Optional[int] = None):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def provider_ready(provider: str) -> tuple[bool, str]:
    """Whether this provider can be called at all right now (key present etc.)."""
    if provider == "claude_code":
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


# ------------------------------------------------------------------ openai

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")


def openai_complete(model: str, messages: list[dict[str, str]], instructions: Optional[str] = None,
                    timeout: int = 120) -> Completion:
    """OpenAI Responses API. `messages` is [{"role": "user"|"assistant", "content": str}, ...]."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError("OPENAI_API_KEY is not set")

    body: dict[str, Any] = {"model": model, "input": messages}
    if instructions:
        body["instructions"] = instructions

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
                    timeout: int = 120) -> Completion:
    """Gemini API generateContent. Same `messages` shape as openai_complete;
    Gemini calls the assistant role "model"."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ProviderError("GEMINI_API_KEY is not set")

    body: dict[str, Any] = {
        "contents": [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
        ],
    }
    if instructions:
        body["systemInstruction"] = {"parts": [{"text": instructions}]}

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

    usage = data.get("usageMetadata") or {}
    output = usage.get("candidatesTokenCount")
    if output is not None:
        output += usage.get("thoughtsTokenCount") or 0  # thinking is billed as output
    return Completion(text, usage.get("promptTokenCount"), output)


COMPLETERS = {
    "openai": openai_complete,
    "gemini": gemini_complete,
}


def complete(model_cfg: dict[str, Any], messages: list[dict[str, str]], instructions: Optional[str] = None,
             timeout: int = 120) -> Completion:
    fn = COMPLETERS.get(model_cfg.get("provider"))
    if not fn:
        raise ProviderError(f"No api-lane support for provider '{model_cfg.get('provider')}'")
    return fn(model_cfg.get("model") or model_cfg["id"], messages, instructions, timeout)


def estimate_cost(model_cfg: dict[str, Any], input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    price = model_cfg.get("price_per_mtok")
    if not price or input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * price.get("input", 0) + output_tokens * price.get("output", 0)) / 1_000_000, 6)
