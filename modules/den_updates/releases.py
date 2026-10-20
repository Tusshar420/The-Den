"""
The owner's side of Den updates: publishing a version and handing it out.

Publishing 2.2.0 (only on the owner's own copy, a git checkout):
  1. VERSION becomes 2.2.0 and the notes go at the top of CHANGELOG.md; just those two files are
     committed, and the commit is tagged v2.2.0.
  2. The bundle is built from that tag with `git archive`, so only what is saved in git goes in -
     never unsaved work, and never anything .releaseignore lists (tests, design files, plists...).
     RELEASE.json inside lists every file with its sha256.
  3. The manifest (version, notes, commit, sha256 and size of the bundle) is signed with the owner's
     Ed25519 key, kept in the Keychain. releases/ holds den-2.2.0.tar.gz and den-2.2.0.json.

Handing it out: a small server on its own port (8790 by default, not the Den's own) answers only
two questions - "what is the latest version?" and "send me version X" - and only to a Den that
sends one of the install keys made here (one per person, stored as a sha256, shown once). Every
call notes which version that Den runs and when it was last seen. Nothing else of this Den is
reachable through it.
"""

import hashlib
import hmac
import io
import json
import logging
import secrets
import tarfile
import threading
import time
from datetime import datetime, timezone
from fnmatch import fnmatch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from . import common
from .common import UpdateError

log = logging.getLogger("the-den.module.den_updates")

KEY_ACCOUNT = "den-release-signing-key"
# A release that lacks these couldn't run, or couldn't update itself again.
REQUIRED = ("main.py", "launcher.py", "VERSION", "modules/den_updates/module.py")
API = "/den-updates/v1"
MAX_NAME = 80


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- signing key

def signing_key() -> Ed25519PrivateKey:
    """The owner's release key, from the Keychain; made the first time it's needed."""
    from assistant import token_store
    stored = token_store.get(KEY_ACCOUNT)
    if stored and stored.get("private"):
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(stored["private"]))
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    token_store.put(KEY_ACCOUNT, {"private": raw.hex(), "created_at": _now()})
    return key


def public_key_b64(key: Optional[Ed25519PrivateKey] = None) -> str:
    key = key or signing_key()
    return common.b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


# ---------------------------------------------------------------- what goes in a release

def ignore_patterns(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def ignored(path: str, patterns: list[str]) -> bool:
    parts = path.split("/")
    for p in patterns:
        if p.endswith("/"):
            if any(fnmatch("/".join(parts[:i]), p[:-1]) for i in range(1, len(parts))):
                return True
        elif fnmatch(path, p) or ("/" not in p and fnmatch(parts[-1], p)):
            return True
    return False


def build_bundle(root: Path, ref: str, version: str, out: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Pack `ref` (a tag) into out as .tar.gz, leaving out .releaseignore. Returns sha256, size, files."""
    import subprocess
    r = subprocess.run(["git", "archive", "--format=tar", ref], cwd=root, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise UpdateError(f"Couldn't pack {ref}: {r.stderr.decode(errors='replace').strip()[:300]}")
    src = tarfile.open(fileobj=io.BytesIO(r.stdout))
    names = {m.name: m for m in src.getmembers()}
    rules = src.extractfile(names[".releaseignore"]).read().decode() if ".releaseignore" in names else ""
    patterns = ignore_patterns(rules) + [".git/", "releases/", ".den-update/", "RELEASE.json"]
    files: dict[str, str] = {}
    tmp = out.with_suffix(".part")
    with tarfile.open(tmp, "w:gz") as dst:
        for m in src.getmembers():
            if not m.isfile() or ignored(m.name, patterns):
                continue
            data = src.extractfile(m).read()
            files[m.name] = _sha(data)
            ti = tarfile.TarInfo(m.name)
            ti.size, ti.mtime, ti.mode = len(data), m.mtime, (0o755 if m.mode & 0o111 else 0o644)
            dst.addfile(ti, io.BytesIO(data))
        missing = [f for f in REQUIRED if f not in files]
        if missing:
            tmp.unlink(missing_ok=True)
            raise UpdateError("This version would be missing " + ", ".join(missing) + " - save them to git first")
        release = json.dumps({**meta, "version": version, "files": files}, indent=1, sort_keys=True).encode()
        ti = tarfile.TarInfo("RELEASE.json")
        ti.size, ti.mtime, ti.mode = len(release), int(time.time()), 0o644
        dst.addfile(ti, io.BytesIO(release))
    data = tmp.read_bytes()
    tmp.replace(out)
    return {"sha256": _sha(data), "size": len(data), "files": len(files)}


# ---------------------------------------------------------------- publishing

class Publisher:
    def __init__(self, root: Path, releases_dir: Path, key_fn: Callable[[], Ed25519PrivateKey] = signing_key):
        self.root, self.dir, self.key_fn = root, releases_dir, key_fn
        self._lock = threading.Lock()

    def releases(self) -> list[dict[str, Any]]:
        out = []
        for p in self.dir.glob("den-*.json"):
            try:
                m = common.clean_manifest(json.loads(p.read_text()))
            except (OSError, ValueError):
                m = None
            if m and (self.dir / f"den-{m['version']}.tar.gz").exists():
                out.append(m)
        return sorted(out, key=lambda m: common.vtuple(m["version"]), reverse=True)

    def latest(self) -> Optional[dict[str, Any]]:
        rel = self.releases()
        return rel[0] if rel else None

    def bundle_path(self, version: str) -> Optional[Path]:
        if not common.VERSION_RE.match(version or ""):
            return None
        p = self.dir / f"den-{version}.tar.gz"
        return p if p.exists() else None

    def _in_head(self, path: str) -> bool:
        import subprocess
        return subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"], cwd=self.root, capture_output=True).returncode == 0

    def status(self) -> dict[str, Any]:
        version = common.read_version(self.root)
        if common.kind(self.root) != "source":
            return {"can_publish": False, "reason": "Versions are published from the owner's own Den", "version": version}
        dirty = [l[3:] for l in common.git(self.root, "status", "--porcelain", "--untracked-files=all", check=False).splitlines() if len(l) > 3]
        head = common.git(self.root, "rev-parse", "--short", "HEAD", check=False).strip()
        missing = [f for f in REQUIRED if f != "VERSION" and not self._in_head(f)]
        latest = self.latest()
        base = max([version] + ([latest["version"]] if latest else []), key=common.vtuple)
        reason = ""
        if missing:
            reason = ("Some of the Den isn't saved in git yet (" + ", ".join(missing) + "), so a version built now "
                      "wouldn't work. Save your changes to git first.")
        return {"can_publish": not missing, "reason": reason, "version": version, "head": head,
                "next": common.next_versions(base) if latest else {**common.next_versions(base), "first": version},
                "dirty_count": len(dirty), "dirty_files": dirty[:50], "releases": self.releases()}

    def publish(self, version: str, title: str, items: list[str]) -> dict[str, Any]:
        if common.kind(self.root) != "source":
            raise UpdateError("Versions are published from the owner's own Den")
        common.vtuple(version)
        title = " ".join(str(title or "").split())[:120]
        items = [" ".join(str(i).split())[:300] for i in (items or []) if str(i).strip()][:50]
        if not title:
            raise UpdateError("Give the version a short title")
        if not items:
            raise UpdateError("Add at least one line saying what changed")
        with self._lock:
            latest = self.latest()
            if latest and common.vtuple(version) <= common.vtuple(latest["version"]):
                raise UpdateError(f"The last published version is {latest['version']} - the new one must be higher")
            if common.vtuple(version) < common.vtuple(common.read_version(self.root)):
                raise UpdateError(f"This Den is already at {common.read_version(self.root)} - use a higher number")
            tag = f"v{version}"
            if common.git(self.root, "tag", "--list", tag).strip():
                raise UpdateError(f"Version {version} already exists in git - use a higher number")
            missing = [f for f in REQUIRED if f != "VERSION" and not self._in_head(f)]
            if missing:
                raise UpdateError("Save your changes to git first: " + ", ".join(missing) + " isn't saved yet")
            date = datetime.now().strftime("%Y-%m-%d")
            vfile, cfile = self.root / "VERSION", self.root / "CHANGELOG.md"
            old_v = vfile.read_text() if vfile.exists() else None
            old_c = cfile.read_text() if cfile.exists() else None
            entry = f"## {version} — {date} — {title}\n" + "".join(f"- {i}\n" for i in items) + "\n"
            body = old_c or "# What's new in the Den\n\n"
            at = body.find("\n## ")
            new_c = body[:at + 1] + entry + body[at + 1:] if at >= 0 else body.rstrip("\n") + "\n\n" + entry
            try:
                vfile.write_text(version + "\n")
                cfile.write_text(new_c)
                common.git(self.root, "add", "--", "VERSION", "CHANGELOG.md")
                common.git(self.root, "commit", "-m", f"Den {version}: {title}", "--only", "--", "VERSION", "CHANGELOG.md")
            except Exception:
                if old_v is not None:
                    vfile.write_text(old_v)
                if old_c is not None:
                    cfile.write_text(old_c)
                raise
            common.git(self.root, "tag", "-a", tag, "-m", f"Den {version}: {title}")
            commit = common.git(self.root, "rev-parse", "--short", tag + "^{commit}").strip()
            self.dir.mkdir(parents=True, exist_ok=True)
            meta = {"date": date, "title": title, "items": items, "commit": commit}
            try:
                built = build_bundle(self.root, tag, version, self.dir / f"den-{version}.tar.gz", meta)
            except Exception:
                common.git(self.root, "tag", "-d", tag, check=False)
                raise
            manifest = {"version": version, **meta, "sha256": built["sha256"], "size": built["size"]}
            manifest["signature"] = common.sign(self.key_fn(), manifest)
            (self.dir / f"den-{version}.json").write_text(json.dumps(manifest, indent=1))
            log.info("published Den %s (%s files, %s bytes, commit %s)", version, built["files"], built["size"], commit)
            return manifest


# ---------------------------------------------------------------- installs (who may download)

class Installs:
    """The Dens given to other people. Only the sha256 of each key is kept."""

    def __init__(self, get: Callable[[str, Any], Any], put: Callable[[str, Any], None]):
        self.get, self.put = get, put
        self._lock = threading.Lock()
        self._seen_at: dict[str, float] = {}

    def _all(self) -> list[dict[str, Any]]:
        v = self.get("installs", [])
        return v if isinstance(v, list) else []

    def public(self) -> list[dict[str, Any]]:
        return [{k: i.get(k) for k in ("id", "name", "created_at", "last_seen", "version", "ip")} for i in self._all()]

    def add(self, name: str) -> tuple[dict[str, Any], str]:
        name = " ".join(str(name or "").split())[:MAX_NAME]
        if not name:
            raise UpdateError("Give the person's name (or their Mac's)")
        key = "den_" + secrets.token_urlsafe(32)
        row = {"id": secrets.token_hex(6), "name": name, "key_sha256": _sha(key.encode()), "created_at": _now(),
               "last_seen": None, "version": None, "ip": None}
        with self._lock:
            self.put("installs", self._all() + [row])
        return {"id": row["id"], "name": name}, key

    def remove(self, install_id: str) -> bool:
        with self._lock:
            rows = self._all()
            keep = [i for i in rows if i.get("id") != install_id]
            self.put("installs", keep)
        return len(keep) != len(rows)

    def check(self, key: str, version: str, ip: str) -> Optional[str]:
        """The install's id if key is one of ours; notes its version and when it was last seen."""
        if not key:
            return None
        digest = _sha(key.encode())
        with self._lock:
            rows = self._all()
            hit = next((i for i in rows if hmac.compare_digest(str(i.get("key_sha256", "")), digest)), None)
            if not hit:
                return None
            version = version if common.VERSION_RE.match(version or "") else hit.get("version")
            fresh = time.time() - self._seen_at.get(hit["id"], 0) < 60
            if not (fresh and hit.get("version") == version and hit.get("ip") == ip):
                hit.update(last_seen=_now(), version=version, ip=ip)
                self.put("installs", rows)
                self._seen_at[hit["id"]] = time.time()
            return hit["id"]


# ---------------------------------------------------------------- the release server

class _Handler(BaseHTTPRequestHandler):
    server_version = "DenUpdates/1"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("release server: " + fmt, *args)

    def _json(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:   # noqa: N802
        srv: ReleaseServer = self.server.den   # type: ignore[attr-defined]
        ip = self.client_address[0]
        if srv.locked(ip):
            return self._json(429, {"error": "Too many wrong keys - try again in a few minutes"})
        if not srv.installs.check(self.headers.get("X-Den-Install-Key", ""), self.headers.get("X-Den-Version", ""), ip):
            srv.failed(ip)
            return self._json(401, {"error": "This Den's update key isn't recognised - ask the owner for a new one"})
        path = self.path.split("?", 1)[0]
        if path == f"{API}/latest":
            latest = srv.publisher.latest()
            return self._json(200, {"latest": latest}) if latest else self._json(404, {"error": "No version published yet"})
        if path.startswith(f"{API}/bundle/"):
            bundle = srv.publisher.bundle_path(path[len(f"{API}/bundle/"):])
            if not bundle:
                return self._json(404, {"error": "No such version"})
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(bundle.stat().st_size))
            self.end_headers()
            with bundle.open("rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        self._json(404, {"error": "Not found"})


class ReleaseServer:
    FAIL_LIMIT, FAIL_WINDOW = 20, 600

    def __init__(self, host: str, port: int, publisher: Publisher, installs: Installs):
        self.host, self.port, self.publisher, self.installs = host, port, publisher, installs
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._fails: dict[str, list[float]] = {}
        self.error = ""

    def locked(self, ip: str) -> bool:
        now = time.time()
        self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < self.FAIL_WINDOW]
        return len(self._fails[ip]) >= self.FAIL_LIMIT

    def failed(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def start(self) -> None:
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as e:
            self.error = f"Couldn't open port {self.port}: {e.strerror or e}"
            log.warning("release server not started: %s", self.error)
            return
        self.httpd.daemon_threads = True
        self.httpd.den = self   # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="den-release-server", daemon=True).start()
        log.info("release server on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    @property
    def running(self) -> bool:
        return self.httpd is not None
