"""
Task names and summaries, both written by the cheapest answer-only model.

Every task is created with a quick name cut from its prompt (db.quick_title)
so it never shows up nameless. namer_loop then asks a model for a proper
short name for each new chat in the background - and works through older
chats the same way, newest first. If no model is available the quick name
simply stays.

summarize_thread is the dashboard's Summarise button: what was asked, what
got done, where it stands. It's cached on the root task until the thread
changes.
"""

import asyncio
import logging
import re
from typing import Any, Optional

import db
import providers
import router

log = logging.getLogger("the-den.titles")

NAMER_BUSY_SECONDS = 3  # between chats while there's a backlog to name
NAMER_IDLE_SECONDS = 10
NAMER_BACKOFF_SECONDS = 300  # no model, bad key, rate limit...

TITLE_INSTRUCTIONS = (
    "You name chats for Octopus, a personal AI assistant. Given the user's first message, reply with a short title "
    "of 2 to 6 words that says what the task is about, like a chat title in a messaging app - e.g. "
    "\"Personal assistant layer for Octopus\", \"Fix flaky login test\", \"Email Rahul about proposal\". "
    "Sentence case, no quotes, no emoji, no trailing punctuation. Keep names of people, products and files as "
    "written. Reply with the title only."
)

SUMMARY_INSTRUCTIONS = (
    "You summarise one task the user gave Octopus, their AI assistant, so they can catch up at a glance on their "
    "phone. The transcript has the user's messages, Octopus's replies and the steps it took. Write plain text - "
    "no markdown, no headings, no bold. Use this shape:\n"
    "One or two sentences on what this task is and the outcome so far.\n\n"
    "What you asked:\n• ...\n\n"
    "What was done:\n• ...\n\n"
    "Where it stands: one or two sentences - finished, still running, failed (and why), or waiting on the user "
    "(and for what).\n\n"
    "Keep it under 180 words, address the user as \"you\", and use only facts from the transcript - if something "
    "isn't there, don't guess."
)
SUMMARY_MAX_CHARS = 40_000
EVENTS_PER_MESSAGE = 40


def _clean_title(text: str) -> Optional[str]:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return None
    title = re.sub(r"^(title\s*:\s*)", "", lines[0].strip(), flags=re.IGNORECASE)
    title = title.strip(" \"'`*_#").rstrip(".!?:;, ")
    if not 2 <= len(title) <= 80:
        return None
    return title[0].upper() + title[1:]


def name_one(task: dict[str, Any], model: dict[str, Any]) -> None:
    """Name one chat. Raises providers.ProviderError if the model call fails."""
    out = providers.complete(model, [{"role": "user", "content": task["prompt"][:4000]}], TITLE_INSTRUCTIONS, 20)
    title = _clean_title(out.text)
    if title:
        db.set_thread_title(task["id"], title, "ai", only_if_by="text")
    else:
        log.warning("Namer returned an unusable title for task %s: %r", task["id"], out.text[:200])
        db.set_thread_title(task["id"], None, "kept", only_if_by="text")


async def namer_loop(stop_event: asyncio.Event) -> None:
    log.info("Namer started")
    while not stop_event.is_set():
        wait = NAMER_IDLE_SECONDS
        try:
            todo = db.tasks_needing_titles(limit=1)
            if todo:
                model = router.cheapest_api_model()
                if not model:
                    wait = NAMER_BACKOFF_SECONDS
                else:
                    await asyncio.to_thread(name_one, todo[0], model)
                    wait = NAMER_BUSY_SECONDS
        except providers.ProviderError as e:
            log.warning("Namer: model call failed, trying again later: %s", e)
            wait = NAMER_BACKOFF_SECONDS
        except Exception:
            log.exception("Namer tick failed")
            wait = NAMER_BACKOFF_SECONDS
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


# ------------------------------------------------------------------ summary

def _transcript(msgs: list[dict[str, Any]]) -> str:
    events = db.list_task_events([m["id"] for m in msgs])
    parts = []
    for m in msgs:
        parts.append(f"USER ({m['created_at'][:16].replace('T', ' ')}):\n{m['prompt']}")
        steps = events.get(m["id"]) or []
        if steps:
            shown = steps[-EVENTS_PER_MESSAGE:]
            head = f"(first {len(steps) - len(shown)} steps omitted)\n" if len(steps) > len(shown) else ""
            parts.append("OCTOPUS STEPS:\n" + head + "\n".join(f"- {e['text'][:300]}" for e in shown))
        status = m["status"]
        if m.get("result_summary"):
            parts.append(f"OCTOPUS REPLY ({status}):\n{m['result_summary']}")
        elif m.get("error_message"):
            parts.append(f"OCTOPUS ({status}): {m['error_message']}")
        else:
            parts.append(f"OCTOPUS: [{status}]")
    text = "\n\n".join(parts)
    if len(text) > SUMMARY_MAX_CHARS:
        # Keep the start (the original ask) and the end (where it stands).
        half = SUMMARY_MAX_CHARS // 2
        text = text[:half] + "\n\n[... middle of the conversation omitted ...]\n\n" + text[-half:]
    return text


def summarize_thread(task_id: int, refresh: bool = False) -> dict[str, Any]:
    """{summary, title, cached, model_id}. Raises LookupError if the task
    doesn't exist, RuntimeError if no model can summarise right now,
    providers.ProviderError if the call fails."""
    msgs = db.get_thread(task_id)
    if not msgs:
        raise LookupError("Task not found")
    root, latest = msgs[0], msgs[-1]
    n_events = sum(len(v) for v in db.list_task_events([m["id"] for m in msgs]).values())
    key = f"{latest['id']}:{latest['status']}:{n_events}"
    if not refresh and root.get("summary") and root.get("summary_key") == key:
        return {"summary": root["summary"], "title": root.get("title"), "cached": True, "model_id": None}

    model = router.cheapest_api_model()
    if not model:
        raise RuntimeError("No answer-only model is available to summarise with")
    out = providers.complete(model, [{"role": "user", "content": _transcript(msgs)}], SUMMARY_INSTRUCTIONS, 60)
    summary = out.text.strip()
    if not summary:
        raise providers.ProviderError("The model returned an empty summary")
    db.set_thread_summary(root["id"], summary, key)
    return {"summary": summary, "title": root.get("title"), "cached": False, "model_id": model["id"]}
