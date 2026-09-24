"""
Context for a pending approval, so it can be decided from a phone without
opening a terminal: how risky the command looks, what the task did before
it asked, and what the project's working tree looks like right now.

Also owns the "always allowed commands" rules, because the approval server
(auto-allow) and the dashboard ("Always allow ..." button) must agree on
exactly what an allow-list entry covers.

Everything here is a heuristic read of the command text - it can't see what
a script actually does, so it errs towards saying "Medium" rather than
calling something safe.
"""

import json
import re
import shlex
import subprocess
from typing import Any, Optional

# ------------------------------------------------------------- allow list

# Anything that could chain, redirect or substitute another command. A
# command containing one of these never matches an allow-list entry, so
# "git status" can't be stretched into "git status; rm -rf ~".
SHELL_CONTROL_RE = re.compile(r"[;&|`$<>(){}\n\r\\]")

# Tools where the first argument is the real action ("npm test", "git
# status"), so an entry keeps two words instead of allowing the whole tool.
RUNNERS = {
    "npm", "npx", "pnpm", "yarn", "bun", "deno", "git", "make", "cargo", "go", "python", "python3",
    "pip", "pip3", "poetry", "uv", "sf", "sfdx", "docker", "swift", "xcodebuild", "bundle", "rake",
    "gradle", "./gradlew", "mvn", "node", "php", "composer", "dotnet", "flutter", "fastlane", "pod",
    "kubectl", "terraform", "fly", "flyctl", "vercel", "heroku", "gh", "brew",
}
# Too dangerous to allow by name alone - "always allow" on these only ever
# covers the exact command that was approved.
EXACT_ONLY = {
    "rm", "sudo", "dd", "mkfs", "chmod", "chown", "kill", "killall", "curl", "wget", "ssh", "scp",
    "rsync", "mv", "sh", "bash", "zsh", "eval", "xargs", "find", "launchctl", "osascript",
}


def _split(command: str) -> Optional[list[str]]:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def allow_entry_for(command: str) -> Optional[str]:
    """What "Always allow" stores for this command: the program (plus its
    subcommand for runners like npm/git), or the exact command for anything
    risky or ambiguous. None if the command can't be allow-listed at all."""
    if SHELL_CONTROL_RE.search(command):
        return None
    words = _split(command)
    if not words:
        return None
    exact = " ".join(shlex.quote(w) for w in words)
    prog = words[0]
    if prog in EXACT_ONLY or prog.rsplit("/", 1)[-1] in EXACT_ONLY:
        return exact
    # Anything High-risk (force-push, sudo, production...) is only ever
    # allowed as this exact command, never as a family of commands.
    if any(re.search(pattern, command, re.IGNORECASE) for pattern, _, _ in HIGH):
        return exact
    if prog in RUNNERS:
        if len(words) > 1 and not words[1].startswith("-"):
            # "npm run build" keeps the script name - "npm run" alone would
            # cover every script in package.json.
            n = 3 if words[1] in ("run", "run-script", "exec", "x", "dlx") and len(words) > 2 else 2
            return " ".join(shlex.quote(w) for w in words[:n])
        return exact
    return shlex.quote(prog)


def match_allowed(command: str, entries: list[str]) -> Optional[str]:
    """The allow-list entry that covers this command (word-prefix match), if any."""
    if SHELL_CONTROL_RE.search(command):
        return None
    words = _split(command)
    if not words:
        return None
    for entry in entries:
        ew = _split(entry)
        if ew and words[:len(ew)] == ew:
            return entry
    return None


def command_of(tool_name: str, tool_input: str) -> str:
    try:
        data = json.loads(tool_input)
    except (ValueError, TypeError):
        return tool_input or ""
    if isinstance(data, dict):
        if "command" in data:
            return str(data["command"])
        if "url" in data:
            return f"{tool_name} {data['url']}"
        if "file_path" in data:
            return f"{tool_name} {data['file_path']}"
    return f"{tool_name} {json.dumps(data)[:200]}"


# ------------------------------------------------------------------- risk

# (pattern, short tag, sentence) - checked against the command text.
HIGH = [
    (r"\bsudo\b", "sudo", "Runs with admin rights (sudo)"),
    (r"\brm\s+-[a-zA-Z]*r", "deletes", "Deletes files recursively"),
    (r"\bgit\s+push\b.*(--force\b|\s-f\b)", "force-push", "Force-pushes, rewriting remote history"),
    (r"\bgit\s+reset\s+--hard\b", "reset", "Throws away uncommitted changes"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh)\b", "curl | sh", "Pipes a download straight into a shell"),
    (r"\b(mkfs|dd)\b", "disk", "Writes raw disk data"),
    (r"\bprod(uction)?\b", "production", "Mentions production"),
    (r"\bdrop\s+(table|database)\b", "drop", "Drops a database table"),
    (r"\b(chmod|chown)\s+-R\b", "permissions", "Changes permissions recursively"),
]
MEDIUM = [
    (r"\b(curl|wget|ssh|scp|rsync|ftp|nc)\b", "network", "Talks to the network"),
    (r"\bgit\s+push\b", "push", "Pushes to a git remote"),
    (r"\b(deploy|publish|release)\b", "deploy", "Deploys or publishes something"),
    (r"\b(npm|pnpm|yarn|bun|pip3?|brew|gem|cargo|pod)\s+(install|add|i|update|upgrade)\b", "packages",
     "Installs or updates packages"),
    (r"\brm\b", "deletes", "Deletes files"),
    (r"\bdocker\b", "docker", "Runs Docker"),
    (r"\bkill(all)?\b", "processes", "Stops running processes"),
    (r"\bgit\s+(checkout|restore|clean|rebase|merge|stash)\b", "git", "Rewrites the working tree"),
]
TOOL_NOTES = {
    "WebFetch": ("network", "Fetches a web page"),
    "WebSearch": ("network", "Searches the web"),
}


def assess(tool_name: str, command: str, is_git: Optional[bool]) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    tags: list[str] = []
    level = 1

    def hit(rules, lvl):
        nonlocal level
        for pattern, tag, text in rules:
            if re.search(pattern, command, re.IGNORECASE):
                if tag not in tags:
                    tags.append(tag)
                reasons.append({"kind": "warn", "text": text})
                level = max(level, lvl)

    hit(HIGH, 3)
    hit(MEDIUM, 2)
    if tool_name in TOOL_NOTES:
        tag, text = TOOL_NOTES[tool_name]
        tags.append(tag)
        reasons.append({"kind": "warn", "text": text})
        level = max(level, 2)
    elif tool_name != "Bash":
        reasons.append({"kind": "warn", "text": f"Uses the {tool_name} tool"})
        level = max(level, 2)
    if tool_name == "Bash" and level == 1 and re.search(r"\.(sh|py|rb|js)\b|^\./", command):
        # A script could do anything; the name alone doesn't make it safe.
        reasons.append({"kind": "warn", "text": "Runs a script, so what it does depends on its contents"})
        level = 2

    if level == 1:
        reasons.append({"kind": "ok", "text": "No network, deletes, installs or pushes spotted"})
    if not re.search(r"\bsudo\b|\bprod(uction)?\b", command, re.IGNORECASE):
        reasons.append({"kind": "ok", "text": "No sudo, and no mention of production"})
    if is_git is True:
        reasons.append({"kind": "ok", "text": "The project is a git repo, so file changes can be rolled back"})
    elif is_git is False:
        reasons.append({"kind": "warn", "text": "Not a git repo, so file changes can't be rolled back easily"})
    return {"level": level, "risk": {1: "Low", 2: "Medium", 3: "High"}[level], "tags": tags, "reasons": reasons}


# ------------------------------------------------------------ working tree

def _git(path: str, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


DIFF_EXCERPT_LINES = 40


def working_tree(path: str) -> dict[str, Any]:
    """Uncommitted changes in the project (vs HEAD), plus untracked files."""
    if not path or _git(path, "rev-parse", "--is-inside-work-tree") is None:
        return {"is_git": False, "files": [], "diff": [], "summary": None}
    files = []
    for line in (_git(path, "diff", "--numstat", "HEAD") or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            a, d, name = parts
            files.append({"name": name, "added": int(a) if a.isdigit() else 0, "deleted": int(d) if d.isdigit() else 0})
    for name in (_git(path, "ls-files", "--others", "--exclude-standard") or "").splitlines()[:50]:
        files.append({"name": name, "added": 0, "deleted": 0, "untracked": True})
    diff = []
    for line in (_git(path, "diff", "HEAD", "-U1", "--no-color") or "").splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file", "similarity", "rename ")):
            continue
        kind = "hunk" if line.startswith("@@") else "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"
        diff.append({"kind": kind, "text": line[:300]})
        if len(diff) >= DIFF_EXCERPT_LINES:
            break
    summary = {"files": len(files), "added": sum(f["added"] for f in files),
               "deleted": sum(f["deleted"] for f in files)} if files else None
    return {"is_git": True, "files": files, "diff": diff, "summary": summary}


def approval_context(approval: dict[str, Any], project: Optional[dict[str, Any]],
                     events: list[dict[str, Any]], allow_entries: list[str]) -> dict[str, Any]:
    command = command_of(approval["tool_name"], approval["tool_input"])
    path = (project or {}).get("path") or ""
    tree = working_tree(path)
    a = assess(approval["tool_name"], command, tree["is_git"] if path else None)
    steps = [{"text": e["text"], "state": "done"} for e in events if e["kind"] == "tool"]
    steps.append({"text": "Waiting for your OK: " + command[:160], "state": "current"})
    entry = allow_entry_for(command) if approval["tool_name"] == "Bash" else None
    return {
        **a,
        "command": command,
        "tool_name": approval["tool_name"],
        "cwd": path,
        "steps": steps,
        "step_no": len(steps),
        "allow_entry": entry,
        "already_allowed": bool(entry and match_allowed(command, allow_entries)),
        **tree,
    }
