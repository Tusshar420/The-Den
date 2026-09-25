"""
The agent and tool registries: agents.json (who does the work) and tools.json
(what they can be given). Both are plain data, re-read whenever the file
changes on disk, so adding an agent or switching a tool off never needs a code
change or a restart - same as models.json.
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
AGENTS_PATH = ROOT / "agents.json"
TOOLS_PATH = ROOT / "tools.json"

_cache: dict[str, dict[str, Any]] = {}


def _load(path: Path) -> dict[str, Any]:
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if not hit or hit["mtime"] != mtime:
        hit = _cache[str(path)] = {"mtime": mtime, "data": json.loads(path.read_text())}
    return hit["data"]


def agents() -> list[dict[str, Any]]:
    return _load(AGENTS_PATH).get("agents", [])


def get_agent(agent_id: Optional[str]) -> Optional[dict[str, Any]]:
    return next((a for a in agents() if a["id"] == agent_id), None)


def agent_for_category(category: str) -> Optional[dict[str, Any]]:
    return next((a for a in agents() if category in a.get("categories", [])), None)


def tools() -> list[dict[str, Any]]:
    return _load(TOOLS_PATH).get("tools", [])


def get_tool(tool_id: str) -> Optional[dict[str, Any]]:
    return next((t for t in tools() if t["id"] == tool_id), None)


def select_tools(agent: dict[str, Any], category: str, text: str) -> list[dict[str, Any]]:
    """Only the tools this request needs, out of the ones the agent may ever
    have: enabled, allowed for the agent, and either always used for this
    category or mentioned by the request. Never "every tool for every task"."""
    allowed = set(agent.get("tools", []))
    chosen = []
    for t in tools():
        if t["id"] not in allowed or not t.get("enabled", True):
            continue
        when = t.get("when") or {}
        if category in when.get("categories", []) or any(re.search(k, text, re.IGNORECASE) for k in when.get("keywords", [])):
            chosen.append(t)
    return chosen


def tool_summary(t: dict[str, Any]) -> dict[str, Any]:
    return {"id": t["id"], "label": t.get("label", t["id"]), "kind": t.get("kind", "builtin"),
            "capabilities": t.get("capabilities", [])}
