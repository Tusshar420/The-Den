"""
Starts a Den that was given to someone (an "install"), and puts updates in place.

launchd (Mac) or den_service.py (Windows) runs this instead of main.py on an install. Every start:

1. If an update is waiting (.den-update/pending.json + .den-update/staged/, left there by the
   Updates page once the download was checked), it goes in: every file it replaces or removes is
   first copied to .den-update/backup/, with a copy of queue.db, then the new files are written.
   Files the old version had and the new one doesn't are removed. The person's own things -
   the database, logins, AI accounts, keys, uploads, the vault, backups - are never touched
   (KEEP), and a release can't contain them.
2. main.py is started as a child (with DEN_LAUNCHER=1, so the Updates page knows it can update).
3. Right after an update, the new version has two minutes to answer /health. If it dies or
   doesn't answer, it is stopped and everything goes back exactly as it was (files, database,
   Python packages); launchd then starts the old version again. Either way the result is written
   to .den-update/history.json, which the Updates page shows.

The Den restarts by exiting; this exits with it, and launchd (KeepAlive) - or den_service.py on
Windows - starts this again.
Stdlib only - it has to work before anything is installed.
"""

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
HEALTH_SECONDS = 120

# Never written or removed by an update, whatever a bundle says.
KEEP_NAMES = {"queue.db", "accounts.json", "login.json", "api_keys.json", "restart_held.json",
              "restart_requested.json", "den.env", "den-keys.json", "mcp-config.local.json"}
KEEP_DIRS = {".venv", ".approval-venv", ".den-update", ".git", "workspace", "vault", "backups", "releases",
             "static/project-images"}


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [launcher] {msg}", flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_path(name: str) -> bool:
    """A relative path inside the Den that an update may write."""
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if parts[-1] in KEEP_NAMES or parts[-1].startswith("queue.db"):
        return False
    return not any("/".join(parts[:i]) in KEEP_DIRS for i in range(1, len(parts) + 1))


class Paths:
    def __init__(self, root: Path):
        self.root = root
        self.upd = root / ".den-update"
        self.staged = self.upd / "staged"
        self.pending = self.upd / "pending.json"
        self.backup = self.upd / "backup"
        self.verify = self.upd / "verify.json"
        self.history = self.upd / "history.json"


def _read(p: Path, default: Any = None) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)


def record(paths: Paths, entry: dict[str, Any]) -> None:
    hist = _read(paths.history, [])
    hist = (hist if isinstance(hist, list) else []) + [{**entry, "at": _now()}]
    _write(paths.history, hist[-30:])


def _files(manifest: Any) -> dict[str, str]:
    files = manifest.get("files") if isinstance(manifest, dict) else None
    return {k: v for k, v in files.items() if isinstance(k, str) and safe_path(k)} if isinstance(files, dict) else {}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".den-new")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _pip(root: Path) -> None:
    req = root / "requirements.txt"
    if req.exists():
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)], cwd=root, check=True, timeout=900)


def _backup_db(root: Path, dst: Path) -> bool:
    db = root / "queue.db"
    if not db.exists():
        return False
    src = sqlite3.connect(str(db))
    try:
        out = sqlite3.connect(str(dst))
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    return True


def apply_staged(paths: Paths) -> Optional[dict[str, Any]]:
    """Put a waiting update in place. Returns what was applied, or None if nothing waits."""
    pending = _read(paths.pending)
    new = _read(paths.staged / "RELEASE.json")
    if not (isinstance(pending, dict) and isinstance(new, dict) and paths.staged.is_dir()):
        return None
    root = paths.root
    old = _read(root / "RELEASE.json", {})
    new_files, old_files = _files(new), _files(old)
    if not new_files or new.get("version") != pending.get("version"):
        raise RuntimeError("the downloaded update is incomplete")
    for name, digest in new_files.items():          # checked again: nothing may change after the download
        if _sha(paths.staged / name) != digest:
            raise RuntimeError(f"{name} changed after it was downloaded")
    if paths.backup.exists():
        shutil.rmtree(paths.backup)
    (paths.backup / "files").mkdir(parents=True)
    saved = []
    for name in sorted(set(new_files) | set(old_files)):
        if (root / name).is_file():
            _copy(root / name, paths.backup / "files" / name)
            saved.append(name)
    if (root / "RELEASE.json").exists():
        _copy(root / "RELEASE.json", paths.backup / "RELEASE.json")
    had_db = _backup_db(root, paths.backup / "queue.db")
    req_changed = (old_files.get("requirements.txt") or "") != new_files.get("requirements.txt", "")
    info = {"from": str(old.get("version") or pending.get("from") or ""), "to": new["version"], "saved": saved,
            "added": sorted(set(new_files) - set(saved)), "db": had_db, "requirements": req_changed}
    _write(paths.backup / "info.json", info)
    # From here on a failure is undone by rollback(), which this marker asks for.
    _write(paths.verify, {"from": info["from"], "to": info["to"], "at": _now()})
    for name in new_files:
        _copy(paths.staged / name, root / name)
    for name in set(old_files) - set(new_files):
        (root / name).unlink(missing_ok=True)
    _copy(paths.staged / "RELEASE.json", root / "RELEASE.json")
    shutil.rmtree(paths.staged, ignore_errors=True)
    paths.pending.unlink(missing_ok=True)
    log(f"updated {info['from'] or '?'} -> {info['to']} ({len(new_files)} files)")
    if req_changed:
        log("installing Python packages for the new version")
        _pip(root)
    return info


def rollback(paths: Paths, reason: str) -> None:
    """Put back exactly what the last update replaced."""
    root = paths.root
    info = _read(paths.backup / "info.json")
    if not isinstance(info, dict):
        log(f"can't roll back ({reason}): no backup")
        paths.verify.unlink(missing_ok=True)
        return
    for name in info.get("added", []):
        if safe_path(name):
            (root / name).unlink(missing_ok=True)
    for name in info.get("saved", []):
        if safe_path(name):
            _copy(paths.backup / "files" / name, root / name)
    if (paths.backup / "RELEASE.json").exists():
        _copy(paths.backup / "RELEASE.json", root / "RELEASE.json")
    else:
        (root / "RELEASE.json").unlink(missing_ok=True)
    if info.get("db") and (paths.backup / "queue.db").exists():
        for side in ("queue.db-wal", "queue.db-shm"):
            (root / side).unlink(missing_ok=True)
        _copy(paths.backup / "queue.db", root / "queue.db")
    if info.get("requirements"):
        try:
            _pip(root)
        except Exception as e:     # the old packages are usually still there
            log(f"reinstalling the old Python packages failed: {e}")
    paths.verify.unlink(missing_ok=True)
    shutil.rmtree(paths.staged, ignore_errors=True)
    paths.pending.unlink(missing_ok=True)
    record(paths, {"from": info.get("from"), "to": info.get("to"), "ok": False,
                   "detail": f"Went back to {info.get('from') or 'the previous version'}: {reason}"})
    log(f"rolled back to {info.get('from')}: {reason}")


def healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def run(root: Path = ROOT, health_seconds: int = HEALTH_SECONDS) -> int:
    paths = Paths(root)
    try:
        apply_staged(paths)
    except Exception as e:
        log(f"update failed: {e}")
        if paths.verify.exists():
            rollback(paths, f"the update couldn't be put in place ({e})")
        else:
            shutil.rmtree(paths.staged, ignore_errors=True)
            paths.pending.unlink(missing_ok=True)
            record(paths, {"to": "", "ok": False, "detail": f"The update wasn't used: {e}"})
    env = {**os.environ, "DEN_LAUNCHER": "1"}
    extra: dict[str, Any] = {}
    if os.name == "nt":      # files are read as UTF-8 (not the Windows code page), and no console window pops up
        env.setdefault("PYTHONUTF8", "1")
        extra["creationflags"] = 0x08000000
    child = subprocess.Popen([sys.executable, str(root / "main.py")], cwd=root, env=env, **extra)

    def forward(signum: int, _frame: Any) -> None:
        if child.poll() is None:
            if os.name == "nt":              # Windows can only be asked to stop, not sent a particular signal
                child.terminate()
            else:
                child.send_signal(signum)
    for s in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None), getattr(signal, "SIGBREAK", None)):
        if s is not None:
            signal.signal(s, forward)

    verify = _read(paths.verify)
    if isinstance(verify, dict):
        port = int(os.environ.get("PORT", "8787"))
        deadline = time.time() + health_seconds
        ok = False
        while time.time() < deadline and child.poll() is None:
            if healthy(port):
                ok = True
                break
            time.sleep(2)
        if ok:
            paths.verify.unlink(missing_ok=True)
            record(paths, {"from": verify.get("from"), "to": verify.get("to"), "ok": True,
                           "detail": f"Updated to {verify.get('to')}"})
            log(f"version {verify.get('to')} is up")
        else:
            why = "it stopped while starting" if child.poll() is not None else "it didn't answer within two minutes"
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            rollback(paths, f"version {verify.get('to')} didn't start ({why})")
            return 1
    return child.wait()


if __name__ == "__main__":
    sys.exit(run())
