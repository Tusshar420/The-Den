"""
Dev server: start and stop a project's local website (localhost) from its page in the dashboard,
so you never have to open VS Code and type the run command.

Each project gets a start command - worked out from its files (package.json "dev" / "start",
Django manage.py, PHP or plain HTML), or one you save yourself. A server is started in the
project's own folder, on its own port, in the background; its output goes to a log file
(~/Library/Logs/TheDen/devserver-<id>.log). The link shown is the one the server printed itself.

Servers keep running if this module is reloaded: their pids are kept with ctx.set_state and picked
up again. Nothing here is started or stopped by itself except by the commands below.

Commands (POST /modules/commands/<name>, {"arg": "<project id>"} unless said):
  devserver_list              every project: command, port, running, link
  devserver_start <id>        start it (does nothing if already running)
  devserver_stop <id>         stop it and everything it started
  devserver_set               arg is JSON {"id": 7, "command": "npm run dev", "port": 3000};
                              an empty command goes back to the automatic one
  devserver_log <id>          the last lines of its output

A module, so a change here needs POST /modules/dev_server/reload, never a restart.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import db

LOG_DIR = Path.home() / "Library" / "Logs" / "TheDen"
URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])(?::(\d+))?[^\s\x1b'\"]*")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PATH_EXTRA = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin"


def detect(path: str, port: int) -> Optional[dict[str, str]]:
    """The command a project most likely uses, from its files. None = nothing to run (e.g. a
    Salesforce project, or no website at all)."""
    root = Path(path)
    if not root.is_dir():
        return None
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts") or {}
        except (ValueError, OSError):
            scripts = {}
        runner = "pnpm" if (root / "pnpm-lock.yaml").is_file() else "yarn" if (root / "yarn.lock").is_file() else "npm run"
        for name in ("dev", "start"):
            if name in scripts:
                cmd = {"pnpm": f"pnpm {name}", "yarn": f"yarn {name}"}.get(runner, "npm start" if name == "start" else "npm run dev")
                return {"command": cmd, "why": f'package.json "{name}"'}
    if (root / "manage.py").is_file():
        return {"command": "python3 manage.py runserver {port}", "why": "Django manage.py"}
    if any(root.glob("*.php")) or (root / "index.php").is_file():
        return {"command": "php -S 127.0.0.1:{port}", "why": "PHP files"}
    if (root / "index.html").is_file():
        return {"command": "python3 -m http.server {port} --bind 127.0.0.1", "why": "index.html"}
    return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a finished child that nobody has collected yet still answers kill(0)
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return bool(out.strip()) and not out.strip().startswith("Z")
    except Exception:
        return True


def tail(path: Path, lines: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 60000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(ANSI_RE.sub("", text).splitlines()[-lines:])


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        self.children: dict[int, subprocess.Popen] = {}   # only so finished children get collected
        ctx.register_command("devserver_list", lambda arg: self.list_all(), "Every project's local-website status")
        ctx.register_command("devserver_start", lambda arg: self.start_one(int(arg)), "Start a project's local website")
        ctx.register_command("devserver_stop", lambda arg: self.stop_one(int(arg)), "Stop a project's local website")
        ctx.register_command("devserver_set", lambda arg: self.set_one(json.loads(arg)), "Save a project's start command / port")
        ctx.register_command("devserver_log", lambda arg: self.log_of(int(arg)), "Last lines of a project's local website output")

    # ---- saved things ------------------------------------------------------------------------
    def _saved(self) -> dict[str, dict[str, Any]]:
        v = self.ctx.get_state("settings", {})
        return v if isinstance(v, dict) else {}

    def _running(self) -> dict[str, dict[str, Any]]:
        v = self.ctx.get_state("running", {})
        return v if isinstance(v, dict) else {}

    def port_for(self, pid: int) -> int:
        saved = self._saved().get(str(pid), {})
        return int(saved.get("port") or int(self.ctx.config["first_port"]) + pid)

    def log_path(self, pid: int) -> Path:
        return LOG_DIR / f"devserver-{pid}.log"

    def project(self, pid: int) -> dict[str, Any]:
        p = db.get_project(pid)
        if not p or p.get("deleted_at"):
            raise ValueError(f"No project with id {pid}")
        return p

    # ---- status ------------------------------------------------------------------------------
    def info(self, p: dict[str, Any]) -> dict[str, Any]:
        pid = p["id"]
        port = self.port_for(pid)
        saved = self._saved().get(str(pid), {})
        auto = detect(p["path"], port)
        command = (saved.get("command") or "").strip() or (auto or {}).get("command", "")
        run = self._running().get(str(pid))
        up = bool(run) and alive(run["pid"])
        if run and not up:   # it ended by itself (crash, or stopped from a terminal)
            state = self._running()
            state.pop(str(pid), None)
            self.ctx.set_state("running", state)
            self.children.pop(pid, None)
        out = tail(self.log_path(pid), 40) if run else ""
        link = ""
        if up:
            m = None
            for m in URL_RE.finditer(out):
                pass   # keep the latest one the server printed
            link = (m.group(0).replace("0.0.0.0", "localhost") if m else f"http://localhost:{run['port']}").rstrip(".,)")
        return {"id": pid, "name": p["name"], "command": command, "custom": bool(saved.get("command")),
                "why": "your own command" if saved.get("command") else (auto or {}).get("why", ""),
                "port": port, "can_run": bool(command), "running": up, "link": link,
                "crashed": bool(run) and not up, "since": run["started"] if up else None,
                "last_output": out.splitlines()[-1] if out else ""}

    def list_all(self) -> dict[str, Any]:
        out = []
        for p in db.list_projects(active_only=True):
            if os.path.realpath(p["path"]) == os.path.realpath(str(Path(__file__).resolve().parents[2])):
                continue   # The Den itself is always running
            out.append(self.info(p))
        return {"servers": out}

    # ---- start / stop ------------------------------------------------------------------------
    def start_one(self, pid: int) -> dict[str, Any]:
        p = self.project(pid)
        cur = self.info(p)
        if cur["running"]:
            return cur
        if not cur["can_run"]:
            raise ValueError("Nothing to run here yet: there is no website command. Save one with devserver_set.")
        if not Path(p["path"]).is_dir():
            raise ValueError(f"The folder is missing: {p['path']}")
        port = cur["port"]
        cmd = cur["command"].replace("{port}", str(port))
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path(pid), "wb")
        env = dict(os.environ, PORT=str(port), BROWSER="none", FORCE_COLOR="0", NO_COLOR="1",
                   PATH=PATH_EXTRA + ":" + os.environ.get("PATH", ""))
        env.pop("API_KEY", None)   # the project's server has no business with The Den's key
        shell = shutil.which("zsh") or "/bin/sh"
        try:   # a login shell, so tools set up in the user's profile (node, php...) are found
            proc = subprocess.Popen([shell, "-lc", f"exec {cmd}" if " && " not in cmd and ";" not in cmd else cmd],
                                    cwd=p["path"], env=env, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        self.children[pid] = proc
        state = self._running()
        state[str(pid)] = {"pid": proc.pid, "port": port, "started": time.time(), "command": cmd}
        self.ctx.set_state("running", state)
        self.ctx.log.info("Started %s: %s (pid %s, port %s)", p["name"], cmd, proc.pid, port)
        time.sleep(1.5)   # so an instant failure (command not found) shows straight away
        return self.info(p)

    def stop_one(self, pid: int) -> dict[str, Any]:
        p = self.project(pid)
        run = self._running().get(str(pid))
        if run and alive(run["pid"]):
            try:
                os.killpg(run["pid"], signal.SIGTERM)   # the server and anything it started
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(50):
                if not alive(run["pid"]):
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(run["pid"], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            child = self.children.pop(pid, None)
            if child:
                try:
                    child.wait(timeout=2)
                except Exception:
                    pass
            self.ctx.log.info("Stopped %s", p["name"])
        state = self._running()
        state.pop(str(pid), None)
        self.ctx.set_state("running", state)
        return self.info(p)

    def set_one(self, body: dict[str, Any]) -> dict[str, Any]:
        p = self.project(int(body["id"]))
        saved = self._saved()
        row = saved.get(str(p["id"]), {})
        if "command" in body:
            row["command"] = str(body["command"] or "").strip()[:500]
        if "port" in body:
            port = int(body["port"] or 0)
            if port and not 1024 <= port <= 65535:
                raise ValueError("The port must be between 1024 and 65535.")
            row["port"] = port
        saved[str(p["id"])] = {k: v for k, v in row.items() if v}
        self.ctx.set_state("settings", saved)
        return self.info(p)

    def log_of(self, pid: int) -> dict[str, Any]:
        self.project(pid)
        return {"id": pid, "log": tail(self.log_path(pid), int(self.ctx.config["log_lines"]))}
