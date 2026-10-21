"""
Sharing projects and chats with other Den users.

The owner's Den stays the one that does the work: guests see the same tasks and data, add tasks and chat (live),
and this Den runs everything. Nothing here needs the guest's own computer to be on, only this one.

Commands (JSON argument, like den-analytics):
  collab         the owner's side - status, invite, update, share, unshare, remove, newlink, approve, decline, events
  collab-guest   the guest's side - list / add / remove the Dens that shared something with this person

The guest page is served by server.py on its own port (config `serve_port`). It is off until the owner turns
`serve` on (PATCH /modules/den_collab/config), so a fresh Den opens no extra port.
"""

import json
import os
import socket
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

from den_modules import events

from . import store
from .server import Hub, Refused, ShareServer


def _args(text: str) -> dict[str, Any]:
    try:
        d = json.loads(text or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def lan_hosts() -> list[str]:
    """Addresses other people could reach this Den on: its network address, its name, and its Tailscale name if set."""
    out: list[str] = []
    den_host = urlparse(os.environ.get("DEN_URL", "")).hostname
    for h in (den_host, _lan_ip(), socket.gethostname()):
        if h and h not in ("127.0.0.1", "localhost", "0.0.0.0") and h not in out:
            out.append(h)
    return out


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("10.255.255.255", 1))      # no packet is sent; it just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return ""


def parse_link(link: str) -> tuple[str, str]:
    """('http://host:8791', 'token') from an invite link like http://host:8791/#join=TOKEN."""
    u = urlparse((link or "").strip())
    token = ""
    for part in u.fragment.split("&"):
        if part.startswith("join="):
            token = part[5:]
    if u.scheme not in ("http", "https") or not u.netloc or not token:
        raise ValueError("That doesn't look like an invite link. It should look like http://name:8791/#join=...")
    return f"{u.scheme}://{u.netloc}", token


def _get(url: str, token: str = "", timeout: float = 4) -> Any:
    req = urllib.request.Request(url, headers={"X-Collab-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class Module:
    def initialize(self, ctx: Any) -> None:
        self.ctx = ctx
        self.server: Optional[ShareServer] = None
        store.init()
        ctx.subscribe(events.TASK_ADDED, lambda task: self.server and self.server.hub.hold_if_expected(task))
        ctx.register_command("collab", self.owner, "Share projects and chats: people, invites, roles")
        ctx.register_command("collab-guest", self.guest, "Dens that shared something with this person")

    def start(self) -> None:
        cfg = self.ctx.config
        if not cfg.get("serve"):
            return
        hub = Hub(self.ctx.secret("API_KEY") or "", cfg.get("host_name") or socket.gethostname(), os.environ.get("PORT", "8787"))
        self.server = ShareServer(cfg.get("serve_host") or "0.0.0.0", int(cfg.get("serve_port") or 8791), hub)
        self.server.start()

    def stop(self) -> None:
        if self.server:
            self.server.stop()
            self.server = None

    # ------------------------------------------------------------- owner side

    def _bases(self) -> list[str]:
        cfg = self.ctx.config
        port = self.server.port if self.server else int(cfg.get("serve_port") or 8791)
        own = (cfg.get("public_url") or "").strip().rstrip("/")
        return ([own] if own else []) + [f"http://{h}:{port}" for h in lan_hosts()]

    def owner(self, arg_text: str) -> dict[str, Any]:
        """A problem comes back as {"error": "..."} so the page can show the plain sentence (a raised error would
        reach it only as "command failed")."""
        a = _args(arg_text)
        fn = getattr(self, "_o_" + str(a.get("action") or "status"), None)
        if not fn:
            return {"error": "Unknown action"}
        try:
            return fn(a)
        except Refused as e:
            return {"error": e.message}
        except ValueError as e:
            return {"error": str(e)}
        except (KeyError, TypeError):
            return {"error": "Something was missing from that request"}

    def _links(self, token: str) -> list[str]:
        return [f"{b}/#join={token}" for b in self._bases()]

    def _o_status(self, a: dict[str, Any]) -> dict[str, Any]:
        import db
        projects = [{"id": p["id"], "name": p["name"]} for p in db.list_projects(active_only=True) if not p.get("hidden")]
        names = {p["id"]: p["name"] for p in projects}
        members = store.list_members()
        for m in members:
            for s in m["shares"]:
                s["name"] = names.get(s["ref_id"]) if s["kind"] == "project" else ((db.get_task(s["ref_id"]) or {}).get("title") or "A chat")
        held = store.held_by_guests()
        for h in held:
            h["project"] = names.get(h["project_id"], "")
            h["prompt"] = (h["prompt"] or "")[:200]
        return {"serving": bool(self.server and self.server.running), "port": self.server.port if self.server else self.ctx.config.get("serve_port"),
                "server_error": self.server.error if self.server else "", "urls": self._bases(), "members": members, "held": held, "projects": projects}

    def _o_invite(self, a: dict[str, Any]) -> dict[str, Any]:
        name = str(a.get("name") or "").strip()
        if not name:
            raise ValueError("Their name, please")
        shares = a.get("shares") or []
        if not shares:
            raise ValueError("Pick a project or chat to share")
        member, token = store.create_member(name, str(a.get("email") or ""), hold=bool(a.get("hold")))
        for s in shares:
            store.set_share(member["id"], s.get("kind", "project"), int(s["ref_id"]), s.get("role", "viewer"))
            if s.get("kind", "project") == "project":
                store.log_event(int(s["ref_id"]), "Owner", f"shared this project with {name}")
        return {"member": store.get_member(member["id"]), "links": self._links(token)}

    def _o_newlink(self, a: dict[str, Any]) -> dict[str, Any]:
        token = store.reset_token(int(a["member_id"]))
        if not token:
            raise ValueError("No such person")
        return {"links": self._links(token)}

    def _o_update(self, a: dict[str, Any]) -> dict[str, Any]:
        store.update_member(int(a["member_id"]), a.get("name"), a.get("email"), a.get("hold") if "hold" in a else None)
        return {"ok": True}

    def _o_share(self, a: dict[str, Any]) -> dict[str, Any]:
        store.set_share(int(a["member_id"]), a.get("kind", "project"), int(a["ref_id"]), a.get("role", "viewer"))
        return {"ok": True}

    def _o_unshare(self, a: dict[str, Any]) -> dict[str, Any]:
        store.remove_share(int(a["member_id"]), a.get("kind", "project"), int(a["ref_id"]))
        return {"ok": True}

    def _o_remove(self, a: dict[str, Any]) -> dict[str, Any]:
        store.remove_member(int(a["member_id"]))
        return {"ok": True}

    def _o_events(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"events": store.events(int(a["project_id"]), 60)}

    def _hub_call(self, method: str, path: str) -> Any:
        hub = Hub(self.ctx.secret("API_KEY") or "", "", os.environ.get("PORT", "8787"))
        return hub.den(method, path)

    def _o_approve(self, a: dict[str, Any]) -> dict[str, Any]:
        self._hub_call("POST", f"/tasks/{int(a['task_id'])}/play")
        return {"ok": True}

    def _o_decline(self, a: dict[str, Any]) -> dict[str, Any]:
        self._hub_call("POST", f"/tasks/{int(a['task_id'])}/cancel")
        return {"ok": True}

    # ------------------------------------------------------------- guest side

    def _conns(self) -> list[dict[str, Any]]:
        return list(self.ctx.get_state("connections", []) or [])

    def guest(self, arg_text: str) -> dict[str, Any]:
        try:
            return self._guest(_args(arg_text))
        except ValueError as e:
            return {"error": str(e)}

    def _guest(self, a: dict[str, Any]) -> dict[str, Any]:
        act = a.get("action") or "list"
        conns = self._conns()
        if act == "add":
            try:
                base, token = parse_link(str(a.get("link") or ""))
            except ValueError as e:
                raise ValueError(str(e))
            try:
                me = _get(base + "/api/me", token)
            except urllib.error.HTTPError as e:
                raise ValueError("That link isn't valid any more - ask for a new one" if e.code == 401 else f"The other Den said no ({e.code})")
            except (urllib.error.URLError, OSError, ValueError):
                raise ValueError("Couldn't reach that Den. Is their computer on, and are you on the same network (or Tailscale)?")
            conns = [c for c in conns if not (c["url"] == base and c["token"] == token)]
            conns.append({"id": uuid.uuid4().hex[:8], "url": base, "token": token, "host": me.get("host") or urlparse(base).hostname, "as": me.get("name", "")})
            self.ctx.set_state("connections", conns)
        elif act == "remove":
            conns = [c for c in conns if c["id"] != a.get("id")]
            self.ctx.set_state("connections", conns)
        out: list[dict[str, Any]] = [None] * len(conns)     # type: ignore[list-item]

        def probe(i: int, c: dict[str, Any]) -> None:
            row = {"id": c["id"], "url": c["url"], "host": c["host"], "as": c.get("as", ""), "open": f"{c['url']}/#join={c['token']}",
                   "online": False, "error": "", "projects": [], "chats": []}
            try:
                me = _get(c["url"] + "/api/me", c["token"], timeout=3)
                row.update(online=True, projects=me.get("projects", []), chats=me.get("chats", []), **{"as": me.get("name", c.get("as", ""))})
            except urllib.error.HTTPError as e:
                row["error"] = "Your access was removed" if e.code == 401 else f"Error {e.code}"
            except (urllib.error.URLError, OSError, ValueError):
                row["error"] = "Their Den is off or not reachable right now"
            out[i] = row

        threads = [threading.Thread(target=probe, args=(i, c)) for i, c in enumerate(conns)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(6)
        return {"connections": [o for o in out if o]}
