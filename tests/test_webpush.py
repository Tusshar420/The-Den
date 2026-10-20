"""Web Push to the Dock / Home Screen web app: encryption, VAPID, subscriptions, sending."""

import io
import json
import os
import urllib.error
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from tests.helpers import TestBase

import api
import approval_links
import db
import webpush
import worker

# RFC 8291 section 5: the worked example, end to end.
RFC = {
    "plaintext": b"When I grow up, I want to be a watermelon",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "auth": "BTBZMqHH6r4Tts7J_aSIgg",
    "body": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
}

ENDPOINT = "https://web.push.apple.com/QGuQyavXutnMH6X7T8f"


def a_browser():
    """A real-looking subscription: the browser's key pair and auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key, {"endpoint": ENDPOINT, "keys": {"p256dh": webpush.b64url(webpush._raw_public(key.public_key())),
                                                "auth": webpush.b64url(os.urandom(16))}}


class Encryption(TestBase):
    def test_rfc8291_example(self):
        server = ec.derive_private_key(int.from_bytes(webpush.unb64url(RFC["as_private"]), "big"), ec.SECP256R1())
        out = webpush.encrypt(RFC["ua_public"], RFC["auth"], RFC["plaintext"],
                              salt=webpush.unb64url(RFC["salt"]), server_key=server)
        self.assertEqual(webpush.b64url(out), RFC["body"])

    def test_fresh_salt_and_key_each_time(self):
        _, sub = a_browser()
        one = webpush.encrypt(sub["keys"]["p256dh"], sub["keys"]["auth"], b"hi")
        two = webpush.encrypt(sub["keys"]["p256dh"], sub["keys"]["auth"], b"hi")
        self.assertNotEqual(one[:16], two[:16])
        self.assertEqual(one[16:21], b"\x00\x00\x10\x00\x41")   # rs 4096, then a 65-byte key


class Vapid(TestBase):
    def test_key_is_made_once_and_kept(self):
        first = webpush.public_key()
        self.assertEqual(webpush.public_key(), first)
        self.assertEqual(len(webpush.unb64url(first)), 65)

    def test_header_is_a_valid_es256_jwt(self):
        key = webpush.vapid_key()
        with mock.patch.dict(os.environ, {"VAPID_SUBJECT": ""}):
            db.set_setting(webpush.ORIGIN_SETTING, "https://mac.tail.ts.net")
            header = webpush.vapid_header(ENDPOINT + "?x=1", key, now=1000)
        t, k = header[len("vapid t="):].split(", k=")
        self.assertEqual(k, webpush.public_key())
        head, claims, sig = t.split(".")
        self.assertEqual(json.loads(webpush.unb64url(head)), {"typ": "JWT", "alg": "ES256"})
        self.assertEqual(json.loads(webpush.unb64url(claims)),
                         {"aud": "https://web.push.apple.com", "exp": 1000 + 12 * 3600, "sub": "https://mac.tail.ts.net"})
        raw = webpush.unb64url(sig)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))   # raises if wrong

    def test_subject_falls_back_to_mailto(self):
        with mock.patch.dict(os.environ, {"VAPID_SUBJECT": ""}):
            self.assertTrue(webpush.subject().startswith("mailto:"))
            db.set_setting(webpush.ORIGIN_SETTING, "http://100.1.2.3:8787")   # not https: Apple would refuse it
            self.assertTrue(webpush.subject().startswith("mailto:"))


class Sending(TestBase):
    def setUp(self):
        super().setUp()
        self.browser, sub = a_browser()
        db.add_push_subscription(sub["endpoint"], sub["keys"]["p256dh"], sub["keys"]["auth"], "Safari")
        patcher = mock.patch.dict(os.environ, {"API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def decrypt(self, body: bytes) -> dict:
        """What the browser does with it (RFC 8291 in reverse) - proves the push is readable."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        sub = db.list_push_subscriptions()[0]
        salt, as_public = body[:16], body[21:86]
        ua_public = webpush._raw_public(self.browser.public_key())
        ecdh = self.browser.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
        ikm = webpush._hmac(webpush._hmac(webpush.unb64url(sub["auth"]), ecdh),
                            b"WebPush: info\x00" + ua_public + as_public + b"\x01")
        prk = webpush._hmac(salt, ikm)
        plain = AESGCM(webpush._hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
            webpush._hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12], body[86:], None)
        return json.loads(plain.rstrip(b"\x02"))

    def test_approval_push(self):
        task = db.add_task(self.project["id"], "ship it", 0)
        approval = db.create_approval(task["id"], "Bash", json.dumps({"command": "git push"}))
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(webpush.send_all("Approval needed", "General: git push", "approval", "/dashboard", approval["id"]), 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, ENDPOINT)
        self.assertEqual(req.get_header("Content-encoding"), "aes128gcm")
        self.assertEqual(req.get_header("Urgency"), "high")
        self.assertTrue(req.get_header("Authorization").startswith("vapid t="))
        data = self.decrypt(req.data)
        self.assertEqual(data["title"], "Approval needed")
        self.assertEqual(data["kind"], "approval")
        self.assertEqual(data["pending"], 1)
        self.assertEqual(data["tag"], f"approval-{approval['id']}")
        self.assertEqual(data["approveUrl"], approval_links.decide_path(approval["id"], "approve"))
        self.assertTrue(data["denyUrl"].startswith(f"/approvals/{approval['id']}/quick/deny?t="))
        # the link the service worker fetches really decides it
        t = data["approveUrl"].split("t=")[1]
        api.quick_decide(approval["id"], "approve", t)
        self.assertEqual(db.get_approval(approval["id"])["status"], "approved")

    def test_gone_subscription_is_dropped(self):
        gone = urllib.error.HTTPError(ENDPOINT, 410, "Gone", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=gone):
            self.assertEqual(webpush.send_all("Done · General", "ok"), 0)
        self.assertEqual(db.list_push_subscriptions(), [])

    def test_other_errors_keep_it_and_never_raise(self):
        err = urllib.error.HTTPError(ENDPOINT, 403, "Forbidden", {}, io.BytesIO(b"BadJwtToken"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(webpush.send_all("Done · General", "ok"), 0)
        self.assertEqual(len(db.list_push_subscriptions()), 1)

    def test_worker_pushes_to_browsers_without_activitysmith(self):
        with mock.patch.object(worker, "ACTIVITYSMITH_API_KEY", None), mock.patch("urllib.request.urlopen") as urlopen:
            self.assertTrue(worker.notify_phone("Reminder", "call back", "reminder", open_hash="#/briefing"))
        self.assertEqual(self.decrypt(urlopen.call_args[0][0].data)["url"], "/dashboard#/briefing")

    def test_nothing_subscribed_sends_nothing(self):
        db.delete_push_subscription(ENDPOINT)
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(webpush.send_all("x", "y"), 0)
        urlopen.assert_not_called()


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


class Endpoints(TestBase):
    def subscribe(self, sub, origin=None):
        return api.push_subscribe(api.PushSubscriptionIn(**sub), FakeRequest({"Origin": origin} if origin else {}))

    def test_subscribe_and_unsubscribe(self):
        _, sub = a_browser()
        self.assertEqual(len(webpush.unb64url(api.push_key()["publicKey"])), 65)
        self.assertEqual(self.subscribe(sub, "https://mac.tail.ts.net"), {"subscribed": True, "count": 1})
        self.subscribe(sub)                                                  # again: still one
        self.assertEqual(len(db.list_push_subscriptions()), 1)
        self.assertEqual(db.get_setting(webpush.ORIGIN_SETTING), "https://mac.tail.ts.net")
        self.assertEqual(api.push_unsubscribe(api.PushSubscriptionIn(**sub))["count"], 0)

    def test_bad_subscriptions_are_refused(self):
        from fastapi import HTTPException
        from pydantic import ValidationError
        _, sub = a_browser()
        with self.assertRaises(ValidationError):
            api.PushSubscriptionIn(**{**sub, "endpoint": "http://evil/x"})
        with self.assertRaises(HTTPException) as e:
            self.subscribe({"endpoint": ENDPOINT})
        self.assertEqual(e.exception.status_code, 400)

    def test_routes_need_the_key_except_the_service_worker(self):
        deps = {r.path + " " + ",".join(sorted(r.methods)): [d.call for d in r.dependant.dependencies]
                for r in api.app.routes if getattr(r, "path", "") in ("/push/key", "/push/subscribe", "/sw.js")}
        for route, calls in deps.items():
            self.assertEqual(api.require_api_key in calls, not route.startswith("/sw.js"), route)
        self.assertEqual(len(deps), 4)

    def test_service_worker_is_served_from_the_root(self):
        r = api.service_worker()
        self.assertEqual(r.headers["service-worker-allowed"], "/")
        self.assertIn("javascript", r.media_type)
        self.assertIn("showNotification", Path(r.path).read_text())
