#!/usr/bin/env python3
"""
Builds the two download files for a colleague: dist/TheDen-Windows.zip and dist/TheDen-Mac.zip.

    python3 installer/build.py            build both
    python3 installer/build.py --only mac
    python3 installer/build.py --exclude 'modules/den_collab/'    leave one more thing out (repeatable)

What goes in: every file git knows about, plus new files git hasn't seen yet but isn't told to ignore
(so work you haven't committed is included). What stays out: your own data, keys and logins, backups,
tests, design files and scratch folders - see EXCLUDE below. The build then searches every file it is
about to pack for keys and tokens, and stops (writes nothing) if it finds one.

Stdlib only. Safe to run again: it replaces the two zip files each time.
"""

import argparse
import fnmatch
import plistlib
import re
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# (pattern, why). A pattern ending in "/" is a folder (at any depth); anything else is matched against the
# whole path, and, when it has no "/", against the file name too.
EXCLUDE = [
    ("queue.db*", "the owner's database"),
    ("*.bak", "backup copies"), ("*.bak-*", "backup copies"), ("*.bak.*", "backup copies"),
    ("accounts.json*", "Claude account tokens"), ("login.json*", "the owner's login"), ("api_keys.json*", "API keys"),
    ("den.env", "an install's own settings"), ("den-keys.json", "an install's own keys"), ("mcp-config.local.json", "an install's own copy"),
    ("restart_held.json", "runtime state"), ("restart_requested.json", "runtime state"),
    (".den-stop", "runtime state"), (".den-service.lock", "runtime state"),
    ("workspace/", "the owner's projects and files"), ("backups/", "database backups"), ("vault/", "the owner's private vault"),
    ("documents/", "the owner's documents"), ("uploads/", "the owner's uploads"), ("static/project-images/", "the owner's pictures"),
    ("releases/", "published releases"), (".den-update/", "update scratch space"),
    (".venv/", "Python environments"), (".approval-venv/", "Python environments"), ("venv/", "Python environments"),
    (".git/", "git history"), (".gitignore", "git settings"), (".claude/", "editor settings"),
    (".scratch*/", "scratch folders"), ("__pycache__/", "compiled caches"), ("*.pyc", "compiled caches"), (".DS_Store", "Mac clutter"),
    ("*.log", "logs"),
    ("tests/", "tests"), ("data/", "design prototypes"), ("design_handoff*/", "design files"), ("prompts/", "the owner's prompts"),
    ("docs/DEN_UI_HANDOVER.*", "design files"), ("Tushars-Workspace-Overview.pdf", "the owner's overview"),
    ("setup/*.plist", "the owner's launchd file (holds his key)"), ("CLAUDE.md", "the owner's coding rules"),
    ("dist/", "build output"), ("installer/", "added back per system below"), (".releaseignore", "release settings"),
]

SECRET_PATTERNS = [
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("API_KEY with a value", re.compile(r"API_KEY[\"']?\s*[=:]\s*[\"']?[A-Fa-f0-9]{24,}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
]
MAX_SCAN_BYTES = 8 * 1024 * 1024
EXEC_NAMES = (".command", ".sh")


def excluded_by(path: str, rules: list[tuple[str, str]] = EXCLUDE) -> Optional[str]:
    """Why this file (a "/"-separated path from the project root) stays out, or None if it goes in."""
    parts = path.split("/")
    for pattern, why in rules:
        if pattern.endswith("/"):
            body = pattern[:-1]
            for i in range(1, len(parts)):          # every folder the file sits in
                folder = "/".join(parts[:i])
                if fnmatch.fnmatchcase(folder if "/" in body else parts[i - 1], body):
                    return why
        elif fnmatch.fnmatchcase(path, pattern) or ("/" not in pattern and fnmatch.fnmatchcase(parts[-1], pattern)):
            return why
    return None


def source_files(root: Path = ROOT) -> list[str]:
    """Tracked files plus new, not-ignored ones, that exist on disk now."""
    names: set[str] = set()
    for args in (["ls-files", "-z"], ["ls-files", "-z", "--others", "--exclude-standard"]):
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout
        names.update(n for n in out.decode("utf-8").split("\0") if n)
    return sorted(n for n in names if (root / n).is_file())


def known_secrets(root: Path = ROOT) -> list[str]:
    """The owner's real API key, if his launchd file is here, so the build can make sure it is nowhere in a zip.
    Only compared in memory; never printed."""
    found = []
    for p in (Path.home() / "Library/LaunchAgents/com.tushar.claudequeue.plist", root / "setup/com.tushar.claudequeue.plist"):
        try:
            env = plistlib.loads(p.read_bytes()).get("EnvironmentVariables", {})
        except Exception:
            continue
        found += [v for k, v in env.items() if k.endswith("API_KEY") and isinstance(v, str) and len(v) >= 16]
    return found


def scan_for_secrets(files: dict[str, bytes], secrets: Optional[list[str]] = None) -> list[str]:
    """What looks like a secret in the files about to be packed: "path: kind" (never the secret itself)."""
    problems = []
    for name, data in sorted(files.items()):
        if len(data) > MAX_SCAN_BYTES or b"\0" in data[:4096]:
            continue
        text = data.decode("utf-8", errors="replace")
        for kind, rx in SECRET_PATTERNS:
            if rx.search(text):
                problems.append(f"{name}: looks like a {kind}")
        for s in secrets or []:
            if s in text:
                problems.append(f"{name}: contains the owner's real API key")
    return problems


def windows_stub(name: str, target: str) -> bytes:
    return (f'@echo off\r\ncall "%~dp0installer\\windows\\{target}" %*\r\n').encode("ascii")


def mac_stub(target: str) -> bytes:
    return (f'#!/bin/bash\n# Double-click this file. It runs the real one in the installer folder.\n'
            f'cd "$(dirname "$0")" || exit 1\nexec "./installer/mac/{target}" "$@"\n').encode("utf-8")


README_WINDOWS = """THE DEN - for Windows (version {version})

1. Unzip this download. Right-click the zip file and choose "Extract All", then open the new folder.
2. Double-click  Install-The-Den.bat
   - Windows may say "Windows protected your PC". That is only because this installer is not signed.
     Click "More info", then click "Run anyway".
3. Wait about 5 to 10 minutes. The window tells you what it is doing ("Step 3 of 7...").
   You will be asked to choose a name and password for signing in to The Den. Choose anything you will remember.
4. When it finishes, The Den opens in your browser. Next time, use the "The Den" icon on your Desktop.

You need an internet connection and a Claude account (the AI that does the work). If it asks you to sign in to Claude, do it once.
To remove The Den later, double-click Uninstall-The-Den.bat (it is kept in %LOCALAPPDATA%\\TheDen).

If something goes wrong, send the file  %LOCALAPPDATA%\\TheDen\\logs\\install.log  to whoever gave you The Den.
"""

README_MAC = """THE DEN - for Mac (version {version})

1. Unzip this download (double-click the zip file) and open the new folder.
2. Double-click  Install The Den.command
   - The Mac may say it "cannot be opened because it is from an unidentified developer". That is only because
     this installer is not signed. Right-click (or Control-click) the file, choose "Open", then click "Open" again.
3. Wait about 5 to 10 minutes. The window tells you what it is doing ("Step 3 of 7...").
   You will be asked to choose a name and password for signing in to The Den. Choose anything you will remember.
4. When it finishes, The Den opens in your browser. Next time, go to  http://127.0.0.1:8787/dashboard

You need Python 3.10 or newer (free, from python.org/downloads/macos), an internet connection and a Claude account.
If it asks you to sign in to Claude, do it once.
To remove The Den later, double-click "Uninstall The Den.command" in this folder.

If something goes wrong, send the files in  ~/Library/Logs/TheDen  to whoever gave you The Den.
"""


def build_one(system: str, files: list[str], root: Path, secrets: list[str]) -> dict:
    """Pack one zip. Returns a report; raises SystemExit if a secret was found."""
    folder, readme = ("windows", README_WINDOWS) if system == "windows" else ("mac", README_MAC)
    version = (root / "VERSION").read_text().strip() if (root / "VERSION").exists() else "?"
    payload: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    for rel in files:
        payload[rel] = (root / rel).read_bytes()
    inst = root / "installer" / folder
    for p in sorted(inst.iterdir()):
        if p.is_file():
            payload[f"installer/{folder}/{p.name}"] = p.read_bytes()
            modes[f"installer/{folder}/{p.name}"] = 0o755 if p.name.endswith(EXEC_NAMES) else 0o644
    if system == "windows":
        payload["Install-The-Den.bat"] = windows_stub("Install", "Install-The-Den.bat")
        payload["Uninstall-The-Den.bat"] = windows_stub("Uninstall", "Uninstall-The-Den.bat")
        payload["README-FIRST.txt"] = readme.format(version=version).replace("\n", "\r\n").encode("ascii")
    else:
        for top, target in (("Install The Den.command", "Install The Den.command"), ("Uninstall The Den.command", "Uninstall The Den.command")):
            payload[top] = mac_stub(target)
            modes[top] = 0o755
        payload["README-FIRST.txt"] = readme.format(version=version).encode("ascii")

    # Windows PowerShell 5 reads a script without a byte-order mark as the old code page: keep them plain ASCII.
    for name, data in payload.items():
        if name.endswith((".ps1", ".bat")):
            try:
                data.decode("ascii")
            except UnicodeDecodeError:
                raise SystemExit(f"{name} has non-ASCII characters; Windows PowerShell 5 would misread it. Fix that first.")

    problems = scan_for_secrets(payload, secrets)
    if problems:
        raise SystemExit("Stopped: the zip would contain secrets, and nothing was written.\n  " + "\n  ".join(problems))

    DIST.mkdir(exist_ok=True)
    out = DIST / f"TheDen-{'Windows' if system == 'windows' else 'Mac'}.zip"
    stamp = time.localtime()[:6]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(payload):
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = modes.get(name)
            if mode is None:
                src = root / name
                mode = (src.stat().st_mode & 0o777) if src.exists() else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(info, payload[name])
    return {"zip": out, "files": len(payload), "bytes": out.stat().st_size, "version": version}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build the Windows and Mac download zips.")
    ap.add_argument("--only", choices=["windows", "mac"], help="build just one")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN", help="leave one more file or folder/ out")
    args = ap.parse_args(argv)

    rules = EXCLUDE + [(p, "left out with --exclude") for p in args.exclude]
    included, left_out = [], {}
    for rel in source_files():
        why = excluded_by(rel, rules)
        if why:
            left_out.setdefault(why, []).append(rel)
        else:
            included.append(rel)

    print(f"The Den {(ROOT / 'VERSION').read_text().strip()}: {len(included)} files go into each zip")
    print("Left out:")
    for why, names in sorted(left_out.items(), key=lambda kv: -len(kv[1])):
        tops = sorted({n.split("/")[0] + ("/" if "/" in n else "") for n in names})
        print(f"  {len(names):4d}  {why}  ({', '.join(tops[:4])}{', ...' if len(tops) > 4 else ''})")

    secrets = known_secrets()
    for system in ([args.only] if args.only else ["windows", "mac"]):
        r = build_one(system, included, ROOT, secrets)
        print(f"Built {r['zip'].relative_to(ROOT)}: {r['files']} files, {r['bytes'] / 1_000_000:.1f} MB")
    print("Checked every file for keys and tokens: none found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
