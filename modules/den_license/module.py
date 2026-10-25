"""
Licensing, wired to the Den.

On the OWNER's Den (a git checkout): the owner creates, lists, edits and revokes licences, and a small
check-in server runs (its own port, only while at least one licence exists) so given-away Dens can
check in. Owner commands:
    den-license-create   {name, tools?, expires_days?, max_offline_days?}  -> the licence + key to hand over
    den-license-list                                                       -> every licence + the public key + server
    den-license-set-tools {id, tools}      change what a person may use (live, at their next check-in)
    den-license-revoke   {id, revoked?}    turn a licence off (or back on)
    den-license-remove   {id}
    den-license-tools                      the list of tools you can allow

On a GIVEN-AWAY Den (an install): it stores its licence, checks in on a schedule, and if the licence is
missing / revoked / on another machine / offline too long it LOCKS - it pauses all work (queue_paused)
and records why, so the dashboard can explain it. It never deletes anything. Install commands:
    den-license-install  {token, public_key, server_url}   paste what the owner gave you
    den-license-state                                       am I licensed? which tools? why locked?
    den-license-checkin                                     check in now

Both: den-license-info -> {kind, machine_id}. A module, so changes need POST /modules/den_license/reload.

NOTE: this module enforces the licence at the lane level (it pauses the queue when locked) and publishes
the allowed-tools list in the `den_license` setting. Enforcing the tool list inside a run, and a hard
licence gate at startup, are a core change (a restart) and are done separately.
"""

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import db

from . import license as lic
from .license import LicenseError

ROOT = Path(__file__).resolve().parents[2]
SETTING = "den_license"          # the current decision, for the dashboard and the core to read
PAUSED_BY_US = "paused_by_license"


def _kind() -> str:
    return "source" if (ROOT / ".git").exists() else "install"


def _catalog() -> list[str]:
    try:
        data = json.loads((ROOT / "tools.json").read_text())
        rows = data if isinstance(data, list) else data.get("tools", [])
        ids = [t.get("id") for t in rows if isinstance(t, dict) and t.get("id")]
        return ids or lic.DEFAULT_CATALOG
    except (OSError, ValueError, AttributeError):
        return lic.DEFAULT_CATALOG


def _arg(text: str) -> dict[str, Any]:
    if not (text or "").strip():
        return {}
    try:
        v = json.loads(text)
    except ValueError:
        raise LicenseError("The request wasn't understood.")
    if not isinstance(v, dict):
        raise LicenseError("The request wasn't understood.")
    return v


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        self.kind = _kind()
        self.catalog = _catalog()
        self.manager = lic.LicenseManager(ctx.get_state, ctx.set_state, catalog=self.catalog)
        self.client = lic.CheckinClient(ctx.get_state, ctx.set_state)
        self.server = None
        self._stop = threading.Event()
        self._thread = None
        cmds = [
            ("den-license-info", self.info, "This Den's licence role and machine id"),
            ("den-license-tools", lambda a: {"catalog": self.catalog}, "Tools you can allow"),
        ]
        if self.kind == "source":
            cmds += [
                ("den-license-create", self.create, "Owner: issue a licence for someone"),
                ("den-license-list", self.list_, "Owner: every licence + the public key"),
                ("den-license-set-tools", self.set_tools, "Owner: change a person's tools"),
                ("den-license-revoke", self.revoke, "Owner: turn a licence off or on"),
                ("den-license-remove", self.remove, "Owner: delete a licence"),
            ]
        else:
            cmds += [
                ("den-license-install", self.install, "Install: enter the licence the owner gave you"),
                ("den-license-state", lambda a: self.client.state(), "Install: licence status and tools"),
                ("den-license-checkin", lambda a: self._checkin(), "Install: check in now"),
            ]
        for name, fn, desc in cmds:
            ctx.register_command(name, fn, desc)

    def start(self):
        if self.kind == "source":
            self._serve()
        else:
            self._apply()                                     # decide + enforce right away
            self._thread = threading.Thread(target=self._loop, name="den-license", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self.server:
            self.server.stop()
            self.server = None

    # ------------------------------------------------------------ owner
    def _serve(self):
        want = self.kind == "source" and self.ctx.config["serve"] and bool(self.manager._all())
        if want and not self.server:
            self.server = lic.LicenseServer(self.ctx.config["serve_host"], int(self.ctx.config["serve_port"]),
                                            self.manager, lic.public_key_b64())
            self.server.start()
        elif not want and self.server:
            self.server.stop()
            self.server = None

    def info(self, _a=""):
        return {"kind": self.kind, "machine_id": lic.machine_id()}

    def _server_view(self):
        port = self.server.port if self.server else int(self.ctx.config["serve_port"])
        hosts, den_url = [], urlparse(os.environ.get("DEN_URL", "")).hostname
        for h in (den_url, socket.gethostname()):
            if h and h not in ("127.0.0.1", "localhost") and h not in hosts:
                hosts.append(h)
        return {"running": bool(self.server and self.server.running), "port": port,
                "error": self.server.error if self.server else "", "urls": [f"http://{h}:{port}" for h in hosts]}

    def create(self, arg_text):
        a = _arg(arg_text)
        url = self._server_view()["urls"]
        record, token = self.manager.create(a.get("name", ""), a.get("tools", "*"),
                                            a.get("expires_days"), a.get("max_offline_days", 30),
                                            server_url=(url[0] if url else ""))
        self._serve()                                         # start the server now there's a licence
        return {"license": {k: record.get(k) for k in ("id", "name", "tools", "expires_at", "max_offline_days")},
                "token": token, "public_key": lic.public_key_b64(), "server_url": url[0] if url else ""}

    def list_(self, _a=""):
        return {"licenses": self.manager.public(), "public_key": lic.public_key_b64(),
                "server": self._server_view(), "catalog": self.catalog}

    def set_tools(self, arg_text):
        a = _arg(arg_text)
        return {"ok": self.manager.set_tools(str(a.get("id") or ""), a.get("tools", "*"))}

    def revoke(self, arg_text):
        a = _arg(arg_text)
        ok = self.manager.revoke(str(a.get("id") or ""), bool(a.get("revoked", True)))
        return {"ok": ok}

    def remove(self, arg_text):
        ok = self.manager.remove(str(_arg(arg_text).get("id") or ""))
        self._serve()
        return {"ok": ok}

    # ------------------------------------------------------------ install
    def install(self, arg_text):
        a = _arg(arg_text)
        state = self.client.install(str(a.get("token") or ""), str(a.get("public_key") or ""), str(a.get("server_url") or ""))
        self._checkin()
        return self.client.state()

    def _checkin(self):
        self.client.checkin(version=lic.__dict__.get("VERSION", "") or self._version())
        return self._apply()

    def _version(self):
        try:
            return (ROOT / "VERSION").read_text().strip()
        except OSError:
            return ""

    def _apply(self) -> dict[str, Any]:
        """Act on the current decision: pause the queue when locked, let it go when licensed."""
        state = self.client.state()
        db.set_setting(SETTING, {"status": state["status"], "reason": state["reason"],
                                 "tools": state.get("tools", []), "offline_days": state.get("offline_days"),
                                 "checked_at": lic._iso(lic._now())})
        locked = state["status"] in ("locked", "none")
        if locked:
            if not db.get_setting("queue_paused"):
                db.set_setting("queue_paused", True)
                self.ctx.set_state(PAUSED_BY_US, True)
            self.ctx.log.info("licence locked: %s", state["reason"])
        elif self.ctx.get_state(PAUSED_BY_US):
            db.set_setting("queue_paused", False)             # we paused it; we lift it
            self.ctx.set_state(PAUSED_BY_US, False)
        return state

    def _loop(self):
        if self._stop.wait(20):
            return
        last_checkin = 0.0
        period = max(0.25, float(self.ctx.config["check_hours"])) * 3600
        while not self._stop.is_set():
            now = time.time()
            if self.client.state().get("configured") and now - last_checkin >= period:
                try:
                    self.client.checkin(version=self._version())
                except Exception as e:
                    self.ctx.log.info("check-in: %s", e)
                last_checkin = now
            try:
                self._apply()                                 # re-decide (offline window may have passed)
            except Exception as e:
                self.ctx.log.info("licence apply: %s", e)
            if self._stop.wait(900):                          # re-check every 15 minutes
                return
