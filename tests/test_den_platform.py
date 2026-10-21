"""den_platform (Mac / Windows switches, the key file fallback, the approval helper config),
den_service (settings file, restart delay) and installer/build.py (what stays out of the zips).
None of these touch the database or the live service."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import den_platform  # noqa: E402
import den_service  # noqa: E402

_spec = importlib.util.spec_from_file_location("den_build", ROOT / "installer/build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


class PlatformTests(unittest.TestCase):
    def test_spawn_kwargs_per_system(self):
        with mock.patch.object(den_platform, "IS_WINDOWS", False):
            self.assertEqual(den_platform.spawn_kwargs(), {"start_new_session": True})
        with mock.patch.object(den_platform, "IS_WINDOWS", True):
            flags = den_platform.spawn_kwargs()["creationflags"]
            self.assertTrue(flags & 0x200 and flags & 0x08000000)

    def test_windows_cannot_pause(self):
        with mock.patch.object(den_platform, "IS_WINDOWS", True):
            self.assertFalse(den_platform.can_pause())
            self.assertFalse(den_platform.freeze_tree(1))
            self.assertFalse(den_platform.thaw_tree(1))

    def test_kill_tree_on_windows_uses_taskkill(self):
        with mock.patch.object(den_platform, "IS_WINDOWS", True), mock.patch.object(den_platform.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(den_platform.kill_tree(4321))
            self.assertEqual(run.call_args[0][0], ["taskkill", "/F", "/T", "/PID", "4321"])

    def test_claude_bin_mac_is_unchanged(self):
        with mock.patch.object(den_platform, "IS_WINDOWS", False), mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(den_platform.claude_bin(), "claude")
        with mock.patch.dict("os.environ", {"CLAUDE_BIN": "/x/claude"}):
            self.assertEqual(den_platform.claude_bin(), "/x/claude")

    def test_claude_bin_on_windows_looks_it_up(self):
        with mock.patch.object(den_platform, "IS_WINDOWS", True), mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(den_platform.shutil, "which", return_value=r"C:\x\claude.exe"):
            self.assertEqual(den_platform.claude_bin(), r"C:\x\claude.exe")

    def test_log_dir(self):
        with mock.patch.dict("os.environ", {"DEN_LOG_DIR": "/tmp/somewhere"}):
            self.assertEqual(den_platform.log_dir(), Path("/tmp/somewhere"))
        with mock.patch.object(den_platform, "IS_WINDOWS", True), mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\L"}, clear=True):
            self.assertEqual(den_platform.log_dir(), Path(r"C:\L") / "TheDen" / "logs")
        with mock.patch.object(den_platform, "IS_WINDOWS", False), mock.patch.object(den_platform, "IS_MAC", True), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(den_platform.log_dir(), Path.home() / "Library" / "Logs" / "TheDen")

    def test_key_file_fallback_round_trip(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(den_platform, "_keyring", return_value=None):
            root = Path(d)
            self.assertIsNone(den_platform.secret_get("svc", "acct", root))
            den_platform.secret_set("svc", "acct", {"k": "v" * 3000}, root)
            self.assertEqual(den_platform.secret_get("svc", "acct", root), {"k": "v" * 3000})
            self.assertEqual(oct((root / den_platform.SECRET_FILE).stat().st_mode & 0o777), oct(0o600))
            den_platform.secret_delete("svc", "acct", root)
            self.assertIsNone(den_platform.secret_get("svc", "acct", root))

    def test_keyring_store_splits_long_values(self):
        class FakeKeyring:
            def __init__(self):
                self.items = {}

            def get_password(self, s, a):
                return self.items.get((s, a))

            def set_password(self, s, a, v):
                assert len(v) <= 1000
                self.items[(s, a)] = v

            def delete_password(self, s, a):
                self.items.pop((s, a), None)
        kr = FakeKeyring()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(den_platform, "_keyring", return_value=kr):
            data = {"token": "t" * 4000}
            den_platform.secret_set("svc", "acct", data, Path(d))
            self.assertGreater(len(kr.items), 2)
            self.assertEqual(den_platform.secret_get("svc", "acct", Path(d)), data)
            self.assertFalse((Path(d) / den_platform.SECRET_FILE).exists())
            den_platform.secret_delete("svc", "acct", Path(d))
            self.assertEqual(kr.items, {})

    def test_mcp_config_kept_when_its_python_exists(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "mcp-config.json").write_text(json.dumps({"mcpServers": {"approval": {"command": sys.executable, "args": ["x"]}}}))
            self.assertEqual(den_platform.mcp_config_path(root), root / "mcp-config.json")

    def test_mcp_config_gets_a_local_copy_when_its_python_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "mcp-config.json").write_text(json.dumps({"mcpServers": {"approval": {"command": "/nope/python3", "args": ["/nope/a.py"]}}}))
            path = den_platform.mcp_config_path(root)
            self.assertEqual(path, root / "mcp-config.local.json")
            cfg = json.loads(path.read_text())["mcpServers"]["approval"]
            self.assertEqual(cfg["args"], [str(root / "approval_server.py")])
            self.assertTrue(Path(cfg["command"]).exists())


class ServiceTests(unittest.TestCase):
    def test_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "den.env"
            p.write_text("\ufeff# note\nAPI_KEY=abc123\nHOST = 127.0.0.1\nQ=\"quoted value\"\n\nbroken line\n", encoding="utf-8")
            self.assertEqual(den_service.read_env_file(p), {"API_KEY": "abc123", "HOST": "127.0.0.1", "Q": "quoted value"})
            self.assertEqual(den_service.read_env_file(Path(d) / "missing"), {})

    def test_restart_delay_grows_only_while_it_keeps_dying(self):
        self.assertEqual(den_service.restart_delay(3600, 40), 2.0)
        self.assertEqual(den_service.restart_delay(1, 0), 5.0)
        self.assertEqual(den_service.restart_delay(1, 5), 10.0)
        self.assertEqual(den_service.restart_delay(1, 50), 60.0)

    def test_stop_file_prevents_any_start(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"DEN_LOG_DIR": d}):
            (Path(d) / den_service.STOP_FLAG).write_text("stop")
            with mock.patch.object(den_service.subprocess, "Popen") as popen:
                self.assertEqual(den_service.run(Path(d)), 0)
                popen.assert_not_called()


class BuildTests(unittest.TestCase):
    def test_private_things_stay_out(self):
        for path in ("queue.db", "queue.db-wal", "queue.db.bak-pre-x", "accounts.json", "login.json", "api_keys.json",
                     "workspace/a/b.txt", "backups/x.db", "vault/f", ".venv/bin/python", ".git/config", ".scratch-x/a.py",
                     "modules/m/__pycache__/x.pyc", "tests/test_a.py", "design_handoff_foo/a.html", "data/TheDen-UI 2.1/a.html",
                     "uploads/a.png", "releases/den-1.tar.gz", ".den-update/pending.json", "den.env", "den-keys.json",
                     "setup/com.tushar.claudequeue.plist", "static/project-images/a.png", "CLAUDE.md"):
            self.assertIsNotNone(build.excluded_by(path), path)

    def test_code_goes_in(self):
        for path in ("main.py", "documents.py", "uploads.py", "worker.py", "static/den/den.js", "modules/den_updates/module.py",
                     "docs/INSTALL.md", "requirements.txt", "VERSION", "den_platform.py", "den_service.py", "mcp-config.json"):
            self.assertIsNone(build.excluded_by(path), path)

    def test_extra_rule(self):
        rules = build.EXCLUDE + [("modules/den_collab/", "x")]
        self.assertEqual(build.excluded_by("modules/den_collab/module.py", rules), "x")

    def test_secret_scan_finds_keys_and_never_repeats_them(self):
        files = {"a.py": b'KEY = "sk-ant-api03-abcdefghijklmnop"', "b.py": b"API_KEY=" + b"a1" * 16,
                 "c.py": b"print('hello')", "d.txt": b"has real-owner-key-123456 inside"}
        found = build.scan_for_secrets(files, ["real-owner-key-123456"])
        self.assertEqual(sorted(p.split(":")[0] for p in found), ["a.py", "b.py", "d.txt"])
        self.assertFalse(any("sk-ant" in p or "real-owner" in p for p in found))

    def test_ordinary_code_is_not_flagged(self):
        self.assertEqual(build.scan_for_secrets({"x.py": b'API_KEY = os.environ.get("API_KEY")\nHOST = "127.0.0.1"'}), [])


if __name__ == "__main__":
    unittest.main()
