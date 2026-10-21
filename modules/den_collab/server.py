"""
The small web server a shared project is reached through - its own port (8791), not the Den's own, like the
release server in den_updates. The Den's own API and the owner's login are never opened to guests: a guest holds one
private token (from their invite link) and this server only answers the few things that token was given.

  GET  /                                  the guest page (guest.html)
  GET  /api/ping                          is this Den up (no token)
  GET  /api/me                            who am I, what was shared with me
  GET  /api/projects/{id}                 the project + people + recent activity
  GET  /api/projects/{id}/tasks           its chats and tasks
  POST /api/projects/{id}/tasks           start a new task or chat                        (editor)
  GET  /api/projects/{id}/notes | timeline | documents
  GET  /api/documents/{id}                download a file
  GET  /api/chats/{root}?after=&wait=     the messages; waits up to `wait` seconds for something new (live chat)
  POST /api/chats/{root}/messages         add a message to a chat                          (editor)
  POST /api/presence                      "I'm here" / "I'm typing"
  GET/POST/DELETE /api/projects/{id}/people   see / invite / remove people                  (admin)

Everything else (tasks, replies, the files themselves) is done by the Den's own API with the owner's key, so the
same checks and the same queue apply as for the owner. Guests never see folder paths, run steps, keys or costs.
"""

import calendar
import json
import logging
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import db
import documents

from . import store

log = logging.getLogger("the-den.module.den_collab")

PAGE = Path(__file__).with_name("guest.html")
MAX_BODY = 64 * 1024
MAX_PROMPT = 8000
MAX_WAIT = 25
PRESENCE_SECONDS = 30
FAIL_LIMIT, FAIL_WINDOW = 20, 600
SAFE_STATUS = {"queued": "waiting", "running": "working", "done": "done", "reviewed": "done", "error": "failed",
               "failed": "failed", "cancelled": "stopped", "rate_limited": "waiting", "interrupted": "stopped"}


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Hub:
    """Everything the handler needs, so a reload of the module replaces it cleanly."""

    def __init__(self, api_key: str, host_name: str, den_port: str, on_event: Optional[Callable[[str], None]] = None):
        self.api_key, self.host_name, self.den_port = api_key, host_name, den_port
        self._holds: list[tuple[float, int, Optional[int], str]] = []
        self._presence: dict[str, dict[int, tuple[float, bool, str]]] = {}
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------------- the Den's own API

    def den(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.den_port}{path}", data=data, method=method,
                                     headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode()).get("detail", "")
            except (ValueError, AttributeError):
                pass
            raise Refused(e.code if e.code in (400, 404, 409, 422) else 502, detail if isinstance(detail, str) and detail else "The Den couldn't do that")
        except (urllib.error.URLError, OSError):
            raise Refused(502, "The Den is busy or restarting - try again in a moment")
        return json.loads(raw.decode()) if raw else None

    # ------------------------------------------------- tasks held for the owner's OK

    def expect_hold(self, project_id: int, thread_id: Optional[int], prompt: str) -> None:
        """A guest's task is about to be created: pause it the instant the Den adds it (see hold_if_expected)."""
        with self._lock:
            self._holds = [h for h in self._holds if time.time() - h[0] < 15]
            self._holds.append((time.time(), project_id, thread_id, prompt))

    def hold_if_expected(self, task: dict[str, Any]) -> None:
        """TASK_ADDED listener: runs on the thread that added the task, right after it was saved."""
        with self._lock:
            for h in self._holds:
                if h[1] == task.get("project_id") and h[2] == task.get("thread_id") and h[3] == task.get("prompt") and time.time() - h[0] < 15:
                    self._holds.remove(h)
                    break
            else:
                return
        db.set_paused(task["id"], True)

    def forget_hold(self, project_id: int, thread_id: Optional[int], prompt: str) -> None:
        with self._lock:
            self._holds = [h for h in self._holds if h[1:] != (project_id, thread_id, prompt)]

    # ------------------------------------------------------------ lockout

    def locked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
            return len(self._fails[ip]) >= FAIL_LIMIT

    def failed(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    # ----------------------------------------------------------- presence

    def seen(self, scope: str, member: dict[str, Any], typing: bool = False) -> None:
        with self._lock:
            self._presence.setdefault(scope, {})[member["id"]] = (time.time(), typing, member["name"])

    def here(self, scope: str, me: Optional[int] = None) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            room = self._presence.get(scope, {})
            for mid in [m for m, (t, _, _) in room.items() if now - t > PRESENCE_SECONDS]:
                del room[mid]
            return [{"name": n, "typing": ty and now - t < 6, "me": mid == me} for mid, (t, ty, n) in sorted(room.items(), key=lambda kv: kv[1][2])]


# ----------------------------------------------------------------- access

def project_role(member: dict[str, Any], project_id: int) -> Optional[str]:
    for s in store.shares_of(member["id"]):
        if s["kind"] == "project" and s["ref_id"] == project_id:
            return s["role"]
    return None


def chat_root(task_id: int) -> Optional[dict[str, Any]]:
    t = db.get_task(task_id)
    if not t or t["status"] == "deleted":
        return None
    return db.get_task(t["thread_id"]) if t.get("thread_id") else t


def chat_role(member: dict[str, Any], root: dict[str, Any]) -> Optional[str]:
    """A project share covers every chat in it; otherwise a share of that one chat."""
    role = project_role(member, root["project_id"])
    if role:
        return role
    for s in store.shares_of(member["id"]):
        if s["kind"] == "chat" and s["ref_id"] == root["id"]:
            return s["role"]
    return None


def need(role: Optional[str], at_least: str = "viewer") -> str:
    if not role:
        raise Refused(404, "Not found")          # the same answer whether it doesn't exist or isn't shared
    if not store.at_least(role, at_least):
        raise Refused(403, "You can only read this - ask the owner for editor access")
    return role


# ---------------------------------------------------------------- shaping

def _msg(t: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    status = "waiting" if t.get("paused") and t["status"] in ("queued", "rate_limited") else SAFE_STATUS.get(t["status"], "waiting")
    return {"id": t["id"], "prompt": t["prompt"], "reply": t.get("result_summary") or "", "status": status,
            "held": bool(t.get("paused")) and t["status"] in ("queued", "rate_limited"), "by": names.get(t["id"], ""),
            "at": t["created_at"], "done_at": t.get("finished_at")}


def _chat_view(root: dict[str, Any], role: str) -> dict[str, Any]:
    tasks = db.get_thread(root["id"])
    names = store.authors([t["id"] for t in tasks])
    return {"id": root["id"], "project_id": root["project_id"], "title": root.get("title") or (root["prompt"] or "")[:60], "role": role,
            "kind": root.get("kind") or "task", "messages": [_msg(t, names) for t in tasks]}


def _signature(root_id: int) -> str:
    return "|".join(f"{t['id']}:{t['status']}:{t.get('paused', 0)}:{len(t.get('result_summary') or '')}" for t in db.get_thread(root_id))


def _project_cards(member: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for s in store.shares_of(member["id"]):
        if s["kind"] == "project":
            p = db.get_project(s["ref_id"])
            if p and p.get("active", 1) != 0:
                out.append({"id": p["id"], "name": p["name"], "status": p.get("status"), "description": p.get("description") or "",
                            "due_date": p.get("due_date"), "role": s["role"]})
    return out


def _chat_cards(member: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for s in store.shares_of(member["id"]):
        if s["kind"] == "chat":
            root = chat_root(s["ref_id"])
            if root:
                out.append({"id": root["id"], "title": root.get("title") or (root["prompt"] or "")[:60], "project_id": root["project_id"], "role": s["role"]})
    return out


def _task_list(project_id: int) -> list[dict[str, Any]]:
    """The project's chats and tasks, newest activity first: one row per conversation."""
    rows = db.list_all_tasks(limit=600, project_id=project_id, hidden=False)
    roots: dict[int, dict[str, Any]] = {}
    for t in sorted(rows, key=lambda r: r["id"]):
        rid = t.get("thread_id") or t["id"]
        r = roots.setdefault(rid, {"id": rid, "title": "", "kind": "task", "messages": 0, "status": "waiting", "updated": t["created_at"], "last": ""})
        if not t.get("thread_id"):
            r["title"] = t.get("title") or (t["prompt"] or "")[:70]
            r["kind"] = t.get("kind") or "task"
        r["messages"] += 1
        r["status"] = SAFE_STATUS.get(t["status"], "waiting")
        r["updated"] = t.get("finished_at") or t.get("started_at") or t["created_at"]
        r["last"] = (t.get("result_summary") or t["prompt"] or "")[:140]
    out = [r for r in roots.values() if r["title"]]
    out.sort(key=lambda r: r["updated"] or "", reverse=True)
    names = store.authors([r["id"] for r in out])
    for r in out:
        r["by"] = names.get(r["id"], "")
    return out


# ----------------------------------------------------------------- handler

class _Handler(BaseHTTPRequestHandler):
    server_version = "TheDenShare"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:    # the Den's own log stays quiet
        pass

    # ---- replies
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    # ---- the request
    def _body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Refused(400, "Bad request")
        if n > MAX_BODY:
            raise Refused(413, "That's too big")
        if n == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode())
        except (ValueError, UnicodeDecodeError):
            raise Refused(400, "Bad request")
        if not isinstance(data, dict):
            raise Refused(400, "Bad request")
        return data

    def _member(self, hub: Hub) -> dict[str, Any]:
        ip = self.client_address[0]
        if hub.locked(ip):
            raise Refused(429, "Too many wrong tries - wait a few minutes")
        m = store.by_token(self.headers.get("X-Collab-Token", ""))
        if not m:
            hub.failed(ip)
            raise Refused(401, "This link isn't valid any more - ask the owner for a new one")
        store.touch(m["id"])
        return m

    def _route(self, method: str) -> None:
        hub: Hub = self.server.hub     # type: ignore[attr-defined]
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if method == "GET" and not parts:
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                               "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"})
            if not parts:
                raise Refused(404, "Not found")
            if parts[:2] == ["api", "ping"]:
                return self._json(200, {"ok": True, "host": hub.host_name})
            if parts[0] != "api":
                raise Refused(404, "Not found")
            member = self._member(hub)
            result = self._api(hub, member, method, parts[1:], q, self._body() if method in ("POST", "PATCH", "DELETE") else {})
            if isinstance(result, tuple):          # a file: (bytes, name)
                data, name = result
                return self._send(200, data, "application/octet-stream",
                                  {"Content-Disposition": f'attachment; filename="{name.replace(chr(34), "")}"'})
            self._json(200, result)
        except Refused as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("sharing request failed: %s %s", method, url.path)
            self._json(500, {"error": "Something went wrong on the owner's Den"})

    def do_GET(self) -> None:       # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:      # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:    # noqa: N802
        self._route("DELETE")

    # ---- the API
    def _api(self, hub: Hub, me: dict[str, Any], method: str, p: list[str], q: dict[str, str], body: dict[str, Any]) -> Any:
        if p == ["me"] and method == "GET":
            return {"name": me["name"], "host": hub.host_name, "projects": _project_cards(me), "chats": _chat_cards(me)}
        if p == ["presence"] and method == "POST":
            scope = self._scope(me, body)
            hub.seen(scope, me, bool(body.get("typing")))
            return {"here": hub.here(scope, me["id"])}

        if len(p) >= 2 and p[0] == "projects":
            pid = self._int(p[1])
            role = need(project_role(me, pid))
            project = db.get_project(pid)
            if not project:
                raise Refused(404, "Not found")
            rest = p[2:]
            if not rest and method == "GET":
                return {"id": pid, "name": project["name"], "status": project.get("status"), "description": project.get("description") or "",
                        "due_date": project.get("due_date"), "role": role, "links": [l for l in (project.get("links") or []) if isinstance(l, dict)],
                        "people": [{"name": m["name"], "role": m["role"], "online": self._recent(m["last_seen"])} for m in store.members_of_project(pid)],
                        "activity": store.events(pid, 30), "here": hub.here(f"p:{pid}", me["id"])}
            if rest == ["tasks"] and method == "GET":
                return {"items": _task_list(pid)}
            if rest == ["tasks"] and method == "POST":
                need(role, "editor")
                return self._new_chat(hub, me, project, body)
            if rest == ["notes"] and method == "GET":
                return {"notes": [{"id": n["id"], "kind": n["kind"], "text": n["text"], "at": n["created_at"]}
                                  for n in db.list_project_notes(pid) if n.get("active", 1)]}
            if rest == ["timeline"] and method == "GET":
                data = hub.den("GET", f"/projects/{pid}/timeline")
                keep = ("id", "title", "kind", "date", "time", "end_date", "status", "when", "notes", "location")
                return {"events": [{k: e.get(k) for k in keep if k in e} for e in data.get("events", [])], "health": data.get("health")}
            if rest == ["documents"] and method == "GET":
                return {"documents": [{"id": d["id"], "name": d["name"], "size": d["size"], "at": d["created_at"]} for d in db.list_project_documents(pid)]}
            if rest == ["people"]:
                return self._people(me, pid, role, method, body)
            raise Refused(404, "Not found")

        if len(p) == 2 and p[0] == "documents" and method == "GET":
            doc = db.get_project_document(self._int(p[1]))
            need(project_role(me, doc["project_id"]) if doc else None)
            path = documents.path_of(doc)
            if path is None:
                raise Refused(404, "That file isn't there any more")
            return path.read_bytes(), doc["name"]

        if len(p) >= 2 and p[0] == "chats":
            root = chat_root(self._int(p[1]))
            role = need(chat_role(me, root) if root else None)
            if len(p) == 2 and method == "GET":
                scope = f"c:{root['id']}"
                hub.seen(scope, me)
                sig = _signature(root["id"])
                if q.get("sig") == sig:                       # nothing new yet: wait a little (the live part of live chat)
                    deadline = time.time() + min(MAX_WAIT, max(0, self._int(q.get("wait") or "0")))
                    here_sig = json.dumps(hub.here(scope, me["id"]))
                    while time.time() < deadline:
                        time.sleep(0.6)
                        hub.seen(scope, me)
                        if _signature(root["id"]) != sig or json.dumps(hub.here(scope, me["id"])) != here_sig:
                            break
                view = _chat_view(root, role)
                view["sig"] = _signature(root["id"])
                view["here"] = hub.here(f"c:{root['id']}", me["id"])
                return view
            if p[2:] == ["messages"] and method == "POST":
                need(role, "editor")
                prompt = self._prompt(body)
                task = self._create(hub, me, root["project_id"], {"prompt": prompt, "thread_id": root["id"]})
                self._after_create(me, root["project_id"], task, "replied in a chat")
                return {"id": task["id"]}
        raise Refused(404, "Not found")

    # ---- helpers
    @staticmethod
    def _int(s: str) -> int:
        try:
            return int(s)
        except ValueError:
            raise Refused(404, "Not found")

    @staticmethod
    def _recent(ts: Optional[str]) -> bool:
        try:
            return bool(ts) and time.time() - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) < 120
        except ValueError:
            return False

    @staticmethod
    def _prompt(body: dict[str, Any]) -> str:
        text = body.get("prompt")
        if not isinstance(text, str) or not text.strip():
            raise Refused(400, "Write something first")
        if len(text) > MAX_PROMPT:
            raise Refused(400, "That message is too long")
        return text.strip()

    def _scope(self, me: dict[str, Any], body: dict[str, Any]) -> str:
        if body.get("chat") is not None:
            root = chat_root(self._int(str(body["chat"])))
            need(chat_role(me, root) if root else None)
            return f"c:{root['id']}"
        pid = self._int(str(body.get("project")))
        need(project_role(me, pid))
        return f"p:{pid}"

    def _new_chat(self, hub: Hub, me: dict[str, Any], project: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        prompt = self._prompt(body)
        kind = "message" if body.get("kind") == "message" else None
        payload: dict[str, Any] = {"prompt": prompt}
        if kind:
            payload["kind"] = kind
        task = self._create(hub, me, project["id"], payload)
        self._after_create(me, project["id"], task, "started " + ("a question" if kind else "a task"))
        return {"id": task["id"]}

    @staticmethod
    def _create(hub: Hub, me: dict[str, Any], project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """The Den's own API makes the task, as for the owner; a person marked "ask me first" has theirs held."""
        thread = payload.get("thread_id")
        if me.get("hold"):
            hub.expect_hold(project_id, thread, payload["prompt"])
        try:
            return hub.den("POST", f"/projects/{project_id}/tasks", payload)
        except Refused:
            hub.forget_hold(project_id, thread, payload["prompt"])
            raise

    @staticmethod
    def _after_create(me: dict[str, Any], project_id: int, task: dict[str, Any], what: str) -> None:
        store.set_author(task["id"], me)
        store.log_event(project_id, me["name"], what)

    def _people(self, me: dict[str, Any], pid: int, role: str, method: str, body: dict[str, Any]) -> Any:
        need(role, "admin")
        if method == "GET":
            return {"people": [{"id": m["id"], "name": m["name"], "role": m["role"], "online": self._recent(m["last_seen"])} for m in store.members_of_project(pid)]}
        if method == "POST":
            name = (body.get("name") or "").strip() if isinstance(body.get("name"), str) else ""
            r = body.get("role") if body.get("role") in ("viewer", "editor") else "viewer"
            if not name:
                raise Refused(400, "Their name, please")
            member, token = store.create_member(name, str(body.get("email") or ""), hold=bool(me.get("hold")))
            store.set_share(member["id"], "project", pid, r)
            store.log_event(pid, me["name"], f"invited {member['name']}")
            return {"id": member["id"], "name": member["name"], "token": token}
        if method == "DELETE":
            mid = self._int(str(body.get("id")))
            target = next((m for m in store.members_of_project(pid) if m["id"] == mid), None)
            if not target or target["role"] == "admin" or mid == me["id"]:
                raise Refused(403, "Only the owner can remove an admin")
            store.remove_share(mid, "project", pid)
            store.log_event(pid, me["name"], f"removed {target['name']}")
            return {"ok": True}
        raise Refused(404, "Not found")


class ShareServer:
    def __init__(self, host: str, port: int, hub: Hub):
        self.host, self.port, self.hub = host, port, hub
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.error = ""

    def start(self) -> None:
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as e:
            self.error = f"Couldn't open port {self.port}: {e.strerror or e}"
            log.warning("sharing server not started: %s", self.error)
            return
        self.httpd.daemon_threads = True
        self.httpd.hub = self.hub          # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="den-share-server", daemon=True).start()
        log.info("sharing server on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    @property
    def running(self) -> bool:
        return self.httpd is not None
