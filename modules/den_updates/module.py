"""
Den versions and updates.

Every Den has a version (the VERSION file) and a changelog (CHANGELOG.md). The owner's own Den -
a git checkout, "source" - publishes versions and hands them out (releases.py); a Den given to
someone else - "install" - stays on its version until its person presses Update, then downloads
the new one from the owner's Den, checks the owner's signature, and restarts into it through
launcher.py, which goes back by itself if the new version doesn't start (updater.py).

The dashboard reaches this through module commands (POST /modules/commands/<name>, {"arg": JSON}):

    den-version          {version, kind, commit, dirty, tag}
    den-changelog        {entries: [{version, date, title, items}]}
    den-update-status    where updates come from, the latest version seen, history
    den-update-check     ask the owner's Den now (changes nothing)
    den-update-install   download, check, restart into the new version
    den-update-source    {url, key, public_key} | {clear: true}
    den-release-status   owner: what would be published, past releases, installs, the server
    den-release-publish  owner: {version, title, items}
    den-install-add      owner: {name} -> a new update key, shown once
    den-install-remove   owner: {id}

A module, so a change here needs POST /modules/den_updates/reload, never a restart.
"""

import json
import os
import socket
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import common, releases, updater
from .common import UpdateError


def _arg(text: str) -> dict[str, Any]:
    if not (text or "").strip():
        return {}
    try:
        v = json.loads(text)
    except ValueError:
        raise UpdateError("The request wasn't understood")
    if not isinstance(v, dict):
        raise UpdateError("The request wasn't understood")
    return v


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        self.root = common.ROOT
        self.kind = common.kind(self.root)
        self.publisher = releases.Publisher(self.root, self.root / "releases")
        self.installs = releases.Installs(ctx.get_state, ctx.set_state)
        self.updater = updater.Updater(self.root, ctx.get_state, ctx.set_state, self._restart)
        self.server = None
        self.auto = updater.AutoCheck(self.updater, float(ctx.config["check_hours"]), ctx.log)
        for name, fn, desc in (
            ("den-version", self.version, "This Den's version"),
            ("den-changelog", lambda a: {"entries": common.read_changelog(self.root)}, "What each version brought"),
            ("den-update-status", lambda a: self.updater.status(), "Where updates come from and the latest version"),
            ("den-update-check", lambda a: self.updater.check(), "Look for a new version now"),
            ("den-update-install", lambda a: self.updater.install(), "Install the new version and restart"),
            ("den-update-source", lambda a: self.updater.set_source(_arg(a)), "Set where updates come from"),
            ("den-release-status", self.release_status, "Owner: versions, installs, release server"),
            ("den-release-publish", self.publish, "Owner: publish a new version"),
            ("den-install-add", self.install_add, "Owner: make an update key for someone's Den"),
            ("den-install-remove", self.install_remove, "Owner: stop a Den getting updates"),
        ):
            ctx.register_command(name, fn, desc)

    def start(self):
        self._serve()
        if self.kind == "install":
            self.auto.start()

    def _serve(self) -> None:
        """The release server runs on the owner's Den only while some other Den has an update key,
        so no port is opened before anyone needs it."""
        want = self.kind == "source" and self.ctx.config["serve"] and bool(self.installs.public())
        if want and not self.server:
            self.server = releases.ReleaseServer(self.ctx.config["serve_host"], int(self.ctx.config["serve_port"]),
                                                 self.publisher, self.installs)
            self.server.start()
        elif not want and self.server:
            self.server.stop()
            self.server = None

    def stop(self):
        self.auto.stop()
        if self.server:
            self.server.stop()
            self.server = None

    # ------------------------------------------------------------ commands
    def version(self, _arg_text: str = "") -> dict[str, Any]:
        out = {"version": common.read_version(self.root), "kind": self.kind, "commit": None, "dirty": 0, "tag": None}
        if self.kind == "source":
            out["commit"] = common.git(self.root, "rev-parse", "--short", "HEAD", check=False).strip() or None
            out["dirty"] = len(common.git(self.root, "status", "--porcelain", "--untracked-files=all", check=False).splitlines())
            out["tag"] = common.git(self.root, "describe", "--tags", "--exact-match", "HEAD", check=False).strip() or None
        else:
            release = updater.launcher._read(self.root / "RELEASE.json", {})
            out["commit"] = release.get("commit") if isinstance(release, dict) else None
        return out

    def _owner_only(self) -> None:
        if self.kind != "source":
            raise UpdateError("Only the owner's own Den publishes versions")

    def release_status(self, _arg_text: str = "") -> dict[str, Any]:
        self._owner_only()
        out = self.publisher.status()
        out["installs"] = self.installs.public()
        out["server"] = self._server_view()
        try:
            out["public_key"] = releases.public_key_b64()
        except Exception as e:
            out["public_key"] = ""
            out["key_error"] = f"The signing key couldn't be read from the Keychain ({e})"
        return out

    def publish(self, arg_text: str) -> dict[str, Any]:
        self._owner_only()
        a = _arg(arg_text)
        items = a.get("items") or []
        if isinstance(items, str):
            items = items.splitlines()
        return {"release": self.publisher.publish(str(a.get("version") or ""), str(a.get("title") or ""), items)}

    def install_add(self, arg_text: str) -> dict[str, Any]:
        self._owner_only()
        install, key = self.installs.add(_arg(arg_text).get("name", ""))
        self._serve()
        urls = self._server_view()["urls"]
        return {"install": install, "key": key, "source_url": urls[0] if urls else "",
                "public_key": releases.public_key_b64()}

    def install_remove(self, arg_text: str) -> dict[str, Any]:
        self._owner_only()
        ok = self.installs.remove(str(_arg(arg_text).get("id") or ""))
        self._serve()
        return {"ok": ok}

    # ------------------------------------------------------------ helpers
    def _server_view(self) -> dict[str, Any]:
        port = self.server.port if self.server else int(self.ctx.config["serve_port"])
        hosts = []
        den_url = urlparse(os.environ.get("DEN_URL", "")).hostname
        for h in (den_url, socket.gethostname()):
            if h and h not in ("127.0.0.1", "localhost") and h not in hosts:
                hosts.append(h)
        return {"running": bool(self.server and self.server.running), "port": port,
                "error": self.server.error if self.server else "", "urls": [f"http://{h}:{port}" for h in hosts]}

    def _restart(self) -> None:
        """A normal restart through the Den's own API: running tasks finish first."""
        key = self.ctx.secret("API_KEY") or ""
        port = os.environ.get("PORT", "8787")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/service/restart", data=b'{"hard": false}', method="POST",
                                     headers={"Content-Type": "application/json", "X-API-Key": key})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
