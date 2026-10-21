"""
The one place that knows whether The Den is on a Mac, on Windows or on Linux.

Everything that has to behave differently (stopping a program and the things it started, where logs go,
where a secret is kept, how to open a folder) asks this file, so the rest of the code stays the same on
every system. On a Mac every answer here is exactly what the code did before this file existed.

Stdlib only (`keyring` is used on Windows when it is installed, and the Den works without it).
"""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

ROOT = Path(__file__).resolve().parent
PAUSE_UNSUPPORTED = "Pausing a running task isn't available on Windows. You can still stop it, and pause one that is waiting."

# Windows process flags (the constants only exist in subprocess on Windows)
_NEW_PROCESS_GROUP = 0x00000200
_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- programs and their children

def spawn_kwargs() -> dict[str, Any]:
    """Extra arguments so a program we start is its own group, and can be stopped with everything it started."""
    if IS_WINDOWS:
        return {"creationflags": _NEW_PROCESS_GROUP | _NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid: int) -> bool:
    """Stop a program and everything it started. False if it couldn't be found or reached."""
    if IS_WINDOWS:
        try:
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=20,
                               creationflags=_NO_WINDOW)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def can_pause() -> bool:
    """Freezing a running program needs Unix signals; Windows has no equivalent we can rely on."""
    return not IS_WINDOWS


def freeze_tree(pid: int) -> bool:
    return _signal_group(pid, "SIGSTOP")


def thaw_tree(pid: int) -> bool:
    return _signal_group(pid, "SIGCONT")


def _signal_group(pid: int, name: str) -> bool:
    sig = getattr(signal, name, None)
    if sig is None or IS_WINDOWS:
        return False
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------- folders

def log_dir() -> Path:
    """Where the Den's own logs go. Mac: ~/Library/Logs/TheDen. Windows: %LOCALAPPDATA%\\TheDen\\logs.
    DEN_LOG_DIR overrides it (for tests)."""
    if os.environ.get("DEN_LOG_DIR"):
        return Path(os.environ["DEN_LOG_DIR"])
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "TheDen" / "logs"
    if IS_MAC:
        return Path.home() / "Library" / "Logs" / "TheDen"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "TheDen" / "logs"


def data_dir() -> Path:
    """The Den's own folder: the database, logins and keys live next to the code."""
    return ROOT


def browser_cache_dirs() -> list[Path]:
    """Where Playwright keeps its browsers."""
    home = Path.home()
    if IS_WINDOWS:
        return [Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "ms-playwright"]
    return [home / "Library/Caches/ms-playwright", home / ".cache/ms-playwright"]


def open_path(path: str) -> None:
    """Show a folder or file in the system's file manager."""
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.run(["open", path], timeout=5, check=False)
    else:
        subprocess.run(["xdg-open", path], timeout=5, check=False)


def file_manager_name() -> str:
    return "Finder" if IS_MAC else "File Explorer" if IS_WINDOWS else "the file manager"


# ---------------------------------------------------------------- Claude Code

def claude_bin() -> str:
    """The Claude Code program: CLAUDE_BIN if set, else `claude`. On Windows the real file is claude.exe or
    claude.cmd, which a plain name won't find when we start it, so it is looked up here."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit
    if not IS_WINDOWS:
        return "claude"
    found = shutil.which("claude")
    if found:
        return found
    for rel in (".local/bin/claude.exe", "AppData/Roaming/npm/claude.cmd", ".claude/local/claude.exe"):
        p = Path.home() / rel
        if p.is_file():
            return str(p)
    return "claude"


# ---------------------------------------------------------------- the approval helper's config

def mcp_config_path(root: Path = ROOT) -> Path:
    """mcp-config.json names the Python that runs approval_server.py. The copy in the repo points at the
    owner's own folder; anywhere else it would point at nothing. When the program it names isn't there, a
    copy that points at this install's own Python is written to mcp-config.local.json and used instead.
    (An update can't overwrite that copy: it isn't part of a release.)"""
    shared = root / "mcp-config.json"
    try:
        data = json.loads(shared.read_text(encoding="utf-8"))
        cmd = data["mcpServers"]["approval"]["command"]
    except (OSError, ValueError, KeyError, TypeError):
        return shared
    if Path(cmd).exists():
        return shared
    local = root / "mcp-config.local.json"
    py = next((p for p in (root / ".approval-venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python3"),) if p.exists()),
              Path(sys.executable))
    data["mcpServers"]["approval"] = {"command": str(py), "args": [str(root / "approval_server.py")]}
    try:
        local.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        return shared
    return local


# ---------------------------------------------------------------- secrets (not the Mac Keychain)

SECRET_FILE = "den-keys.json"
_CHUNK = 900              # Windows Credential Manager refuses long values, so they are stored in pieces
_secret_lock = threading.Lock()


def _keyring() -> Any:
    if not IS_WINDOWS:
        return None
    try:
        import keyring
        return keyring
    except Exception:
        return None


def _file_path(root: Optional[Path] = None) -> Path:
    return (root or data_dir()) / SECRET_FILE


def _file_read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # owner-only where the system supports it
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    if IS_WINDOWS:                    # chmod does little there: drop everyone but this person from the file
        try:
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                               capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass


def secret_get(service: str, account: str, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """A small JSON value kept safely for this person. None if it isn't there. Raises OSError if the store can't be read."""
    with _secret_lock:
        kr = _keyring()
        if kr is not None:
            try:
                head = kr.get_password(service, account)
                if head is not None:
                    n = int(head)
                    parts = [kr.get_password(service, f"{account}#{i}") for i in range(n)]
                    if None not in parts:
                        return json.loads(base64.urlsafe_b64decode("".join(parts)).decode("utf-8"))
            except Exception:
                pass                    # fall through to the file
        return _file_read(_file_path(root)).get(f"{service}/{account}")


def secret_set(service: str, account: str, data: dict[str, Any], root: Optional[Path] = None) -> None:
    with _secret_lock:
        kr = _keyring()
        if kr is not None:
            try:
                blob = base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
                parts = [blob[i:i + _CHUNK] for i in range(0, len(blob), _CHUNK)] or [""]
                for i, p in enumerate(parts):
                    kr.set_password(service, f"{account}#{i}", p)
                kr.set_password(service, account, str(len(parts)))
                path = _file_path(root)                        # an older file copy must not outlive the new value
                current = _file_read(path)
                if current.pop(f"{service}/{account}", None) is not None:
                    _file_write(path, current)
                return
            except Exception:
                pass                    # no usable store: the key file below
        path = _file_path(root)
        current = _file_read(path)
        current[f"{service}/{account}"] = data
        _file_write(path, current)


def secret_delete(service: str, account: str, root: Optional[Path] = None) -> None:
    with _secret_lock:
        kr = _keyring()
        if kr is not None:
            try:
                n = int(kr.get_password(service, account) or 0)
                for name in [account] + [f"{account}#{i}" for i in range(n)]:
                    try:
                        kr.delete_password(service, name)
                    except Exception:
                        pass
            except Exception:
                pass
        path = _file_path(root)
        current = _file_read(path)
        if current.pop(f"{service}/{account}", None) is not None:
            _file_write(path, current)
