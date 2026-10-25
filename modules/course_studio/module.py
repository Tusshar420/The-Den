"""
Course Studio: lets the dashboard's "Course Studio" page start, stop and find the AFI India course maker.

The studio itself lives in the AFI-India project (course-studio/server.py) and only listens on this Mac
(127.0.0.1). This module just runs it in the background, the same way the dev_server module runs a
project's website, and answers whether it is up.

It keeps running if this module is reloaded or The Den restarts: "start" first checks whether something is
already answering on the port, and only starts a new one if not.

The studio is started with a small, clean environment (HOME, PATH and the like), NOT The Den's own - so none of
The Den's keys or secrets are handed to it.

Commands (POST /modules/commands/<name>):
  course-studio-status   {"running", "url", "installed", "log"}
  course-studio-start    starts it if needed, waits a few seconds, then returns the status
  course-studio-stop     stops it

A module, so a change here needs POST /modules/course_studio/reload, never a restart.
"""

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

LOG_FILE = Path.home() / "Library" / "Logs" / "TheDen" / "course-studio.log"


class Module:
    def initialize(self, ctx) -> None:
        self.ctx = ctx
        self.project = Path(ctx.config["project_path"]).expanduser()
        self.port = int(ctx.config["port"])
        ctx.register_command("course-studio-status", lambda arg: self.status(), "Is Course Studio running?")
        ctx.register_command("course-studio-start", lambda arg: self.start_it(), "Start Course Studio")
        ctx.register_command("course-studio-stop", lambda arg: self.stop_it(), "Stop Course Studio")

    # ------------------------------------------------------------------------------------------
    def _up(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.6):
                return True
        except OSError:
            return False

    def _script(self) -> Path:
        return self.project / "course-studio" / "server.py"

    def status(self) -> dict[str, Any]:
        return {"running": self._up(), "url": f"http://127.0.0.1:{self.port}/",
                "installed": self._script().is_file(), "project": str(self.project), "log": str(LOG_FILE)}

    def start_it(self) -> dict[str, Any]:
        if not self._script().is_file():
            raise RuntimeError(f"Course Studio is not installed at {self._script()}")
        if not self._up():
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            home = str(Path.home())
            env = {"HOME": home, "USER": os.environ.get("USER", ""), "LANG": "en_US.UTF-8", "TERM": "dumb",
                   "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
            log = open(LOG_FILE, "ab")
            self.ctx.add_cleanup(log.close)
            proc = subprocess.Popen(["/usr/bin/python3", str(self._script()), "--no-browser"], cwd=str(self.project), env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.ctx.set_state("pid", proc.pid)
            for _ in range(30):
                if self._up() or proc.poll() is not None:
                    break
                time.sleep(0.2)
            if not self._up():
                raise RuntimeError(f"Course Studio did not start. See {LOG_FILE}")
        return self.status()

    def stop_it(self) -> dict[str, Any]:
        pid = self.ctx.get_state("pid", None)
        if pid:
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self.ctx.set_state("pid", None)
        for _ in range(20):
            if not self._up():
                break
            time.sleep(0.2)
        return self.status()
