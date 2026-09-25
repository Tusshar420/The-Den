import base64
import hashlib
import json
import logging
import os
import sqlite3
import stat
import sys
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

import db
from tests.helpers import ME, TestBase
from assistant import connections, net, oauth, token_store

ACCESS, REFRESH = "ya29.ACCESS-SECRET-1", "1//REFRESH-SECRET-1"


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeGoogleAuth(BaseHTTPRequestHandler):
    """Token + revoke endpoints. Class attributes are the knobs and the log."""
    log = []
    mode = "ok"
    expected_verifier_hash = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        type(self).log.append((self.path, form))
        if self.path == "/revoke":
            return self._send(200, {})
        if form.get("grant_type") == "authorization_code":
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if challenge != type(self).expected_verifier_hash or form["code"] != "good-code":
                return self._send(400, {"error": "invalid_grant", "error_description": "Bad code"})
            body = {"access_token": ACCESS, "expires_in": 3600, "scope": form.get("scope") or "openid email",
                    "id_token": jwt({"email": ME})}
            if type(self).mode != "no_refresh":
                body["refresh_token"] = REFRESH
            return self._send(200, body)
        if form.get("grant_type") == "refresh_token":
            if type(self).mode == "revoked":
                return self._send(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return self._send(200, {"access_token": "ya29.ACCESS-SECRET-2", "expires_in": 3600})
        self._send(400, {"error": "unsupported_grant_type"})

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class OAuthBase(TestBase):
    def setUp(self):
        super().setUp()
        FakeGoogleAuth.log, FakeGoogleAuth.mode, FakeGoogleAuth.expected_verifier_hash = [], "ok", None
        self.server = HTTPServer(("127.0.0.1", 0), FakeGoogleAuth)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.provider = replace(oauth.GOOGLE, token_url=base + "/token", revoke_url=base + "/revoke")
        for patcher in (mock.patch.dict(oauth.PROVIDERS, {"google": self.provider}),
                        mock.patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid.apps.example", "GOOGLE_CLIENT_SECRET": "csecret"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sign_in(self, code="good-code"):
        """Run begin -> (browser) -> callback the way a real sign-in would."""
        url = connections.begin("google")["auth_url"]
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        FakeGoogleAuth.expected_verifier_hash = q["code_challenge"]
        return q, lambda c=code, state=None: connections.finish(state or q["state"], c, expected_provider="google")

    def dump_db(self):
        raw = sqlite3.connect(db.DB_PATH)
        return "\n".join(raw.iterdump())


class ConsentUrl(OAuthBase):
    def test_url_uses_pkce_offline_access_and_least_privilege_scopes(self):
        url = connections.begin("google")["auth_url"]
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertEqual((q["response_type"], q["code_challenge_method"], q["access_type"]), ("code", "S256", "offline"))
        self.assertEqual(q["client_id"], "cid.apps.example")
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:8787/assistant/oauth/google/callback")
        scopes = set(q["scope"].split())
        self.assertEqual(scopes, {"openid", "email",
                                  "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
                                  "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.freebusy"})
        self.assertFalse(any(s.endswith(("gmail.modify", "mail.google.com/", "calendar")) for s in scopes),
                         "no scope that could delete mail or manage whole calendars")
        self.assertNotIn("csecret", url)
        self.assertGreater(len(q["state"]), 30)

    def test_a_single_service_can_be_requested(self):
        q = parse_qs(urlparse(connections.begin("google", ["calendar"])["auth_url"]).query)
        self.assertNotIn("gmail", q["scope"][0])
        self.assertIn("calendar.events", q["scope"][0])

    def test_unconfigured_unavailable_and_paid_providers_refuse_to_start(self):
        with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}):
            self.assertEqual(connections.status_of("google")["status"], "not_configured")
            with self.assertRaises(oauth.OAuthError):
                connections.begin("google")
        ms = connections.status_of("microsoft")
        self.assertEqual(ms["status"], "unavailable")
        with self.assertRaises(oauth.OAuthError):
            connections.begin("microsoft")
        paid = replace(self.provider, id="paidco", label="PaidCo", cost="paid")
        with mock.patch.dict(oauth.PROVIDERS, {"paidco": paid}):
            self.assertEqual(connections.status_of("paidco")["cost"], "paid")
            with self.assertRaises(oauth.OAuthError) as cm:
                connections.begin("paidco")
            self.assertIn("paid plan", str(cm.exception))
            db.set_setting("assistant_allow_paid_paidco", True)
            self.assertIn("auth_url", connections.begin("paidco"))

    def test_status_labels(self):
        st = connections.status_of("google")
        self.assertEqual((st["status"], st["cost"]), ("disconnected", "free"))
        self.assertEqual(connections.status_of("microsoft")["cost"], "free")


class Flow(OAuthBase):
    def test_full_sign_in_stores_tokens_in_the_token_store_only(self):
        q, finish = self.sign_in()
        status = finish()
        self.assertEqual((status["status"], status["account"]), ("connected", ME))
        self.assertEqual(sorted(status["services_granted"]), [])  # the fake grants only what it echoes back
        exchange = next(f for p, f in FakeGoogleAuth.log if f.get("grant_type") == "authorization_code")
        self.assertEqual((exchange["client_id"], exchange["client_secret"], exchange["redirect_uri"]),
                         ("cid.apps.example", "csecret", "http://127.0.0.1:8787/assistant/oauth/google/callback"))
        blob = token_store.get("google")
        self.assertEqual((blob["access_token"], blob["refresh_token"]), (ACCESS, REFRESH))
        # ...and nowhere else
        for where, text in (("database", self.dump_db()), ("status JSON", json.dumps(connections.list_status())),
                            ("activity", json.dumps(db.list_activity(50)))):
            for secret in (ACCESS, REFRESH, "csecret", exchange["code_verifier"]):
                self.assertNotIn(secret, text, f"{secret[:8]}... leaked into {where}")

    def test_state_is_single_use_and_provider_bound_and_expires(self):
        q, finish = self.sign_in()
        finish()
        with self.assertRaises(oauth.OAuthError):
            finish()  # replay
        with self.assertRaises(oauth.OAuthError):
            connections.finish("made-up-state", "good-code")
        q, finish = self.sign_in()
        with self.assertRaises(oauth.OAuthError):
            connections.finish(q["state"], "good-code", expected_provider="microsoft")
        q, finish = self.sign_in()
        with mock.patch("time.time", return_value=time.time() + oauth.STATE_TTL_SECONDS + 5):
            with self.assertRaises(oauth.OAuthError):
                finish()

    def test_wrong_code_or_pkce_verifier_is_rejected(self):
        q, finish = self.sign_in()
        with self.assertRaises(oauth.ReauthRequired):
            finish("stolen-or-wrong-code")
        self.assertIsNone(token_store.get("google"))
        self.assertEqual(connections.status_of("google")["status"], "disconnected")

    def test_no_refresh_token_means_no_connection(self):
        FakeGoogleAuth.mode = "no_refresh"
        q, finish = self.sign_in()
        with self.assertRaises(oauth.OAuthError) as cm:
            finish()
        self.assertIn("offline access", str(cm.exception))
        self.assertIsNone(token_store.get("google"))

    def test_access_token_is_reused_until_near_expiry_then_refreshed(self):
        q, finish = self.sign_in()
        finish()
        n = len(FakeGoogleAuth.log)
        self.assertEqual(connections.access_token("google"), ACCESS)
        self.assertEqual(len(FakeGoogleAuth.log), n, "no network while the token is fresh")
        blob = token_store.get("google")
        blob["expires_at"] = time.time() + 10
        token_store.put("google", blob)
        self.assertEqual(connections.access_token("google"), "ya29.ACCESS-SECRET-2")
        self.assertEqual(token_store.get("google")["refresh_token"], REFRESH, "the refresh token is kept")

    def test_revoked_refresh_token_flips_to_reconnect_needed_and_stops_working(self):
        q, finish = self.sign_in()
        finish()
        blob = token_store.get("google")
        blob["expires_at"] = 0
        token_store.put("google", blob)
        FakeGoogleAuth.mode = "revoked"
        with self.assertRaises(oauth.ReauthRequired):
            connections.access_token("google")
        st = connections.status_of("google")
        self.assertEqual(st["status"], "reauth_required")
        self.assertIn("connect the account again", st["last_error"])
        with self.assertRaises(oauth.ReauthRequired):
            connections.access_token("google")

    def test_disconnect_revokes_at_google_and_forgets_everything(self):
        q, finish = self.sign_in()
        finish()
        st = connections.disconnect("google")
        self.assertEqual((st["status"], st["account"]), ("disconnected", None))
        self.assertIsNone(token_store.get("google"))
        revoke = next(f for p, f in FakeGoogleAuth.log if p == "/revoke")
        self.assertEqual(revoke["token"], REFRESH)
        with self.assertRaises(connections.NotConnected):
            connections.access_token("google")
        self.assertEqual([r["action"] for r in db.list_activity(5) if r["tool"] == "connections"], ["disconnect", "connect"])

    def test_partial_consent_is_visible_and_email_only_connection_gives_no_calendar(self):
        self.connect_google(scopes=["https://www.googleapis.com/auth/gmail.readonly",
                                    "https://www.googleapis.com/auth/gmail.compose"])
        st = connections.status_of("google")
        self.assertEqual(st["services_granted"], ["email"])
        self.assertEqual(connections.connected_with("email"), ["google"])
        self.assertEqual(connections.connected_with("calendar"), [])
        from assistant import providers
        with self.assertRaises(connections.NotConnected):
            providers.calendar()


class Scrubbing(unittest.TestCase):
    def test_credentials_are_redacted_from_error_text(self):
        for raw in ("Bearer ya29.a0AfH6SMBxyz", "token=ya29.a0AfH6SMBxyz", "refresh 1//0gABCdef-ghi",
                    '{"access_token": "abc.def-123"}', "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6InhAeS5jb20ifQ.sig",
                    "client_secret=GOCSPX-abc123"):
            out = net.scrub(raw)
            self.assertIn("[redacted]", out, raw)
            for frag in ("ya29", "1//0g", "abc.def", "eyJ", "GOCSPX"):
                self.assertNotIn(frag, out)

    def test_oauth_codes_are_kept_out_of_the_access_log(self):
        import main
        f = main._RedactOAuthParams()
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                ("127.0.0.1:5", "GET", "/assistant/oauth/google/callback?state=SECRETSTATE&code=4/0AXsecret&scope=x", "1.1", 303), None)
        f.filter(rec)
        line = rec.getMessage()
        self.assertNotIn("SECRETSTATE", line)
        self.assertNotIn("4/0AXsecret", line)
        self.assertIn("scope=x", line)


class TokenStores(unittest.TestCase):
    def test_file_store_is_owner_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = token_store.FileStore(os.path.join(d, "sub", "tokens.json"))
            store.set("google", {"refresh_token": "r"})
            self.assertEqual(store.get("google"), {"refresh_token": "r"})
            self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(store.path.parent).st_mode), 0o700)
            store.delete("google")
            self.assertIsNone(store.get("google"))

    @unittest.skipUnless(sys.platform == "darwin", "macOS Keychain only")
    def test_keychain_roundtrip_keeps_the_secret_out_of_argv(self):
        store = token_store.KeychainStore()
        with mock.patch.object(token_store, "SERVICE", "com.tushar.octopus.selftest"):
            self.addCleanup(store.delete, "unittest")
            blob = {"refresh_token": "1//" + "x" * 2500, "expires_at": 1}
            seen = []
            real_run = token_store.subprocess.run

            def spy(cmd, **kw):
                seen.append(cmd)
                return real_run(cmd, **kw)
            with mock.patch.object(token_store.subprocess, "run", spy):
                store.set("unittest", blob)
            self.assertEqual(seen, [["security", "-i"]], "the token travels over stdin, not the command line")
            self.assertEqual(store.get("unittest"), blob)
            store.delete("unittest")
            self.assertIsNone(store.get("unittest"))

    def test_keychain_rejects_account_names_that_could_inject_commands(self):
        with self.assertRaises(token_store.TokenStoreError):
            token_store.KeychainStore().set("x -w evil", {})


if __name__ == "__main__":
    unittest.main()
