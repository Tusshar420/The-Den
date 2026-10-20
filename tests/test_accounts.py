"""Claude accounts (accounts.py): which subscription Claude Code runs on, and each account's limits."""

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

import accounts
import api
import db
from tests.helpers import TestBase

TOKEN = "sk-ant-oat01-" + "a" * 40


def usage(util):
    resets = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    return {"status": "allowed", "windows": {"five_hour": {"utilization": util, "resets_at": resets}}}


class Accounts(TestBase):
    def setUp(self):
        super().setUp()
        self._old_file, accounts.FILE = accounts.FILE, Path(self._tmp.name) / "accounts.json"
        self.addCleanup(lambda: setattr(accounts, "FILE", self._old_file))
        accounts._mac_cache.update(at=9e18, info={"logged_in": True, "email": "work@example.com", "org": "Work", "plan": "team"})
        self.addCleanup(lambda: accounts._mac_cache.update(at=0.0, info=None))
        # No real Claude Code runs: the "tiny request" answers from this table.
        self.probe_answers = {}
        self._old_probe = accounts.probe

        def fake_probe(account_id, timeout=90):
            ok, util = self.probe_answers.get(account_id, (True, 0.25))
            if ok:
                accounts.record_usage(account_id, usage(util))
            accounts.set_auth_error(account_id, None if ok else "Failed to authenticate. API Error: 401")
            return {"ok": ok, "usage": usage(util) if ok else None, "error": None if ok else "Failed to authenticate"}
        accounts.probe = fake_probe
        self.addCleanup(lambda: setattr(accounts, "probe", self._old_probe))
        self._old_bg, accounts.probe_in_background = accounts.probe_in_background, lambda i: None
        self.addCleanup(lambda: setattr(accounts, "probe_in_background", self._old_bg))

    def add(self, label="Personal", token=TOKEN):
        return accounts.add(label, token, "me@example.com")

    def test_the_macs_login_is_the_default_and_uses_no_token(self):
        self.assertEqual(accounts.active_id(), "mac")
        env = accounts.env_for("mac", {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "stale"})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_a_token_account_runs_with_its_token(self):
        a = self.add()
        self.assertEqual(accounts.env_for(a["id"], {})["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)

    def test_tokens_are_saved_private_and_never_listed(self):
        self.add()
        mode = stat.S_IMODE(os.stat(accounts.FILE).st_mode)
        self.assertEqual(mode, 0o600)
        listed = accounts.public_list()
        self.assertEqual([a["id"] for a in listed][0], "mac")
        self.assertNotIn(TOKEN, repr(listed))
        self.assertNotIn(TOKEN, repr(api.get_settings()))

    def test_a_token_that_cant_log_in_is_not_saved(self):
        self.probe_answers["*"] = None
        accounts.probe = lambda account_id, timeout=90: {"ok": False, "usage": None, "error": "Failed to authenticate"}
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(len(accounts.public_list()), 1)

    def test_rejects_things_that_are_not_tokens_and_duplicates(self):
        with self.assertRaises(ValueError):
            accounts.add("Personal", "not a token")
        self.add()
        with self.assertRaises(ValueError):
            self.add("Again")

    def test_switching_shows_that_accounts_limits(self):
        a = self.add()                                   # its probe recorded 25%
        accounts.record_usage("mac", usage(0.80))
        self.assertEqual(db.get_setting("claude_usage")["windows"]["five_hour"]["utilization"], 0.80)
        accounts.activate(a["id"])
        self.assertEqual(db.get_setting("claude_usage")["windows"]["five_hour"]["utilization"], 0.25)
        # a run still finishing on the old account doesn't overwrite the bars
        accounts.record_usage("mac", usage(0.90))
        self.assertEqual(db.get_setting("claude_usage")["windows"]["five_hour"]["utilization"], 0.25)
        accounts.activate("mac")
        self.assertEqual(db.get_setting("claude_usage")["windows"]["five_hour"]["utilization"], 0.90)

    def test_switch_endpoint_lets_paused_tasks_try_again(self):
        a = self.add()
        t = db.add_task(self.project["id"], "something")
        later = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        db.mark_rate_limited(t["id"], later)
        view = api.activate_account(a["id"])
        self.assertEqual(view["claude_account"], a["id"])
        self.assertEqual(view["resumed"], 1)
        self.assertLessEqual(db.get_task(t["id"])["resume_at"], datetime.now(timezone.utc).isoformat())
        with self.assertRaises(HTTPException):
            api.activate_account("nope")

    def test_removing_the_active_account_goes_back_to_the_mac(self):
        a = self.add()
        accounts.activate(a["id"])
        accounts.remove(a["id"])
        self.assertEqual(accounts.active_id(), "mac")
        with self.assertRaises(ValueError):
            accounts.remove("mac")   # nothing else to run on

    def test_the_macs_login_can_be_hidden_once_there_is_a_token_account(self):
        a = self.add()
        db.update_project_settings(self.project["id"], claude_account="mac")
        self.assertTrue(accounts.remove("mac"))
        self.assertEqual([x["id"] for x in accounts.public_list()], [a["id"]])
        self.assertEqual(accounts.active_id(), a["id"])
        self.assertEqual(accounts.for_project(self.project["id"]), a["id"])
        self.assertFalse(accounts.exists("mac"))
        self.assertTrue(api.get_settings()["claude_mac_hidden"])
        accounts.show_mac()
        self.assertEqual([x["id"] for x in accounts.public_list()], ["mac", a["id"]])
        self.assertEqual(accounts.active_id(), a["id"])   # coming back doesn't make it the default

    def test_the_hidden_macs_login_comes_back_when_the_last_token_goes(self):
        a = self.add()
        accounts.remove("mac")
        accounts.remove(a["id"])
        self.assertEqual(accounts.active_id(), "mac")
        self.assertEqual([x["id"] for x in accounts.public_list()], ["mac"])

    def test_login_failures_are_recognised(self):
        for text in ("Failed to authenticate. API Error: 401 OAuth access token is invalid.",
                     "OAuth token has expired", "Invalid API key · Please run /login"):
            self.assertTrue(accounts.AUTH_FAILED.search(text), text)
        self.assertIsNone(accounts.AUTH_FAILED.search("Added a 401 handler to the login page"))


class UsageAfterReset(TestBase):
    """A reading only changes when something runs, so a window past its reset shows as empty."""

    def test_a_window_past_its_reset_reads_empty(self):
        now = datetime.now(timezone.utc)
        stored = {"status": "rejected", "limited_window": "five_hour", "resets_at": (now - timedelta(minutes=5)).isoformat(),
                  "windows": {"five_hour": {"utilization": 1.0, "resets_at": (now - timedelta(minutes=5)).isoformat()},
                              "seven_day": {"utilization": 0.6, "resets_at": (now + timedelta(days=2)).isoformat()}}}
        got = accounts.current(stored, now)
        self.assertEqual(got["windows"]["five_hour"], {"utilization": 0, "resets_at": None})
        self.assertEqual(got["windows"]["seven_day"], stored["windows"]["seven_day"])
        self.assertEqual((got["status"], got["limited_window"], got["resets_at"]), ("allowed", None, None))
        self.assertEqual(stored["windows"]["five_hour"]["utilization"], 1.0)   # what's stored is left alone

    def test_a_current_reading_is_unchanged(self):
        u = usage(0.7)
        self.assertEqual(accounts.current(u), u)
        self.assertIsNone(accounts.current(None))

    def test_the_accounts_list_shows_it(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        accounts.record_usage("mac", {"status": "allowed", "windows": {"five_hour": {"utilization": 0.9, "resets_at": past}}})
        mac = next((a for a in accounts.public_list() if a["id"] == "mac"), None)
        if mac:
            self.assertEqual(mac["usage"]["windows"]["five_hour"]["utilization"], 0)
