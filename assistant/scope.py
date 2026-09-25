"""
What the personal assistant may be given at all, as configured in agents.json
and tools.json - checked by the gate (actions.invoke) on every call, and again
when an approval is decided.

The capability policies (ALLOW / ASK / DENY) say how an action is handled; this
says whether the Personal Assistant has the tool and the permission in the first
place. Take a tool out of the "personal" agent's `tools`, set `enabled: false` on
it in tools.json, or drop a capability from the agent's `permissions`, and the
assistant can no longer use it - the model is not even shown the tool.

Fails closed: if either file is missing, unreadable or has no "personal" agent,
nothing is allowed rather than everything.
"""

from typing import Iterable, Optional

from octopus import registry

AGENT_ID = "personal"

# assistant tool id (assistant/tools/) -> tools.json id
TOOL_IDS = {"email": "gmail", "calendar": "calendar", "reminders": "reminders", "agent": "coding_handoff",
            "workspace": "workspace", "ai": "ai_models"}


def _agent() -> dict:
    agent = registry.get_agent(AGENT_ID)
    if not agent:
        raise LookupError(f"agents.json has no '{AGENT_ID}' agent")
    return agent


def tool_refusal(tool_id: str) -> Optional[str]:
    """Why the Personal Assistant may not use this tool, or None if it may."""
    try:
        agent = _agent()
        entry_id = TOOL_IDS.get(tool_id, tool_id)
        entry = registry.get_tool(entry_id)
    except (OSError, ValueError, LookupError) as e:
        return f"I can't check what I'm allowed to use ({e}), so I've stopped."
    if not entry:
        return f"The '{entry_id}' tool isn't listed in tools.json."
    if not entry.get("enabled", True):
        return f"The {entry.get('label', entry_id)} tool is switched off in tools.json."
    if entry_id not in agent.get("tools", []):
        return f"The Personal Assistant hasn't been given the {entry.get('label', entry_id)} tool (agents.json)."
    return None


def capability_refusal(capabilities: Iterable[str]) -> Optional[str]:
    """Why the Personal Assistant may not exercise all of these, or None."""
    try:
        allowed = set(_agent().get("permissions", []))
    except (OSError, ValueError, LookupError) as e:
        return f"I can't check what I'm allowed to do ({e}), so I've stopped."
    missing = [c for c in capabilities if c not in allowed]
    if missing:
        return f"The Personal Assistant isn't allowed to use {', '.join(missing)} (agents.json)."
    return None


def refusal(tool_id: str, capabilities: Iterable[str]) -> Optional[str]:
    return tool_refusal(tool_id) or capability_refusal(capabilities)
