"""The API key check: constant-time compare, a failed-attempt limit, and refusing
to start an unprotected API on a network-reachable address."""

import unittest
from unittest import mock

from fastapi import HTTPException, Request

import api


def request_from(host="100.64.0.7") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [],
                    "client": (host, 5000)})


class KeyCheck(unittest.TestCase):
    KEY = "s3cret-key"

    def setUp(self):
        for p in (mock.patch.object(api, "API_KEY", self.KEY), mock.patch.dict(api._failures, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def status(self, key, host="100.64.0.7"):
        try:
            api.require_api_key(request_from(host), key)
        except HTTPException as e:
            return e.status_code
        return 200

    def test_right_key_passes_wrong_or_missing_is_401(self):
        self.assertEqual(self.status(self.KEY), 200)
        self.assertEqual(self.status("nope"), 401)
        self.assertEqual(self.status(None), 401)
        self.assertEqual(self.status("café"), 401)              # non-ASCII must not crash the compare

    def test_uses_a_constant_time_compare(self):
        with mock.patch.object(api.hmac, "compare_digest", return_value=True) as cmp:
            self.assertEqual(self.status("anything"), 200)
        cmp.assert_called_once()

    def test_too_many_failures_lock_the_client_out_even_with_the_right_key(self):
        for _ in range(api.FAIL_LIMIT):
            self.assertEqual(self.status("bad"), 401)
        with self.assertRaises(HTTPException) as cm:
            api.require_api_key(request_from(), self.KEY)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertGreaterEqual(int(cm.exception.headers["Retry-After"]), 1)
        self.assertEqual(self.status(self.KEY, host="100.64.0.8"), 200)   # someone else is unaffected

    def test_failures_age_out(self):
        clock = [1000.0]
        with mock.patch.object(api.time, "monotonic", lambda: clock[0]):
            for _ in range(api.FAIL_LIMIT):
                self.status("bad")
            self.assertEqual(self.status(self.KEY), 429)
            clock[0] += api.FAIL_WINDOW_SECONDS + 1
            self.assertEqual(self.status(self.KEY), 200)

    def test_successes_do_not_count_against_the_limit(self):
        for _ in range(api.FAIL_LIMIT * 3):
            self.assertEqual(self.status(self.KEY), 200)


class Startup(unittest.TestCase):
    def test_no_key_on_a_reachable_address_refuses_to_start(self):
        for host in ("0.0.0.0", "192.168.1.5", "::"):
            with self.assertRaises(SystemExit, msg=host):
                api.check_bind_is_safe(host, api_key="")

    def test_no_key_is_fine_on_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            api.check_bind_is_safe(host, api_key="")

    def test_a_key_is_fine_anywhere(self):
        api.check_bind_is_safe("0.0.0.0", api_key="k")

    def test_defaults_to_the_configured_key(self):
        with mock.patch.object(api, "API_KEY", None):
            with self.assertRaises(SystemExit):
                api.check_bind_is_safe("0.0.0.0")
        with mock.patch.object(api, "API_KEY", "k"):
            api.check_bind_is_safe("0.0.0.0")


if __name__ == "__main__":
    unittest.main()
