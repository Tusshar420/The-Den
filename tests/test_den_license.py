"""
Licensing (modules/den_license/license.py): signing, the owner's store, machine-locking at check-in,
the offline window, and the install-side decision. Pure logic + a loopback check-in server; no real
Keychain, no real network beyond 127.0.0.1.
"""

import json
import socket
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone

from tests import helpers  # noqa: F401  (puts the Den on sys.path)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from modules.den_license import license as lic
from modules.den_license.license import LicenseError


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Memory:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return json.loads(json.dumps(self.d.get(k, default)))
    def put(self, k, v): self.d[k] = json.loads(json.dumps(v))


def iso(dt): return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class SigningTest(unittest.TestCase):
    def test_sign_verify_tamper(self):
        key = Ed25519PrivateKey.generate()
        payload = {"id": "a1", "name": "Priya", "tools": ["web"], "issued_at": "2026-10-21T00:00:00Z",
                   "expires_at": None, "max_offline_days": 30, "server_url": "http://x", "nonce": "n"}
        token = lic.make_token(key, payload)
        data = lic.read_token(token)
        pub = lic.public_key_b64(key)
        lic.verify_token(pub, data)                                  # ok
        data2 = dict(data); data2["tools"] = ["web", "terminal"]      # tamper
        with self.assertRaises(LicenseError):
            lic.verify_token(pub, data2)
        with self.assertRaises(LicenseError):                        # wrong owner
            lic.verify_token(lic.public_key_b64(Ed25519PrivateKey.generate()), data)

    def test_read_bad_token(self):
        for bad in ("", "not-base64!!", lic.base64.urlsafe_b64encode(b"{}").decode()):
            with self.assertRaises(LicenseError):
                lic.read_token(bad)

    def test_clean_tools(self):
        self.assertEqual(lic.clean_tools("*"), "*")
        self.assertEqual(lic.clean_tools(["web", "web", "nope", "terminal"]), ["web", "terminal"])
        with self.assertRaises(LicenseError):
            lic.clean_tools(42)

    def test_machine_id_stable(self):
        self.assertEqual(lic.machine_id(), lic.machine_id())
        self.assertEqual(len(lic.machine_id()), 32)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.mem = Memory()
        self.mgr = lic.LicenseManager(self.mem.get, self.mem.put, key_fn=lambda: self.key)

    def test_create_list_edit_revoke(self):
        rec, token = self.mgr.create("Priya's Mac", ["web", "gmail"], expires_days=30)
        lic.verify_token(lic.public_key_b64(self.key), lic.read_token(token))
        pub = self.mgr.public()
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["name"], "Priya's Mac")
        self.assertEqual(pub[0]["tools"], ["web", "gmail"])
        self.assertIsNone(pub[0]["machine_id"])
        self.assertTrue(self.mgr.set_tools(rec["id"], "*"))
        self.assertEqual(self.mgr.public()[0]["tools"], "*")
        self.assertTrue(self.mgr.revoke(rec["id"]))
        self.assertTrue(self.mgr.public()[0]["revoked"])
        self.assertTrue(self.mgr.revoke(rec["id"], False))
        self.assertFalse(self.mgr.public()[0]["revoked"])
        self.assertFalse(self.mgr.set_tools("nope", "*"))

    def test_create_needs_name(self):
        with self.assertRaises(LicenseError):
            self.mgr.create("   ", "*")

    def test_check_binds_machine_and_rejects_others(self):
        rec, token = self.mgr.create("Ravi", "*")
        data = lic.read_token(token)
        r1 = self.mgr.check(data, "machineAAA", "2.1.0", "1.1.1.1")
        self.assertTrue(r1["valid"])
        self.assertEqual(r1["tools"], "*")
        self.assertEqual(self.mgr.public()[0]["machine_id"], "machineAAA")
        r2 = self.mgr.check(data, "machineAAA", "2.1.0", "1.1.1.1")     # same machine ok
        self.assertTrue(r2["valid"])
        r3 = self.mgr.check(data, "machineBBB", "2.1.0", "2.2.2.2")     # different machine -> rejected
        self.assertFalse(r3["valid"])
        self.assertEqual(r3["reason"], "machine")

    def test_check_revoked_and_unknown(self):
        rec, token = self.mgr.create("X", "*")
        data = lic.read_token(token)
        self.mgr.revoke(rec["id"])
        self.assertEqual(self.mgr.check(data, "m", "", "ip")["reason"], "revoked")
        fake = {**data, "id": "deadbeef"}
        self.assertEqual(self.mgr.check(fake, "m", "", "ip")["reason"], "unknown")

    def test_check_returns_live_tools(self):
        rec, token = self.mgr.create("X", ["web"])
        data = lic.read_token(token)
        self.mgr.check(data, "m", "", "ip")
        self.mgr.set_tools(rec["id"], ["web", "terminal", "gmail"])
        self.assertEqual(self.mgr.check(data, "m", "", "ip")["tools"], ["web", "terminal", "gmail"])


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.pub = lic.public_key_b64(self.key)

    def _token(self, **over):
        payload = {"id": "a1", "name": "P", "tools": ["web"], "issued_at": iso(lic._now()),
                   "expires_at": None, "max_offline_days": 30, "server_url": "", "nonce": "n"}
        payload.update(over)
        return lic.make_token(self.key, payload)

    def test_none_when_no_token(self):
        self.assertEqual(lic.evaluate({})["status"], "none")

    def test_ok_fresh_install(self):
        st = {"token": self._token(), "public_key": self.pub, "first_seen": iso(lic._now()), "last_result": {}}
        ev = lic.evaluate(st)
        self.assertEqual(ev["status"], "ok")
        self.assertEqual(ev["tools"], ["web"])

    def test_locked_bad_signature(self):
        st = {"token": self._token(), "public_key": lic.public_key_b64(Ed25519PrivateKey.generate()),
              "first_seen": iso(lic._now())}
        self.assertEqual(lic.evaluate(st)["status"], "locked")

    def test_expired(self):
        st = {"token": self._token(expires_at=iso(lic._now() - timedelta(days=1))),
              "public_key": self.pub, "first_seen": iso(lic._now())}
        self.assertEqual(lic.evaluate(st)["status"], "locked")
        self.assertIn("ended", lic.evaluate(st)["reason"])

    def test_revoked_and_machine_from_last_result(self):
        base = {"token": self._token(), "public_key": self.pub, "first_seen": iso(lic._now())}
        self.assertEqual(lic.evaluate({**base, "last_result": {"revoked": True}})["status"], "locked")
        self.assertEqual(lic.evaluate({**base, "last_result": {"reason": "machine"}})["status"], "locked")

    def test_offline_window(self):
        old = iso(lic._now() - timedelta(days=40))
        st = {"token": self._token(max_offline_days=30), "public_key": self.pub,
              "first_seen": old, "last_checkin": old, "last_result": {"valid": True, "tools": ["web"]}}
        ev = lic.evaluate(st)
        self.assertEqual(ev["status"], "locked")
        self.assertIn("hasn't reached the owner", ev["reason"])
        # a recent check-in keeps it alive and uses the server's tools
        st["last_checkin"] = iso(lic._now())
        st["last_result"] = {"valid": True, "tools": ["web", "gmail"]}
        ev2 = lic.evaluate(st)
        self.assertEqual(ev2["status"], "ok")
        self.assertEqual(ev2["tools"], ["web", "gmail"])


class EndToEndTest(unittest.TestCase):
    """Owner server + install client over 127.0.0.1."""

    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.pub = lic.public_key_b64(self.key)
        self.owner = Memory()
        self.mgr = lic.LicenseManager(self.owner.get, self.owner.put, key_fn=lambda: self.key)
        self.server = lic.LicenseServer("127.0.0.1", 0, self.mgr, self.pub)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.url = f"http://127.0.0.1:{self.server.port}"

    def test_install_checkin_settools_revoke(self):
        rec, token = self.mgr.create("Asha", ["web"])
        mem = Memory()
        client = lic.CheckinClient(mem.get, mem.put)
        with self.assertRaises(LicenseError):
            client.install(token, "bad-key", self.url)
        state = client.install(token, self.pub, self.url)
        self.assertTrue(state["configured"])
        # first check-in binds the machine and returns tools
        st = client.checkin(version="2.1.0")
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["tools"], ["web"])
        self.assertEqual(self.mgr.public()[0]["machine_id"], lic.machine_id())
        self.assertEqual(self.mgr.public()[0]["last_version"], "2.1.0")
        # owner widens tools -> next check-in reflects it
        self.mgr.set_tools(rec["id"], ["web", "terminal"])
        self.assertEqual(client.checkin()["tools"], ["web", "terminal"])
        # owner revokes -> install locks
        self.mgr.revoke(rec["id"])
        locked = client.checkin()
        self.assertEqual(locked["status"], "locked")
        self.assertIn("turned off", locked["reason"])

    def test_server_rejects_foreign_token(self):
        other = Ed25519PrivateKey.generate()
        payload = {"id": "zz", "name": "Z", "tools": "*", "issued_at": iso(lic._now()),
                   "expires_at": None, "max_offline_days": 30, "server_url": "", "nonce": "n"}
        foreign = lic.make_token(other, payload)
        body = json.dumps({"token": foreign, "machine": "m", "version": ""}).encode()
        req = urllib.request.Request(self.url + lic.API + "/check", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("should have been a 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main()
