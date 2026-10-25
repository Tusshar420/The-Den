"""The Den 2.0 system screens: AI account order / auto-switch, sites, tools, push devices, backups and export."""

import io
import json
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.helpers import TZ, TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import threading
import time
import urllib.error
import urllib.request

import uvicorn
from fastapi import FastAPI, HTTPException, Header

import accounts
import db
import den_system
from octopus import sites


def acct(aid, five=0.0, week=0.0, active=False, **extra):
    return {"id": aid, "active": active, "usage": {"windows": {"five_hour": {"utilization": five}, "seven_day": {"utilization": week}}},
            "limited_until": None, "auth_error": None, **extra}


class Base(TestBase):
    def setUp(self):
        super().setUp()
        den_system._tool_cache.update(at=0.0, rows=None)
        den_system._gpu_cache.clear()

    def client(self):
        def key(x_api_key: str = Header("")):
            if x_api_key != "k":
                raise HTTPException(status_code=401, detail="API key needed")
        app = FastAPI()
        app.include_router(den_system.build_router(key))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        self.addCleanup(lambda: setattr(server, "should_exit", True))
        return Client(server.servers[0].sockets[0].getsockname()[1])


class Reply:
    def __init__(self, status, content, headers):
        self.status_code, self.content, self.headers = status, content, {k.lower(): v for k, v in headers.items()}
        self.text = content.decode(errors="replace")

    def json(self):
        return json.loads(self.content or b"null")


class Client:
    """Just enough of a test client over urllib (the venv has no httpx)."""

    def __init__(self, port):
        self.port = port

    def _call(self, method, path, json_body=None, headers=None):
        h = {"X-API-Key": "k", "Content-Type": "application/json", **(headers or {})}
        data = json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return Reply(r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            return Reply(e.code, e.read(), dict(e.headers))

    def get(self, path, headers=None):
        return self._call("GET", path, headers=headers)

    def post(self, path, json=None):
        return self._call("POST", path, json if json is not None else {})

    def put(self, path, json=None):
        return self._call("PUT", path, json)


class Accounts(Base):
    def test_policy_defaults_and_validation(self):
        self.assertEqual(den_system.policy("claude"), {"auto": False, "at": 90, "order": []})
        den_system.set_policy("claude", auto=True, at=80, order=["b", "a"])
        self.assertEqual(den_system.policy("claude"), {"auto": True, "at": 80, "order": ["b", "a"]})
        with self.assertRaises(HTTPException):
            den_system.set_policy("claude", at=85)
        self.assertEqual(den_system.ordered("claude", ["a", "c", "b"]), ["b", "a", "c"])

    def test_api_key_switch_is_the_failover_setting(self):
        den_system.set_policy("openai", auto=False)
        self.assertFalse(db.get_setting("failover")["retry"])
        self.assertFalse(den_system.policy("openai")["auto"])
        with mock.patch.object(den_system.api_keys, "make_default") as made:
            den_system.set_policy("gemini", order=["k2", "k1"])
        made.assert_called_once_with("k2")

    def test_auto_switch_moves_the_default_to_the_next_account_under_the_line(self):
        den_system.set_policy("claude", auto=True, at=90, order=["mac", "a", "b"])
        listed = [acct("mac", five=0.95, active=True), acct("a", week=0.92), acct("b", five=0.3)]
        with mock.patch.object(accounts, "public_list", return_value=listed), \
                mock.patch.object(accounts, "activate") as activate, mock.patch("worker.wake_agent_lane"):
            self.assertEqual(den_system.auto_switch_claude(), "b")
        activate.assert_called_once_with("b")
        self.assertEqual(den_system._state()["last_switch"]["to"], "b")

    def test_auto_switch_does_nothing_when_off_or_under_the_line(self):
        listed = [acct("mac", five=0.95, active=True), acct("a")]
        with mock.patch.object(accounts, "public_list", return_value=listed), mock.patch.object(accounts, "activate") as activate:
            self.assertIsNone(den_system.auto_switch_claude())                  # off by default
            den_system.set_policy("claude", auto=True, at=95)
            self.assertEqual(den_system.auto_switch_claude(), "a")              # 95% counts as reached
        activate.assert_called_once_with("a")
        listed = [acct("mac", five=0.5, active=True), acct("a")]                # under the line
        with mock.patch.object(accounts, "public_list", return_value=listed), mock.patch.object(accounts, "activate") as activate:
            self.assertIsNone(den_system.auto_switch_claude())
        activate.assert_not_called()
        listed = [acct("mac", five=1.03, active=True), acct("a")]               # past the limit is 103%, not 1%
        with mock.patch.object(accounts, "public_list", return_value=listed), mock.patch.object(accounts, "activate") as activate:
            self.assertEqual(den_system.auto_switch_claude(), "a")

    def test_auto_switch_skips_logged_out_and_limited_accounts(self):
        den_system.set_policy("claude", auto=True, at=80)
        listed = [acct("mac", five=0.9, active=True), acct("a", auth_error="expired"), acct("b", limited_until="2026-10-12T00:00:00Z")]
        with mock.patch.object(accounts, "public_list", return_value=listed), mock.patch.object(accounts, "activate") as activate:
            self.assertIsNone(den_system.auto_switch_claude())
        activate.assert_not_called()

    def test_looks_and_spend(self):
        den_system.set_look("openai", "k1", name=" Den API ", color="#6FCF97")
        self.assertEqual(den_system._state()["looks"]["openai:k1"], {"name": "Den API", "color": "#6fcf97"})
        with self.assertRaises(HTTPException):
            den_system.set_look("openai", "k1", color="green")
        with self.assertRaises(HTTPException):
            den_system.set_look("claude", "mac", name="x")
        t = db.add_task(self.project["id"], "hi", lane="api", model_id="gpt-6-sol")
        with db._lock:
            db.get_conn().execute("UPDATE tasks SET cost_usd = 1.25, finished_at = ? WHERE id = ?", (db._now(), t["id"]))
            db.get_conn().commit()
        spend = den_system.month_spend()
        self.assertAlmostEqual(spend["providers"]["openai"], 1.25)
        self.assertAlmostEqual(spend["models"]["gpt-6-sol"], 1.25)

    def test_ollama_gpu_only_for_ollama_servers(self):
        self.assertIsNone(den_system.ollama_gpu("https://openrouter.ai/api/v1"))
        body = json.dumps({"models": [{"name": "llama3.1:8b", "size": 8e9, "size_vram": 6e9}]}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body
        with mock.patch.object(den_system.urllib.request, "urlopen", return_value=resp) as op:
            g = den_system.ollama_gpu("http://192.168.1.20:11434/v1")
        self.assertEqual(op.call_args[0][0], "http://192.168.1.20:11434/api/ps")
        self.assertEqual((g["loaded"], g["pct"], g["vram_gb"]), ("llama3.1:8b", 75, 6.0))
        with mock.patch.object(den_system.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIsNone(den_system.ollama_gpu("http://localhost:11434/v1"))

    def test_routes_need_the_key(self):
        c = self.client()
        self.assertEqual(c.get("/den/system/accounts", headers={"X-API-Key": "wrong"}).status_code, 401)
        r = c.get("/den/system/accounts")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()["policy"]), set(den_system.PROVIDERS))
        self.assertEqual(c.put("/den/system/accounts/claude/policy", json={"auto": True, "at": 95}).json()["at"], 95)


class Sites(Base):
    def setUp(self):
        super().setUp()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        p = mock.patch.object(sites, "SKILLS_DIR", root / "skills")
        p.start()
        self.addCleanup(p.stop)
        d = root / "skills" / "site-demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: site-demo\ndescription: Demo\n---\n- URL: https://demo.example\n## One\n## Two\n")
        self.root = root

    def test_off_moves_out_of_skills_and_back(self):
        self.assertTrue(den_system.site_list()["sites"][0]["on"])
        den_system.set_site("site-demo", False)
        self.assertFalse((self.root / "skills" / "site-demo").exists())
        self.assertEqual([(s["id"], s["on"], s["url"]) for s in den_system.site_list()["sites"]], [("site-demo", False, "https://demo.example")])
        den_system.set_site("site-demo", True)
        self.assertTrue((self.root / "skills" / "site-demo" / "SKILL.md").exists())

    def test_remove_keeps_a_copy_and_ids_are_checked(self):
        out = den_system.remove_site("site-demo")
        self.assertTrue(Path(out["kept_in"], "SKILL.md").exists())
        self.assertEqual(den_system.site_list()["sites"], [])
        for bad in ("../x", "site-../../etc", "notasite"):
            with self.assertRaises(HTTPException):
                den_system.remove_site(bad)

    def test_learning_tasks_are_listed(self):
        db.add_task(self.project["id"], sites.learn_prompt("https://docs.example.com"), routed_by="sites")
        self.assertEqual(den_system.site_list()["learning"][0]["url"], "https://docs.example.com")


class Tools(Base):
    def test_list_toggle_and_install_queues_a_task(self):
        with mock.patch.object(den_system, "_detect", return_value={"path": "/usr/bin/x", "version": "1.2", "status": "ok"}):
            rows = den_system.tool_list()
        self.assertEqual(len(rows), len(den_system.TOOLS))
        self.assertTrue(all(r["on"] for r in rows))
        den_system.set_tool("docker", False)
        self.assertFalse(next(r for r in den_system.tool_list() if r["id"] == "docker")["on"])
        out = den_system.install_tool("gh")
        self.assertIn("brew install gh", out["task"]["prompt"])
        self.assertEqual(out["task"]["status"], "queued")
        with self.assertRaises(HTTPException):
            den_system.install_tool("python")            # nothing to install


class Delivery(Base):
    def test_devices_hide_endpoints_and_can_be_removed(self):
        db.add_push_subscription("https://push.example/abc", "p", "a", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1")
        devs = den_system.push_devices()
        self.assertEqual(devs[0]["name"], "iPhone · Safari")
        self.assertNotIn("endpoint", devs[0])
        den_system.remove_push_device(devs[0]["id"])
        self.assertEqual(db.list_push_subscriptions(), [])

    def test_alert_email(self):
        self.assertEqual(den_system.set_alert_email(" me@example.com ")["to"], "me@example.com")
        with self.assertRaises(HTTPException):
            den_system.set_alert_email("not an email")


class Backups(Base):
    def test_backup_list_download_and_restore_needs_confirm(self):
        c = self.client()
        made = c.post("/den/system/backups").json()
        self.assertTrue(made["name"].startswith("den-") and made["size"] > 0)
        self.assertTrue((db.DB_PATH.parent / "backups" / ".gitignore").exists())
        self.assertEqual(c.get("/den/system/backups/" + made["name"]).content[:16], b"SQLite format 3\x00")
        self.assertEqual(c.get("/den/system/backups/queue.db").status_code, 400)
        db.add_project("Later", self.project["path"])
        self.assertEqual(c.post(f"/den/system/backups/{made['name']}/restore", json={}).status_code, 400)
        r = c.post(f"/den/system/backups/{made['name']}/restore", json={"confirm": "replace-everything"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(db.get_project_by_name("Later"))
        self.assertEqual(len(den_system.backup_list()), 2)          # the safety copy is listed too

    def test_schedule_and_pruning(self):
        self.assertFalse(den_system.backup_due())                   # off until chosen
        den_system.set_backup_settings(schedule="daily", drive=True)
        self.assertTrue(den_system.backup_settings()["drive"])
        self.assertFalse(den_system.backup_settings()["drive_available"])
        night = datetime(2026, 10, 11, 1, 30, tzinfo=TZ)
        self.assertFalse(den_system.backup_due(night))
        self.assertTrue(den_system.backup_due(night + timedelta(hours=1)))
        with mock.patch.object(den_system.timeutil, "now", return_value=night + timedelta(hours=1)):
            den_system.tick()
            self.assertEqual([b["auto"] for b in den_system.backup_list()], [True])
            self.assertFalse(den_system.backup_due())
        for _ in range(den_system.KEEP_AUTO + 2):
            den_system.make_backup(auto=True)
        self.assertEqual(len(den_system.backup_list()), den_system.KEEP_AUTO)
        den_system.set_backup_settings(schedule="weekly")
        self.assertFalse(den_system.backup_due())
        with self.assertRaises(HTTPException):
            den_system.set_backup_settings(schedule="hourly")

    def test_drive_copy_uploads_when_on_and_records_failure(self):
        den_system.set_backup_settings(drive=True)
        with mock.patch.object(den_system.connections, "connected_with", return_value=["google"]), \
                mock.patch.object(den_system.google_drive, "drive_folder", return_value="F1") as folder, \
                mock.patch.object(den_system.google_drive, "drive_upload", return_value="X") as up:
            made = den_system.make_backup()
            self.assertEqual(up.call_args.args[0], made["name"])
            self.assertEqual(up.call_args.args[2], "F1")
            s = den_system.backup_settings()
            self.assertTrue(s["drive_available"] and s["last_drive"] and not s["drive_error"])
            self.assertEqual(den_system.backup_list()[0]["where"], "Local + Drive")
            up.side_effect = den_system.ProviderError("Google is rate-limiting requests right now.")
            den_system.make_backup()                                  # the local backup still succeeds
            self.assertIn("rate-limiting", den_system.backup_settings()["drive_error"])
            self.assertEqual(folder.call_args.args[2], "F1")          # the folder id is remembered
        den_system.set_backup_settings(drive=False)
        with mock.patch.object(den_system.google_drive, "drive_upload") as up:
            den_system.make_backup()
            up.assert_not_called()


class Export(Base):
    def test_hidden_and_secret_things_stay_out(self):
        secret = db.add_project("Vault project", self.project["path"])
        with db._lock:
            db.get_conn().execute("UPDATE projects SET hidden = 1 WHERE id = ?", (secret["id"],))
            db.get_conn().commit()
        db.add_task(secret["id"], "hidden work")
        db.add_task(self.project["id"], "visible work")
        db.set_setting("github_token", "abc")
        body, media, name = den_system.export(["tasks", "projects", "settings"], "json")
        data = json.loads(body)
        self.assertEqual([t["prompt"] for t in data["tasks"]["tasks"]], ["visible work"])
        self.assertNotIn("Vault project", [p["name"] for p in data["projects"]["projects"]])
        self.assertNotIn("github_token", [s["key"] for s in data["settings"]["settings"]])
        self.assertEqual(media, "application/json")

    def test_formats(self):
        db.add_task(self.project["id"], "one")
        body, media, name = den_system.export(["usage"], "csv")
        self.assertEqual((media, name.endswith(".csv")), ("text/csv", True))
        body, media, name = den_system.export(["tasks"], "csv")             # two tables -> a zip of CSVs
        self.assertEqual(media, "application/zip")
        self.assertEqual(sorted(zipfile.ZipFile(io.BytesIO(body)).namelist()), ["tasks/task_events.csv", "tasks/tasks.csv"])
        body, _, _ = den_system.export(["notes", "crm"], "zip")
        self.assertIn("crm.json", zipfile.ZipFile(io.BytesIO(body)).namelist())
        with self.assertRaises(HTTPException):
            den_system.export([], "zip")
        with self.assertRaises(HTTPException):
            den_system.export(["passwords"], "json")
        r = self.client().post("/den/system/export", json={"sets": ["settings"], "format": "json"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
