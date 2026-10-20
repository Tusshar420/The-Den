"""Signing in to the dashboard with a username and password (login.py): the login itself, the
session cookie it gives a browser, and the API key still working - plus the key never staying in
a /dashboard link."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from unittest import mock

import uvicorn

import api
import login
from tests.helpers import TestBase


class LoginStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(login, "FILE", Path(tmp.name) / "login.json")
        p.start()
        self.addCleanup(p.stop)

    def test_nothing_set_up_at_first(self):
        self.assertFalse(login.configured())
        self.assertIsNone(login.username())
        self.assertFalse(login.check_password("tushar", "whatever1"))

    def test_password_is_checked_and_never_stored(self):
        login.set_login("Tushar", "correct horse")
        self.assertTrue(login.check_password("tushar", "correct horse"))      # username isn't case-sensitive
        self.assertTrue(login.check_password(" Tushar ", "correct horse"))
        self.assertFalse(login.check_password("tushar", "wrong horse"))
        self.assertFalse(login.check_password("someone", "correct horse"))
        self.assertNotIn("correct horse", login.FILE.read_text())
        self.assertEqual(login.FILE.stat().st_mode & 0o777, 0o600)

    def test_weak_logins_are_refused(self):
        for name, pw in (("", "longenough"), ("has space", "longenough"), ("tushar", "short"), ("x" * 65, "longenough")):
            with self.assertRaises(ValueError, msg=(name, pw)):
                login.set_login(name, pw)
        self.assertFalse(login.configured())

    def test_sessions_last_until_they_expire_or_end(self):
        login.set_login("tushar", "longenough")
        token, max_age = login.new_session(True)
        self.assertEqual(max_age, login.REMEMBER_SECONDS)
        self.assertTrue(login.valid_session(token))
        self.assertNotIn(token, login.FILE.read_text())                       # only its hash is kept
        self.assertFalse(login.valid_session(token + "x"))
        self.assertFalse(login.valid_session(None))
        short, short_age = login.new_session(False)
        self.assertIsNone(short_age)                                          # a cookie for this browser session
        later = time.time() + login.SHORT_SECONDS + 5
        with mock.patch.object(login.time, "time", lambda: later):
            self.assertFalse(login.valid_session(short))
            self.assertTrue(login.valid_session(token))
        login.end_session(token)
        self.assertFalse(login.valid_session(token))
        self.assertTrue(login.valid_session(short))

    def test_a_new_login_signs_every_device_out(self):
        login.set_login("tushar", "longenough")
        token, _ = login.new_session(True)
        login.set_login("tushar", "evenlonger")
        self.assertFalse(login.valid_session(token))


class LoginApi(TestBase):
    KEY = "test-key"

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(api, "API_KEY", self.KEY),
                  mock.patch.object(login, "FILE", Path(self._tmp.name) / "login.json")):
            p.start()
            self.addCleanup(p.stop)
        self.server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=0, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.addCleanup(self._stop)
        self.cookie: Optional[str] = None

    def _stop(self):
        self.server.should_exit = True
        time.sleep(0.2)

    def http(self, method, path, body=None, key=None):
        """Like a browser: keeps the session cookie the server gives it, and sends it back."""
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-API-Key"] = key
        if self.cookie:
            headers["Cookie"] = f"{login.COOKIE}={self.cookie}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, raw, set_cookie, url = r.status, r.read(), r.headers.get_all("set-cookie") or [], r.geturl()
        except urllib.error.HTTPError as e:
            status, raw, set_cookie, url = e.code, e.read(), e.headers.get_all("set-cookie") or [], path
        for c in set_cookie:
            if c.startswith(login.COOKIE + "="):
                value = c.split(";")[0].split("=", 1)[1].strip('"')
                self.cookie = value or None
                self.last_cookie = c
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            data = None
        return status, data, url

    def setup_login(self, **extra):
        return self.http("POST", "/auth/setup", {"username": "tushar", "password": "longenough", **extra})

    def test_creating_the_login_needs_the_api_key(self):
        self.assertEqual(self.http("GET", "/auth/state")[1], {"configured": False, "signed_in": False, "via": None, "username": None})
        self.assertEqual(self.setup_login()[0], 401)
        self.assertEqual(self.setup_login(key="wrong")[0], 401)
        self.assertEqual(self.setup_login(key=self.KEY, password="short")[0], 400)
        self.assertFalse(login.configured())
        status, data, _ = self.setup_login(key=self.KEY)
        self.assertEqual((status, data["username"]), (200, "tushar"))
        self.assertIn("HttpOnly", self.last_cookie)
        self.assertIn("SameSite=strict", self.last_cookie)
        self.assertEqual(self.http("GET", "/projects")[0], 200)                # the cookie alone opens the API
        self.assertEqual(self.http("GET", "/auth/state")[1]["via"], "session")

    def test_sign_in_and_out(self):
        self.setup_login(key=self.KEY)
        self.cookie = None
        self.assertEqual(self.http("GET", "/projects")[0], 401)
        self.assertEqual(self.http("POST", "/auth/login", {"username": "tushar", "password": "nope-nope"})[0], 401)
        self.assertIsNone(self.cookie)
        self.assertEqual(self.http("POST", "/auth/login", {"username": "Tushar", "password": "longenough"})[0], 200)
        self.assertEqual(self.http("GET", "/projects")[0], 200)
        old = self.cookie
        self.assertEqual(self.http("POST", "/auth/logout", {})[0], 200)
        self.cookie = old                                                     # even a kept copy no longer works
        self.assertEqual(self.http("GET", "/projects")[0], 401)

    def test_the_api_key_still_works_and_can_reset_the_password(self):
        self.setup_login(key=self.KEY)
        phone = self.cookie
        self.cookie = None
        self.assertEqual(self.http("GET", "/projects", key=self.KEY)[0], 200)
        self.assertEqual(self.http("POST", "/auth/setup", {"username": "tushar", "password": "brandnew1", "key": self.KEY})[0], 200)
        self.cookie = phone                                                   # the reset signed the phone out
        self.assertEqual(self.http("GET", "/projects")[0], 401)
        self.cookie = None
        self.assertEqual(self.http("POST", "/auth/login", {"username": "tushar", "password": "brandnew1"})[0], 200)

    def test_changing_the_password_with_the_current_one(self):
        self.setup_login(key=self.KEY)
        self.assertEqual(self.http("POST", "/auth/setup", {"username": "tushar", "password": "second-pw", "current_password": "wrong"})[0], 401)
        self.assertEqual(self.http("POST", "/auth/setup", {"username": "tushar", "password": "second-pw", "current_password": "longenough"})[0], 200)
        self.assertTrue(login.check_password("tushar", "second-pw"))

    def test_a_kept_key_is_swapped_for_a_session(self):
        self.assertEqual(self.http("POST", "/auth/key-session", key=self.KEY)[0], 409)    # no login yet
        self.setup_login(key=self.KEY)
        self.cookie = None
        self.assertEqual(self.http("POST", "/auth/key-session", key="wrong")[0], 401)
        self.assertEqual(self.http("POST", "/auth/key-session", key=self.KEY)[0], 200)
        self.assertEqual(self.http("GET", "/projects")[0], 200)

    def test_wrong_passwords_lock_out_but_signed_out_requests_do_not(self):
        self.setup_login(key=self.KEY)
        self.cookie = None
        for _ in range(api.FAIL_LIMIT * 2):
            self.assertEqual(self.http("GET", "/projects")[0], 401)                    # no credentials isn't a guess
        for _ in range(api.FAIL_LIMIT):
            self.http("POST", "/auth/login", {"username": "tushar", "password": "guessing"})
        self.assertEqual(self.http("POST", "/auth/login", {"username": "tushar", "password": "longenough"})[0], 429)

    def test_a_key_in_the_dashboard_link_is_dropped(self):
        status, _, url = self.http("GET", "/dashboard?key=%20secret&status=error")
        self.assertEqual(status, 200)
        self.assertEqual(url, f"http://127.0.0.1:{self.port}/dashboard?status=error")
        _, _, url = self.http("GET", "/dashboard?key=secret")
        self.assertEqual(url, f"http://127.0.0.1:{self.port}/dashboard")


if __name__ == "__main__":
    unittest.main()
