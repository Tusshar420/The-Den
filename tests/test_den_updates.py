"""
Den versions and updates (modules/den_updates, launcher.py): publishing from a git checkout,
the release server and its keys, a given-away Den downloading and checking an update, and
launcher.py putting it in place - or going back when the new version doesn't start.

Everything runs in temporary folders, on 127.0.0.1, with a throwaway signing key.
"""

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401  (puts the Den on sys.path)

import launcher
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from modules.den_updates import common, releases, updater
from modules.den_updates.common import UpdateError

ROOT = Path(__file__).resolve().parent.parent


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Memory:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return json.loads(json.dumps(self.d.get(k, default)))

    def put(self, k, v):
        self.d[k] = json.loads(json.dumps(v))


class CommonTest(unittest.TestCase):
    def test_changelog_and_versions(self):
        text = "# What's new\n\n## 2.2.0 — 2026-11-01 — Faster\n- One\n- Two\n\n## 2.1.0 — 2026-10-21 — First\n- Start\n"
        entries = common.parse_changelog(text)
        self.assertEqual([e["version"] for e in entries], ["2.2.0", "2.1.0"])
        self.assertEqual(entries[0], {"version": "2.2.0", "date": "2026-11-01", "title": "Faster", "items": ["One", "Two"]})
        self.assertEqual(common.next_versions("2.1.9"), {"patch": "2.1.10", "minor": "2.2.0", "major": "3.0.0"})
        self.assertGreater(common.vtuple("2.10.0"), common.vtuple("2.9.9"))
        with self.assertRaises(UpdateError):
            common.vtuple("2.1")

    def test_signature(self):
        key = Ed25519PrivateKey.generate()
        m = {"version": "1.2.3", "date": "2026-10-21", "title": "t", "items": ["a"], "commit": "abc", "sha256": "00", "size": 5}
        m["signature"] = common.sign(key, m)
        pub = releases.public_key_b64(key)
        common.verify(pub, m)
        for field, value in (("size", 6), ("sha256", "11"), ("items", ["b"]), ("version", "1.2.4")):
            with self.assertRaises(UpdateError):
                common.verify(pub, {**m, field: value})
        with self.assertRaises(UpdateError):
            common.verify(releases.public_key_b64(Ed25519PrivateKey.generate()), m)

    def test_ignore_patterns(self):
        pats = releases.ignore_patterns((ROOT / ".releaseignore").read_text())
        for path in ("tests/test_x.py", "data/TheDen-UI 2.0/x.html", "setup/com.x.plist", ".scratch-crm4/serve.py",
                     "design_handoff_tushars_workspace/README.md", "queue.db.bak", "docs/DEN_UI_HANDOVER.pdf"):
            self.assertTrue(releases.ignored(path, pats), path)
        for path in ("main.py", "static/den/DenHome.dc.html", "docs/MODULES.md", "setup/alexa/skill.json", "modules/den_updates/module.py"):
            self.assertFalse(releases.ignored(path, pats), path)

    def test_safe_paths(self):
        for bad in ("../x", "/etc/passwd", "a/../../b", "queue.db", "queue.db-wal", "accounts.json", "sub/login.json",
                    ".venv/bin/python", "workspace/uploads/a", "vault/k", ".git/config", "static/project-images/1.png", ""):
            self.assertFalse(launcher.safe_path(bad), bad)
        for ok in ("main.py", "static/den/den.js", "modules/den_updates/module.py"):
            self.assertTrue(launcher.safe_path(ok), ok)


class ReleaseFlowTest(unittest.TestCase):
    """Owner publishes; a given-away Den checks, downloads and stages; launcher applies."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "source"
        self.src.mkdir()
        files = {
            "main.py": "print('v1')\n", "launcher.py": "# launcher\n", "modules/den_updates/module.py": "# updates\n",
            "old_only.py": "gone in v2\n", "tests/test_secret.py": "no\n", "data/design.html": "no\n",
            "VERSION": "1.0.0\n", "CHANGELOG.md": "# What's new in the Den\n\n## 1.0.0 — 2026-10-01 — First\n- Hello\n",
            ".releaseignore": (ROOT / ".releaseignore").read_text(), "requirements.txt": "fastapi\n",
        }
        for name, text in files.items():
            (self.src / name).parent.mkdir(parents=True, exist_ok=True)
            (self.src / name).write_text(text)
        sh(self.src, "git", "init", "-q", "-b", "main")
        sh(self.src, "git", "config", "user.email", "t@example.com")
        sh(self.src, "git", "config", "user.name", "Test")
        sh(self.src, "git", "add", "-A")
        sh(self.src, "git", "commit", "-q", "-m", "start")
        self.key = Ed25519PrivateKey.generate()
        self.pub = releases.public_key_b64(self.key)
        self.pubr = releases.Publisher(self.src, self.src / "releases", key_fn=lambda: self.key)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bundle_names(self, version):
        with tarfile.open(self.src / "releases" / f"den-{version}.tar.gz") as t:
            return {m.name for m in t}

    def test_publish(self):
        (self.src / "unsaved.py").write_text("work in progress\n")
        st = self.pubr.status()
        self.assertTrue(st["can_publish"])
        self.assertEqual(st["dirty_count"], 1)
        m = self.pubr.publish("1.0.0", "First release", ["Hello"])
        common.verify(self.pub, m)
        names = self.bundle_names("1.0.0")
        self.assertIn("main.py", names)
        self.assertIn("RELEASE.json", names)
        self.assertNotIn("unsaved.py", names)
        self.assertNotIn("tests/test_secret.py", names)
        self.assertNotIn("data/design.html", names)
        self.assertEqual(subprocess.run(["git", "tag"], cwd=self.src, capture_output=True, text=True).stdout.split(), ["v1.0.0"])
        self.assertEqual((self.src / "unsaved.py").read_text(), "work in progress\n")   # left alone, not committed
        self.assertIn("unsaved.py", subprocess.run(["git", "status", "--porcelain"], cwd=self.src, capture_output=True, text=True).stdout)
        for version in ("1.0.0", "0.9.0"):
            with self.assertRaises(UpdateError):
                self.pubr.publish(version, "Again", ["x"])
        m2 = self.pubr.publish("1.1.0", "Second", ["Two"])
        self.assertEqual(common.read_version(self.src), "1.1.0")
        self.assertEqual(common.read_changelog(self.src)[0]["version"], "1.1.0")
        self.assertEqual([r["version"] for r in self.pubr.releases()], ["1.1.0", "1.0.0"])
        self.assertEqual(self.pubr.latest()["sha256"], m2["sha256"])

    def test_refuses_without_the_updater_saved(self):
        sh(self.src, "git", "rm", "-q", "--cached", "launcher.py")
        sh(self.src, "git", "commit", "-q", "-m", "oops")
        st = self.pubr.status()
        self.assertFalse(st["can_publish"])
        self.assertIn("launcher.py", st["reason"])
        with self.assertRaises(UpdateError):
            self.pubr.publish("1.0.1", "x", ["y"])
        self.assertEqual(common.read_version(self.src), "1.0.0")

    def make_install(self, version):
        """A given-away Den unpacked from a published bundle, with some personal data."""
        inst = self.tmp / "install"
        shutil.rmtree(inst, ignore_errors=True)
        inst.mkdir()
        updater.unpack(self.src / "releases" / f"den-{version}.tar.gz", inst)
        (inst / "queue.db").write_bytes(b"")
        import sqlite3
        c = sqlite3.connect(str(inst / "queue.db"))
        c.execute("create table t (v text)")
        c.execute("insert into t values ('mine')")
        c.commit()
        c.close()
        (inst / "accounts.json").write_text('{"token": "theirs"}')
        (inst / "workspace").mkdir()
        (inst / "workspace" / "note.txt").write_text("their note")
        return inst

    def test_end_to_end_update_and_rollback(self):
        self.pubr.publish("1.0.0", "First", ["Hello"])
        inst = self.make_install("1.0.0")
        self.assertEqual(common.kind(inst), "install")
        # The owner changes things and publishes 1.1.0 - the install doesn't get it by itself.
        (self.src / "main.py").write_text("print('v2')\n")
        (self.src / "new_file.py").write_text("new\n")
        sh(self.src, "git", "rm", "-q", "old_only.py")
        sh(self.src, "git", "add", "-A")
        sh(self.src, "git", "commit", "-q", "-m", "v2 work")
        self.pubr.publish("1.1.0", "Second", ["New things"])
        self.assertEqual((inst / "main.py").read_text(), "print('v1')\n")

        mem_owner = Memory()
        installs = releases.Installs(mem_owner.get, mem_owner.put)
        server = releases.ReleaseServer("127.0.0.1", 0, self.pubr, installs)
        server.start()
        self.addCleanup(server.stop)
        info, key = installs.add("Asha's Mac")
        url = f"http://127.0.0.1:{server.port}"

        restarts = []
        mem = Memory()
        up = updater.Updater(inst, mem.get, mem.put, lambda: restarts.append(1), under_launcher=lambda: True)
        self.assertFalse(up.status()["configured"])
        with self.assertRaises(UpdateError):
            up.set_source({"url": url, "key": key, "public_key": "not-a-key"})

        # A wrong key is refused by the owner's server.
        up.set_source({"url": url, "key": "den_wrong", "public_key": self.pub})
        with self.assertRaises(UpdateError) as e:
            up.check()
        self.assertIn("isn't recognised", str(e.exception))

        # The wrong owner's key: the signature doesn't match, so nothing is trusted.
        up.set_source({"url": url, "key": key, "public_key": releases.public_key_b64(Ed25519PrivateKey.generate())})
        with self.assertRaises(UpdateError) as e:
            up.check()
        self.assertIn("isn't signed", str(e.exception))

        up.set_source({"url": url, "key": key, "public_key": self.pub})
        st = up.check()
        self.assertTrue(st["update_available"])
        self.assertEqual(st["latest"]["version"], "1.1.0")
        seen = installs.public()[0]
        self.assertEqual((seen["name"], seen["version"]), ("Asha's Mac", "1.0.0"))
        self.assertNotIn("key_sha256", seen)

        up.install()
        self.assertEqual(restarts, [1])
        self.assertTrue((inst / ".den-update" / "pending.json").exists())
        self.assertEqual((inst / "main.py").read_text(), "print('v1')\n")   # nothing changes until the restart

        paths = launcher.Paths(inst)
        launcher.apply_staged(paths)
        self.assertEqual((inst / "main.py").read_text(), "print('v2')\n")
        self.assertTrue((inst / "new_file.py").exists())
        self.assertFalse((inst / "old_only.py").exists())
        self.assertEqual(common.read_version(inst), "1.1.0")
        self.assertEqual((inst / "accounts.json").read_text(), '{"token": "theirs"}')
        self.assertEqual((inst / "workspace" / "note.txt").read_text(), "their note")
        self.assertTrue(paths.verify.exists())

        launcher.rollback(paths, "test")
        self.assertEqual((inst / "main.py").read_text(), "print('v1')\n")
        self.assertTrue((inst / "old_only.py").exists())
        self.assertFalse((inst / "new_file.py").exists())
        self.assertEqual(common.read_version(inst), "1.0.0")
        self.assertEqual((inst / "accounts.json").read_text(), '{"token": "theirs"}')
        import sqlite3
        c = sqlite3.connect(str(inst / "queue.db"))
        self.assertEqual(c.execute("select v from t").fetchall(), [("mine",)])
        c.close()
        hist = json.loads(paths.history.read_text())
        self.assertFalse(hist[-1]["ok"])

    def test_tampered_bundle_is_refused(self):
        self.pubr.publish("1.0.0", "First", ["Hello"])
        inst = self.make_install("1.0.0")
        sh(self.src, "git", "commit", "-q", "--allow-empty", "-m", "more")
        self.pubr.publish("1.0.1", "Fix", ["Fix"])
        bundle = self.src / "releases" / "den-1.0.1.tar.gz"
        data = bytearray(bundle.read_bytes())
        data[-10] ^= 0xFF
        bundle.write_bytes(bytes(data))
        mem_owner = Memory()
        installs = releases.Installs(mem_owner.get, mem_owner.put)
        server = releases.ReleaseServer("127.0.0.1", 0, self.pubr, installs)
        server.start()
        self.addCleanup(server.stop)
        _, key = installs.add("x")
        mem = Memory()
        up = updater.Updater(inst, mem.get, mem.put, lambda: self.fail("must not restart"), under_launcher=lambda: True)
        up.set_source({"url": f"http://127.0.0.1:{server.port}", "key": key, "public_key": self.pub})
        with self.assertRaises(UpdateError) as e:
            up.install()
        self.assertIn("doesn't match", str(e.exception))
        self.assertFalse((inst / ".den-update" / "pending.json").exists())
        self.assertFalse((inst / ".den-update" / "staged").exists())

    def test_install_rules(self):
        mem = Memory()
        src_up = updater.Updater(self.src, mem.get, mem.put, lambda: None, under_launcher=lambda: True)
        with self.assertRaises(UpdateError):
            src_up.install()       # the owner's copy never updates itself
        self.pubr.publish("1.0.0", "First", ["Hello"])
        inst = self.make_install("1.0.0")
        no_launcher = updater.Updater(inst, mem.get, mem.put, lambda: None, under_launcher=lambda: False)
        with self.assertRaises(UpdateError):
            no_launcher.install()


class UnpackTest(unittest.TestCase):
    def bundle(self, members):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "b.tar.gz"
        with tarfile.open(p, "w:gz") as t:
            for name, kind in members:
                ti = tarfile.TarInfo(name)
                if kind == "link":
                    ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    t.addfile(ti)
                else:
                    ti.size = 2
                    t.addfile(ti, io.BytesIO(b"hi"))
        return p, tmp / "out"

    def test_refuses_unsafe_members(self):
        for bad in ([("RELEASE.json", "f"), ("../evil.py", "f")], [("RELEASE.json", "f"), ("/abs.py", "f")],
                    [("RELEASE.json", "f"), ("link.py", "link")], [("RELEASE.json", "f"), ("queue.db", "f")],
                    [("RELEASE.json", "f"), ("accounts.json", "f")], [("main.py", "f")]):
            p, out = self.bundle(bad)
            with self.assertRaises(UpdateError):
                updater.unpack(p, out)
        p, out = self.bundle([("RELEASE.json", "f"), ("static/x.js", "f")])
        self.assertEqual(updater.unpack(p, out), {"RELEASE.json", "static/x.js"})


class LauncherRunTest(unittest.TestCase):
    """launcher.run: the new version has to answer /health, or everything goes back."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.port = free_port()
        os.environ["PORT"] = str(self.port)
        self.addCleanup(os.environ.pop, "PORT", None)

    def stage(self, new_main):
        old = "import sys; sys.exit(0)\n"
        (self.root / "main.py").write_text(old)
        (self.root / "RELEASE.json").write_text(json.dumps(
            {"version": "1.0.0", "files": {"main.py": hashlib.sha256(old.encode()).hexdigest()}}))
        st = self.root / ".den-update" / "staged"
        st.mkdir(parents=True)
        (st / "main.py").write_text(new_main)
        (st / "RELEASE.json").write_text(json.dumps(
            {"version": "1.1.0", "files": {"main.py": hashlib.sha256(new_main.encode()).hexdigest()}}))
        (self.root / ".den-update" / "pending.json").write_text(json.dumps({"version": "1.1.0", "from": "1.0.0"}))
        return old

    def test_good_update_stays(self):
        new_main = textwrap.dedent(f"""
            import http.server, os
            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
                def log_message(self, *a): pass
            s = http.server.HTTPServer(("127.0.0.1", {self.port}), H)
            assert os.environ.get("DEN_LAUNCHER") == "1"
            s.handle_request()
        """)
        self.stage(new_main)
        self.assertEqual(launcher.run(self.root, health_seconds=20), 0)
        self.assertEqual((self.root / "main.py").read_text(), new_main)
        hist = json.loads((self.root / ".den-update" / "history.json").read_text())
        self.assertTrue(hist[-1]["ok"])
        self.assertFalse((self.root / ".den-update" / "verify.json").exists())

    def test_broken_update_goes_back(self):
        old = self.stage("import sys; sys.exit(3)\n")
        self.assertEqual(launcher.run(self.root, health_seconds=20), 1)
        self.assertEqual((self.root / "main.py").read_text(), old)
        self.assertIn('"1.0.0"', (self.root / "RELEASE.json").read_text())
        hist = json.loads((self.root / ".den-update" / "history.json").read_text())
        self.assertFalse(hist[-1]["ok"])
        self.assertIn("1.1.0", hist[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
