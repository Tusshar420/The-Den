"""
A given-away Den's side of updates: is there a newer version, and getting it.

The person's Den knows three things, given to them by the owner (Updates page, "Update source"):
the address of the owner's release server, this Den's own update key, and the owner's public key.

- check(): asks for the latest version and checks the owner's signature on it. Nothing changes.
- install(): downloads that version, checks its size and sha256 against the signed manifest,
  unpacks it into .den-update/staged/ (refusing any path outside the Den or onto the person's
  own data, see launcher.safe_path) and checks every file against RELEASE.json. Then it asks the
  Den for a normal restart (running tasks finish first); launcher.py puts the files in place on
  the way back up, and goes back to the old version by itself if the new one doesn't start.

The Den stays on its version until the person presses Update - new work the owner does never
reaches it on its own.
"""

import hashlib
import json
import os
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import launcher
from . import common
from .common import UpdateError

API = "/den-updates/v1"
MAX_BUNDLE = 500 * 1024 * 1024
TIMEOUT = 20


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Updater:
    def __init__(self, root: Path, get: Callable[[str, Any], Any], put: Callable[[str, Any], None],
                 restart: Callable[[], Any], under_launcher: Callable[[], bool] = lambda: os.environ.get("DEN_LAUNCHER") == "1"):
        self.root, self.get, self.put, self.restart = root, get, put, restart
        self.under_launcher = under_launcher
        self.paths = launcher.Paths(root)
        self.state, self.error = "idle", ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------ where updates come from
    def source(self) -> dict[str, str]:
        s = self.get("source", {})
        return s if isinstance(s, dict) else {}

    def set_source(self, arg: dict[str, Any]) -> dict[str, Any]:
        if arg.get("clear"):
            self.put("source", {})
            self.put("latest", None)
            return self.status()
        url = str(arg.get("url") or "").strip().rstrip("/")
        key = str(arg.get("key") or "").strip()
        pub = str(arg.get("public_key") or "").strip()
        if not url.startswith(("http://", "https://")) or len(url) > 300 or " " in url:
            raise UpdateError("The update address should look like http://name-of-the-mac:8790")
        if not key or len(key) > 200:
            raise UpdateError("Paste this Den's update key, given by the owner")
        common.public_from_b64(pub)
        self.put("source", {"url": url, "key": key, "public_key": pub})
        self.put("latest", None)
        return self.status()

    # ------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        src = self.source()
        latest = self.get("latest", None)
        current = common.read_version(self.root)
        newer = bool(latest and common.vtuple(latest["version"]) > common.vtuple(current))
        hist = launcher._read(self.paths.history, [])
        state = self.state
        if state == "idle" and self.paths.pending.exists():
            state = "ready"
        return {"configured": bool(src.get("url") and src.get("key") and src.get("public_key")),
                "source_url": src.get("url", ""), "version": current, "last_checked": self.get("last_checked", None),
                "latest": latest, "update_available": newer, "state": state, "error": self.error,
                "can_install": common.kind(self.root) == "install" and self.under_launcher(),
                "history": list(reversed(hist if isinstance(hist, list) else []))[:10]}

    # ------------------------------------------------------------ talking to the owner's Den
    def _open(self, path: str, timeout: int = TIMEOUT):
        src = self.source()
        if not (src.get("url") and src.get("key") and src.get("public_key")):
            raise UpdateError("Set the update source first (the owner gives you the address and keys)")
        req = urllib.request.Request(src["url"] + API + path, headers={
            "X-Den-Install-Key": src["key"], "X-Den-Version": common.read_version(self.root), "User-Agent": "TheDen-updater"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error")
            except Exception:
                msg = None
            raise UpdateError(msg or f"The owner's Den answered with an error ({e.code})")
        except (urllib.error.URLError, OSError) as e:
            raise UpdateError("Couldn't reach the owner's Den - check Tailscale is on, then try again"
                              f" ({getattr(e, 'reason', e)})")

    def check(self) -> dict[str, Any]:
        try:
            with self._open("/latest") as r:
                body = json.loads(r.read(1024 * 1024).decode())
        except UpdateError as e:
            if "No version published" in str(e):
                self.put("latest", None)
                self.put("last_checked", _now())
                return self.status()
            raise
        m = common.clean_manifest(body.get("latest") if isinstance(body, dict) else None)
        if not m:
            raise UpdateError("The owner's Den sent something that isn't a version")
        common.verify(self.source()["public_key"], m)
        self.put("latest", m)
        self.put("last_checked", _now())
        return self.status()

    # ------------------------------------------------------------ installing
    def install(self) -> dict[str, Any]:
        if common.kind(self.root) != "install":
            raise UpdateError("This is the owner's own Den - it changes when you save your work, not by updating")
        if not self.under_launcher():
            raise UpdateError("This Den isn't started by launcher.py, so it can't update itself - see the Setup guide")
        if not self._lock.acquire(blocking=False):
            raise UpdateError("An update is already on its way")
        try:
            self.error = ""
            status = self.check()
            m = status["latest"]
            if not status["update_available"]:
                raise UpdateError(f"This Den is already up to date ({status['version']})")
            self.state = "downloading"
            self._download_and_stage(m)
            launcher._write(self.paths.pending, {"version": m["version"], "from": common.read_version(self.root), "at": _now()})
            self.state = "restarting"
            self.restart()
            return {"ok": True, "state": self.state, "version": m["version"]}
        except Exception as e:
            self.state = "error"
            self.error = str(e)
            shutil.rmtree(self.paths.staged, ignore_errors=True)
            self.paths.pending.unlink(missing_ok=True)
            raise
        finally:
            self._lock.release()

    def _download_and_stage(self, m: dict[str, Any]) -> None:
        if not 0 < m["size"] <= MAX_BUNDLE:
            raise UpdateError("The update has an unexpected size, so it was not used")
        upd = self.paths.upd
        upd.mkdir(parents=True, exist_ok=True)
        part = upd / "download.part"
        digest, got = hashlib.sha256(), 0
        with self._open(f"/bundle/{m['version']}", timeout=120) as r, part.open("wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > m["size"]:
                    raise UpdateError("The download is bigger than it should be, so it was not used")
                digest.update(chunk)
                f.write(chunk)
        if got != m["size"] or digest.hexdigest() != m["sha256"]:
            part.unlink(missing_ok=True)
            raise UpdateError("The download doesn't match the signed version, so it was not used")
        tmp = upd / "staged.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(self.paths.staged, ignore_errors=True)
        try:
            files = unpack(part, tmp)
            release = json.loads((tmp / "RELEASE.json").read_text())
            listed = release.get("files") if isinstance(release, dict) else None
            if release.get("version") != m["version"] or not isinstance(listed, dict):
                raise UpdateError("The update's file list doesn't match its version")
            if set(listed) != set(files) - {"RELEASE.json"}:
                raise UpdateError("The update's files don't match its file list")
            for name, sha in listed.items():
                if hashlib.sha256((tmp / name).read_bytes()).hexdigest() != sha:
                    raise UpdateError(f"{name} in the update is damaged")
            tmp.rename(self.paths.staged)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        finally:
            part.unlink(missing_ok=True)


def unpack(bundle: Path, dest: Path) -> set[str]:
    """Unpack only plain files with safe relative paths; anything else means the bundle is refused."""
    names: set[str] = set()
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            for m in tar:
                if m.isdir():
                    continue
                if not m.isfile() or not (m.name == "RELEASE.json" or launcher.safe_path(m.name)):
                    raise UpdateError(f"The update contains something it shouldn't ({m.name[:80]}), so it was not used")
                if m.name in names:
                    raise UpdateError("The update lists a file twice, so it was not used")
                names.add(m.name)
                out = dest / m.name
                out.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                with out.open("wb") as f:
                    shutil.copyfileobj(src, f)
                os.chmod(out, 0o755 if m.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError, EOFError) as e:
        raise UpdateError(f"The update couldn't be unpacked ({e})")
    if "RELEASE.json" not in names:
        raise UpdateError("The update has no file list, so it was not used")
    return names


class AutoCheck:
    """Looks for a new version every few hours (only says so - never installs by itself)."""

    def __init__(self, updater: Updater, hours: float, log: Any):
        self.updater, self.hours, self.log = updater, hours, log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.hours <= 0:
            return
        self._thread = threading.Thread(target=self._loop, name="den-update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(90):
            return
        while not self._stop.is_set():
            if self.updater.status()["configured"]:
                try:
                    self.updater.check()
                except Exception as e:
                    self.log.info("update check: %s", e)
            if self._stop.wait(self.hours * 3600):
                return
