"""
Keeps The Den running on Windows (a Mac has launchd for this).

The Windows Scheduled Task "The Den" starts this at sign-in. It runs launcher.py, and whenever that
exits it runs it again - The Den restarts by exiting (Update, Restart), so this is what brings it back.
It stops for good only when the stop file `.den-stop` exists in the Den's folder (the uninstaller and
`python den_service.py stop` make it; the installer removes it before starting).

Settings come from `den.env` in the Den's folder (one NAME=value per line: API_KEY, HOST, PORT...), the
way launchd's plist gives them on a Mac. Output goes to the log folder (den_platform.log_dir()).

Stdlib only.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
STOP_FLAG = ".den-stop"
ENV_FILE = "den.env"
LOG_NAME = "stdout.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
QUICK_EXIT_SECONDS = 20        # an exit sooner than this counts as "it can't start", and the wait grows
_NO_WINDOW = 0x08000000


def read_env_file(path: Path) -> dict[str, str]:
    """NAME=value lines. Blank lines and # comments are skipped; matching quotes around a value are removed."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name.strip():
            out[name.strip()] = value
    return out


def restart_delay(ran_seconds: float, previous: float) -> float:
    """How long to wait before starting again: 2 seconds normally, longer (up to a minute) while it keeps dying at once."""
    if ran_seconds >= QUICK_EXIT_SECONDS:
        return 2.0
    return min(60.0, max(5.0, previous * 2))


def _log_dir() -> Path:
    sys.path.insert(0, str(ROOT))
    try:
        import den_platform
        return den_platform.log_dir()
    except Exception:
        return ROOT


def _say(log, msg: str) -> None:
    log.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [service] {msg}\n")
    log.flush()


def _open_log(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOG_NAME
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            path.replace(directory / (LOG_NAME + ".old"))
    except OSError:
        pass
    return open(path, "a", encoding="utf-8", errors="replace", buffering=1)


def _python_for_child() -> str:
    """python.exe, even when this itself runs under pythonw.exe (which has no console to give it)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.with_name("python.exe")).exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def _stop_tree(child: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
    else:
        child.terminate()
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()


def _single_instance(root: Path):
    """Hold a lock for as long as this runs, so a second copy exits instead of fighting for the port.
    Returns the open file (keep it), or None when another copy already has the lock."""
    f = open(root / ".den-service.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def run(root: Path = ROOT, max_runs: Optional[int] = None) -> int:
    root = Path(root)
    stop = root / STOP_FLAG
    lock = _single_instance(root)
    if lock is None:
        return 0
    log = None
    try:
        env = {**os.environ, **read_env_file(root / ENV_FILE)}
        env.setdefault("PYTHONUTF8", "1")
        log = _open_log(_log_dir())
        flags = _NO_WINDOW if os.name == "nt" else 0
        delay, runs = 0.0, 0
        _say(log, "The Den service started")
        while not stop.exists() and (max_runs is None or runs < max_runs):
            runs += 1
            started = time.time()
            try:
                child = subprocess.Popen([_python_for_child(), str(root / "launcher.py")], cwd=root, env=env,
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=flags)
            except OSError as e:
                _say(log, f"couldn't start the Den: {e}")
                child = None
            while child is not None:
                try:
                    code = child.wait(timeout=2)
                    _say(log, f"the Den exited (code {code})")
                    break
                except subprocess.TimeoutExpired:
                    if stop.exists():
                        _say(log, "stop requested; stopping the Den")
                        _stop_tree(child)
                        return 0
            delay = restart_delay(time.time() - started, delay)
            waited = 0.0
            while waited < delay and not stop.exists():
                time.sleep(0.5)
                waited += 0.5
        _say(log, "The Den service ended")
        return 0
    finally:
        lock.close()
        if log is not None:
            log.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        (ROOT / STOP_FLAG).write_text("stop\n")
        print("The Den will stop within a few seconds. Delete the .den-stop file to let it run again.")
        sys.exit(0)
    sys.exit(run())
