"""
More than one account per provider: several OpenAI / Gemini API keys
(api_keys.py, tried in order by providers.py) and several Google accounts
(assistant/connections.py, chosen per call by the email and calendar tools).
"""

import json
import os
import sqlite3
import stat
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import api_keys
import db
import providers as llm
from assistant import actions, connections, oauth, token_store, tools
from assistant.providers.base import ProviderError
from tests.helpers import ME, FakeCalendar, FakeEmail, TestBase, event, message
from tests.test_oauth import FakeGoogleAuth, OAuthBase

WORK = "tushar@lawaccounting.com"
ENV_KEY, WORK_KEY = "sk-env-" + "e" * 30, "sk-work-" + "w" * 30
ALL_SCOPES = [s for svc in oauth.GOOGLE.service_scopes.values() for s in svc]


# ------------------------------------------------------------------ API keys

class ApiKeys(TestBase):
    def setUp(self):
        super().setUp()
        self._old_file, api_keys.FILE = api_keys.FILE, Path(self._tmp.name) / "api_keys.json"
        self.addCleanup(lambda: setattr(api_keys, "FILE", self._old_file))
        env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": ENV_KEY})
        env.start()
        self.addCleanup(env.stop)

    def add_work(self):
        return api_keys.add("openai", "Work", WORK_KEY, check=False)

    def test_the_plist_key_comes_first_and_added_keys_are_kept_owner_only(self):
        added = self.add_work()
        listed = api_keys.public_list("openai")
        self.assertEqual([k["id"] for k in listed], ["openai-env", added["id"]])
        self.assertEqual([k["default"] for k in listed], [True, False])
        self.assertEqual(listed[1]["hint"], "…wwww")
        self.assertNotIn(WORK_KEY, json.dumps(listed), "the page never gets the key itself")
        self.assertNotIn(ENV_KEY, json.dumps(listed))
        self.assertEqual(stat.S_IMODE(os.stat(api_keys.FILE).st_mode), 0o600)
        self.assertEqual(api_keys.public_list("gemini"), [], "keys are per provider")
        with self.assertRaisesRegex(ValueError, "already saved"):
            api_keys.add("openai", "Again", WORK_KEY, check=False)
        with self.assertRaisesRegex(ValueError, "service settings"):
            api_keys.remove("openai-env")

    def calls(self, fail):
        """Patch the one-key OpenAI call: `fail` maps a key to the error it raises."""
        used = []

        def one(body, key, timeout):
            used.append(key)
            if key in fail:
                raise fail[key]
            return llm.Completion("hi from " + key[:7], 1, 1)
        p = mock.patch.object(llm, "_openai_call", one)
        p.start()
        self.addCleanup(p.stop)
        return used

    def test_the_default_key_is_used_first_and_a_limited_one_hands_over(self):
        added = self.add_work()
        api_keys.make_default(added["id"])
        self.assertEqual([k for k, _ in api_keys.keys_for("openai")], [added["id"], "openai-env"])
        used = self.calls({WORK_KEY: llm.ProviderError("OpenAI rate limit", rate_limited=True, key_problem=True)})
        out = llm.openai_complete("gpt-x", [{"role": "user", "content": "hi"}])
        self.assertEqual(used, [WORK_KEY, ENV_KEY])
        self.assertEqual(out.text, "hi from sk-env-")
        self.assertIsNone(api_keys.public_list("openai")[1]["error"], "a rate limit isn't the key's fault")

    def test_a_rejected_key_is_marked_and_skipped(self):
        added = self.add_work()
        api_keys.make_default(added["id"])
        used = self.calls({WORK_KEY: llm.ProviderError("OpenAI error 401: bad key", key_problem=True)})
        llm.openai_complete("gpt-x", [{"role": "user", "content": "hi"}])
        self.assertEqual(used, [WORK_KEY, ENV_KEY])
        work = next(k for k in api_keys.public_list("openai") if k["id"] == added["id"])
        self.assertIn("401", work["error"])

    def test_a_bad_request_is_not_tried_again_on_other_keys(self):
        self.add_work()
        used = self.calls({ENV_KEY: llm.ProviderError("OpenAI error 400: bad model")})
        with self.assertRaisesRegex(llm.ProviderError, "bad model"):
            llm.openai_complete("gpt-x", [{"role": "user", "content": "hi"}])
        self.assertEqual(used, [ENV_KEY])

    def test_every_key_limited_reports_the_rate_limit(self):
        self.add_work()
        limited = llm.ProviderError("OpenAI rate limit", rate_limited=True, key_problem=True, retry_after=30)
        self.calls({ENV_KEY: llm.ProviderError("no credit", key_problem=True), WORK_KEY: limited})
        with self.assertRaises(llm.ProviderError) as ctx:
            llm.openai_complete("gpt-x", [{"role": "user", "content": "hi"}])
        self.assertTrue(ctx.exception.rate_limited, "so the router waits instead of failing the task")

    def test_an_added_key_alone_makes_the_provider_ready(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(llm.provider_ready("openai"), (False, "OPENAI_API_KEY is not set"))
            self.add_work()
            self.assertEqual(llm.provider_ready("openai"), (True, ""))
            self.assertEqual(api_keys.default_id("openai"), api_keys.public_list("openai")[0]["id"])


# ------------------------------------------------------- Google accounts

class GoogleAccounts(OAuthBase):
    def connect(self, email):
        FakeGoogleAuth.email, FakeGoogleAuth.scope = email, " ".join(["openid", "email"] + ALL_SCOPES)
        _, finish = self.sign_in()
        return finish()

    def test_a_second_account_is_added_next_to_the_first(self):
        self.connect(ME)
        st = self.connect(WORK)
        self.assertEqual([a["account"] for a in st["accounts"]], [ME, WORK])
        first, second = (a["id"] for a in st["accounts"])
        self.assertEqual(first, "google", "the first account keeps the id it always had")
        self.assertTrue(second.startswith("google-"))
        self.assertEqual(connections.connected_with("email"), [first, second])
        self.assertTrue(token_store.get(first) and token_store.get(second), "each account has its own tokens")
        self.assertEqual(st["accounts"][0]["default_for"], ["email", "calendar", "drive"])

    def test_signing_in_again_with_the_same_account_refreshes_it(self):
        self.connect(ME)
        self.connect(WORK)
        st = self.connect(WORK)
        self.assertEqual(len(st["accounts"]), 2)

    def test_default_and_choosing_an_account_by_name(self):
        self.connect(ME)
        work = self.connect(WORK)["accounts"][1]["id"]
        connections.set_default("email", work)
        self.assertEqual(connections.connected_with("email")[0], work)
        self.assertEqual(connections.connected_with("calendar")[0], "google", "defaults are per service")
        self.assertEqual(connections.resolve("email", "lawaccounting"), work)
        self.assertEqual(connections.resolve("email", ME.upper()), "google")
        with self.assertRaisesRegex(connections.NotConnected, "Connected: .*" + WORK):
            connections.resolve("email", "yahoo")
        with self.assertRaisesRegex(connections.NotConnected, "More than one"):
            connections.resolve("email", "tushar")

    def test_disconnecting_one_keeps_the_other(self):
        self.connect(ME)
        work = self.connect(WORK)["accounts"][1]["id"]
        connections.set_default("calendar", work)
        st = connections.disconnect(work)
        self.assertEqual([a["account"] for a in st["accounts"]], [ME])
        self.assertIsNone(token_store.get(work))
        self.assertEqual(connections.connected_with("calendar"), ["google"])
        self.assertIsNone(db.get_setting("assistant_default_calendar"))

    def test_reconnect_suggests_the_account_and_google_always_offers_a_choice(self):
        self.connect(WORK)
        q = {k: v[0] for k, v in parse_qs(urlparse(connections.begin("google", reconnect="google")["auth_url"]).query).items()}
        self.assertEqual(q["login_hint"], WORK)
        self.assertIn("select_account", q["prompt"].split())

    def test_an_old_single_account_database_is_upgraded(self):
        db._conn.close()
        db._conn = None
        raw = sqlite3.connect(db.DB_PATH)
        raw.execute("DROP TABLE connections")
        raw.execute("CREATE TABLE connections (provider TEXT PRIMARY KEY, account TEXT, status TEXT NOT NULL, "
                    "scopes TEXT NOT NULL DEFAULT '[]', connected_at TEXT, last_error TEXT, updated_at TEXT NOT NULL)")
        raw.execute("INSERT INTO connections VALUES ('google', ?, 'connected', ?, 'then', NULL, 'then')", (ME, json.dumps(ALL_SCOPES)))
        raw.commit()
        raw.close()
        row = db.get_connection("google")
        self.assertEqual((row["id"], row["provider"], row["account"]), ("google", "google", ME))
        self.assertEqual(connections.connected_with("email"), ["google"])


# ------------------------------------------------- tools on two accounts

class Box(FakeEmail):
    def __init__(self, conn, addr, messages):
        super().__init__(messages)
        self.conn, self.addr = conn, addr

    def account(self):
        return self.addr

    def get(self, message_id):
        if message_id not in self.messages:
            e = ProviderError("Google couldn't find that item")
            e.status = 404
            raise e
        return super().get(message_id)


class Cal(FakeCalendar):
    def __init__(self, conn, addr, events):
        super().__init__(events)
        self.conn, self.addr = conn, addr

    def account(self):
        return self.addr

    def get_event(self, event_id):
        if event_id not in self.events:
            e = ProviderError("Google couldn't find that item")
            e.status = 404
            raise e
        return super().get_event(event_id)


class ToolsOnTwoAccounts(TestBase):
    def setUp(self):
        super().setUp()
        tools.load_all()
        db.save_connection("google", ME, "connected", ALL_SCOPES)
        db.save_connection("google-w", WORK, "connected", ALL_SCOPES)
        self.home = Box("google", ME, [message("m1", subject="Dinner")])
        self.work = Box("google-w", WORK, [message("m2", subject="Proposal", to=[WORK])])
        self.cal_home = Cal("google", ME, [event("e1", "Gym")])
        self.cal_work = Cal("google-w", WORK, [event("e2", "Client call")])
        boxes = {"email": {"google": self.home, "google-w": self.work},
                 "calendar": {"google": self.cal_home, "google-w": self.cal_work}}

        def members(service, account=None):
            ids = [connections.resolve(service, account)] if account else connections.connected_with(service)
            return [boxes[service][i] for i in ids]
        p = mock.patch("assistant.providers._members", members)
        p.start()
        self.addCleanup(p.stop)
        self.c = actions.make_context(None)

    def run_tool(self, tool, action, args):
        t = tools.get(tool)
        prepared = t.prepare(action, args, self.c)
        return prepared, t.execute(action, prepared.args, self.c)

    def test_searching_covers_every_mailbox_and_says_which(self):
        _, out = self.run_tool("email", "search", {"query": "rahul"})
        self.assertEqual({(m["id"], m["account"]) for m in out["data"]["messages"]}, {("m1", ME), ("m2", WORK)})
        self.assertIn("in " + WORK, out["text"])

    def test_a_reply_goes_out_from_the_account_that_got_the_message(self):
        prepared, _ = self.run_tool("email", "reply", {"message_id": "m2", "body": "Sending it today."})
        self.assertEqual(prepared.args["account"], "google-w")
        self.assertIn({"label": "Account", "value": WORK, "block": False}, prepared.preview["fields"])
        self.assertEqual(len(self.work.sent), 1)
        self.assertEqual(self.home.sent, [])

    def test_new_mail_uses_the_named_account_or_else_the_default(self):
        prepared, _ = self.run_tool("email", "send", {"to": "a@example.com", "subject": "Hi", "body": "Hello"})
        self.assertEqual(prepared.args["account"], "google")
        self.assertEqual(len(self.home.sent), 1)
        prepared, _ = self.run_tool("email", "send", {"to": "a@example.com", "subject": "Hi", "body": "Hello",
                                                      "account": "lawaccounting"})
        self.assertEqual(prepared.args["account"], "google-w")
        self.assertEqual(len(self.work.sent), 1)
        connections.set_default("email", "google-w")
        prepared, _ = self.run_tool("email", "send", {"to": "a@example.com", "subject": "Hi", "body": "Hello"})
        self.assertEqual(prepared.args["account"], "google-w")

    def test_calendars_are_read_together_and_changes_go_to_the_one_with_the_event(self):
        _, out = self.run_tool("calendar", "list_events", {"start": "2026-09-25T00:00:00+05:30", "end": "2026-09-26T00:00:00+05:30"})
        self.assertEqual({e["id"] for e in out["data"]["events"]}, {"e1", "e2"})
        prepared, _ = self.run_tool("calendar", "cancel_event", {"event_id": "e2"})
        self.assertEqual(prepared.args["account"], "google-w")
        self.assertEqual([d[0] for d in self.cal_work.deleted], ["e2"])
        self.assertEqual(self.cal_home.deleted, [])

    def test_the_planner_is_told_about_the_accounts(self):
        cat = tools.catalog()
        self.assertIn("email.send(to, subject, body, cc?, account?)", cat)
        self.assertIn(f"{ME} (default), {WORK}", cat)
        connections.disconnect("google-w")
        self.assertNotIn("account?", tools.catalog(), "one account: nothing to choose")
