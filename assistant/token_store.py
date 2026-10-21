"""
Where OAuth tokens live. Never in queue.db, task text, logs or API
responses - only here.

On the Mac that is the login Keychain, written through `security -i` so the
token travels over stdin and never shows up in `ps`. Anywhere without the
`security` tool (or with OCTOPUS_TOKEN_BACKEND=file) it falls back to a
0600 file under ~/.octopus/, which is weaker but still owner-only. On Windows it is the
Credential Manager (or an owner-only key file), through den_platform.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

import den_platform

SERVICE = "com.tushar.octopus.assistant"
_lock = threading.Lock()


class TokenStoreError(Exception):
    """Never carries token material in its message."""


def _encode(data: dict[str, Any]) -> str:
    # URL-safe base64 has no characters `security -i` would treat specially.
    return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).decode("ascii")


def _decode(blob: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(blob.strip().encode("ascii")).decode("utf-8"))


class KeychainStore:
    name = "keychain"

    def get(self, account: str) -> Optional[dict[str, Any]]:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 44:  # errSecItemNotFound
            return None
        if r.returncode != 0:
            raise TokenStoreError(f"Keychain read failed (security exit {r.returncode})")
        try:
            return _decode(r.stdout)
        except (ValueError, UnicodeError):
            raise TokenStoreError("Keychain item is unreadable; disconnect and reconnect the account")

    def set(self, account: str, data: dict[str, Any]) -> None:
        if not re.fullmatch(r"[\w.:\-]+", account):  # it is pasted into a `security -i` command line
            raise TokenStoreError("Invalid account name")
        r = subprocess.run(["security", "-i"], capture_output=True, text=True, timeout=15,
                           input=f"add-generic-password -U -s {SERVICE} -a {account} -w {_encode(data)}\n")
        if r.returncode != 0:
            raise TokenStoreError(f"Keychain write failed (security exit {r.returncode}); is the login keychain unlocked?")

    def delete(self, account: str) -> None:
        r = subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                           capture_output=True, text=True, timeout=15)
        if r.returncode not in (0, 44):
            raise TokenStoreError(f"Keychain delete failed (security exit {r.returncode})")


class PlatformStore:
    """Windows: the Credential Manager (through the `keyring` package) when it is there, otherwise a
    owner-only key file in the Den's folder. The choice is made in den_platform."""
    name = "platform"

    def get(self, account: str) -> Optional[dict[str, Any]]:
        try:
            return den_platform.secret_get(SERVICE, account)
        except OSError as e:
            raise TokenStoreError(f"The key store couldn't be read ({e.__class__.__name__})")

    def set(self, account: str, data: dict[str, Any]) -> None:
        try:
            den_platform.secret_set(SERVICE, account, data)
        except OSError as e:
            raise TokenStoreError(f"The key store couldn't be written ({e.__class__.__name__})")

    def delete(self, account: str) -> None:
        try:
            den_platform.secret_delete(SERVICE, account)
        except OSError as e:
            raise TokenStoreError(f"The key store couldn't be changed ({e.__class__.__name__})")


class FileStore:
    name = "file"

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.environ.get("OCTOPUS_TOKEN_FILE") or Path.home() / ".octopus" / "tokens.json")

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            raise TokenStoreError(f"{self.path} is not valid JSON")

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def get(self, account: str) -> Optional[dict[str, Any]]:
        return self._read().get(account)

    def set(self, account: str, data: dict[str, Any]) -> None:
        current = self._read()
        current[account] = data
        self._write(current)

    def delete(self, account: str) -> None:
        current = self._read()
        if current.pop(account, None) is not None:
            self._write(current)


def backend():
    choice = os.environ.get("OCTOPUS_TOKEN_BACKEND")
    if choice == "file":
        return FileStore()
    if choice == "keychain" or (sys.platform == "darwin" and shutil.which("security")):
        return KeychainStore()
    if den_platform.IS_WINDOWS:
        return PlatformStore()
    return FileStore()


def get(account: str) -> Optional[dict[str, Any]]:
    with _lock:
        return backend().get(account)


def put(account: str, data: dict[str, Any]) -> None:
    with _lock:
        backend().set(account, data)


def remove(account: str) -> None:
    with _lock:
        backend().delete(account)
