"""Alexa: request verification, the conversation (alexa/skill.py), notifications when away, presence."""

import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

import anyio._backends._asyncio  # noqa: F401  load it now: the server's first threadpool call racing another thread's import breaks on 3.9
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import Store

from tests import test_e2e
from tests.helpers import TestBase

import db
import presence
import router
import worker
from alexa import model, notifier, skill, state, verify

SKILL = "amzn1.ask.skill.test"
ME = "amzn1.ask.account.ME"
CERT_URL = "https://s3.amazonaws.com/echo.api/echo-api-cert-test.pem"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(kind="IntentRequest", intent=None, slots=None, attrs=None, new=False, user=ME, timestamp=None):
    req = {"type": kind, "requestId": "r1", "timestamp": timestamp or _now_iso(), "locale": "en-IN"}
    if intent:
        req["intent"] = {"name": intent, "slots": {k: {"name": k, "value": v} for k, v in (slots or {}).items()}}
    return {"version": "1.0",
            "session": {"new": new, "sessionId": "s1", "application": {"applicationId": SKILL}, "attributes": attrs or {},
                        "user": {"userId": user}},
            "context": {"System": {"application": {"applicationId": SKILL}, "user": {"userId": user},
                                   "apiEndpoint": "https://api.eu.amazonalexa.com"}},
            "request": req}


# ------------------------------------------------------------------ a test CA in place of Amazon's

def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_chain(san="echo-api.amazon.com", days=30):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = (x509.CertificateBuilder().subject_name(_name("Test Root")).issuer_name(_name("Test Root"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (x509.CertificateBuilder().subject_name(_name(san)).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return ca, leaf, key


def sign(key, body: bytes) -> str:
    return base64.b64encode(key.sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()


class _Pem:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Verify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca, cls.leaf, cls.key = make_chain()

    def setUp(self):
        verify._cache.clear()
        self.addCleanup(verify._cache.clear)
        p = mock.patch.object(verify, "_roots", Store([self.ca]))
        p.start()
        self.addCleanup(p.stop)
        pem = self.leaf.public_bytes(serialization.Encoding.PEM)
        self.fetches = []

        def fake_urlopen(url, timeout=None):
            self.fetches.append(url)
            return _Pem(pem)
        p = mock.patch.object(verify.urllib.request, "urlopen", fake_urlopen)
        p.start()
        self.addCleanup(p.stop)

    def test_cert_url_rules_match_amazons_examples(self):
        good = ["https://s3.amazonaws.com/echo.api/echo-api-cert.pem", "https://s3.amazonaws.com:443/echo.api/echo-api-cert.pem",
                "https://s3.amazonaws.com/echo.api/../echo.api/echo-api-cert.pem", "HTTPS://S3.AMAZONAWS.COM/echo.api/x.pem"]
        bad = ["http://s3.amazonaws.com/echo.api/echo-api-cert.pem", "https://notamazon.com/echo.api/echo-api-cert.pem",
               "https://s3.amazonaws.com/EcHo.aPi/echo-api-cert.pem", "https://s3.amazonaws.com/invalid.path/echo-api-cert.pem",
               "https://s3.amazonaws.com:563/echo.api/echo-api-cert.pem", "https://s3.amazonaws.com/echo.api/../x/cert.pem", ""]
        self.assertTrue(all(verify.valid_cert_url(u) for u in good))
        self.assertFalse(any(verify.valid_cert_url(u) for u in bad))

    def test_a_correct_signature_passes_and_the_chain_is_cached(self):
        body = b'{"hello": "alexa"}'
        verify.check_signature(body, CERT_URL, sign(self.key, body))
        verify.check_signature(body, CERT_URL, sign(self.key, body))
        self.assertEqual(self.fetches, [CERT_URL])

    def test_tampered_body_wrong_key_bad_url_and_missing_headers_fail(self):
        body = b'{"hello": "alexa"}'
        with self.assertRaises(verify.VerifyError):
            verify.check_signature(b'{"hello": "mallory"}', CERT_URL, sign(self.key, body))
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(verify.VerifyError):
            verify.check_signature(body, CERT_URL, sign(other, body))
        with self.assertRaises(verify.VerifyError):
            verify.check_signature(body, "https://evil.example.com/echo.api/cert.pem", sign(self.key, body))
        with self.assertRaises(verify.VerifyError):
            verify.check_signature(body, "", "")

    def test_a_certificate_for_another_domain_is_refused(self):
        ca, leaf, key = make_chain(san="example.com")
        with mock.patch.object(verify, "_roots", Store([ca])), self.assertRaises(verify.VerifyError):
            verify._check_chain([leaf])

    def test_a_certificate_from_an_untrusted_root_is_refused(self):
        _, leaf, _ = make_chain()
        with self.assertRaises(verify.VerifyError):
            verify._check_chain([leaf])

    def test_timestamp_and_application(self):
        verify.check_timestamp(envelope())
        old = (datetime.now(timezone.utc) - timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaises(verify.VerifyError):
            verify.check_timestamp(envelope(timestamp=old))
        with self.assertRaises(verify.VerifyError):
            verify.check_timestamp(envelope(timestamp="yesterday"))
        verify.check_application(envelope(), SKILL)
        for skill_id in ("amzn1.ask.skill.other", ""):
            with self.assertRaises(verify.VerifyError):
                verify.check_application(envelope(), skill_id)


# ------------------------------------------------------------------ the conversation

class Conversation(TestBase):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("KodeAura7", tempfile.mkdtemp())
        self.site = db.add_project("Tushar Site", tempfile.mkdtemp())
        for p in (mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test", "ALEXA_SKILL_ID": SKILL}),
                  mock.patch.object(router, "_llm_classify", lambda *a, **k: None),
                  mock.patch.object(skill, "REPLY_SECONDS", 1.5)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("ALEXA_USER_ID", None)
        os.environ.pop("ALEXA_CLIENT_ID", None)
        presence.reset()
        self.attrs = {}

    def say(self, intent=None, kind="IntentRequest", new=False, user=ME, **slots):
        out = skill.handle(envelope(kind, intent, slots, dict(self.attrs), new=new, user=user))
        self.attrs = out.get("sessionAttributes") or {}
        self.last = out
        return (out.get("response", {}).get("outputSpeech") or {}).get("text", "")

    def answer_next_api_task(self, text, status="done"):
        """Stand in for the api lane: finish the next queued api task with `text`."""
        def run():
            deadline = time.time() + 5
            while time.time() < deadline:
                q = [t for t in db.list_tasks(statuses=["queued"]) if t["lane"] == "api"]
                if q:
                    db.mark_running(q[0]["id"])
                    db.mark_done(q[0]["id"], text)
                    return
                time.sleep(0.03)
        threading.Thread(target=run, daemon=True).start()

    def approval(self, command="git push origin main", project=None):
        task = db.add_task((project or self.proj)["id"], "Ship the footer fix", 0, None)
        return db.create_approval(task["id"], "Bash", json.dumps({"command": command}))

    # ---- opening, pairing
    def test_open_pairs_the_first_account_and_refuses_others(self):
        spoken = self.say(kind="LaunchRequest", new=True)
        self.assertIn("Octopus here", spoken)
        self.assertIn("Nothing is running", spoken)
        st = state.get()
        self.assertEqual((st["user_id"], st["api_endpoint"], st["locale"]), (ME, "https://api.eu.amazonalexa.com", "en-IN"))
        spoken = self.say(kind="LaunchRequest", new=True, user="amzn1.ask.account.SOMEONE_ELSE")
        self.assertIn("different Amazon account", spoken)
        self.assertTrue(self.last["response"]["shouldEndSession"])
        self.assertEqual(state.get()["user_id"], ME)

    def test_a_pinned_account_wins_over_pairing(self):
        with mock.patch.dict(os.environ, {"ALEXA_USER_ID": "amzn1.ask.account.PINNED"}):
            self.assertIn("different Amazon account", self.say(kind="LaunchRequest", new=True))
            self.assertIn("Octopus here", self.say(kind="LaunchRequest", new=True, user="amzn1.ask.account.PINNED"))

    # ---- status
    def test_queue_and_working_on(self):
        running = db.add_task(self.proj["id"], "Refactor the checkout page", 0, None)
        db.mark_running(running["id"])
        db.add_task(self.site["id"], "Fix the footer links", 0, None)
        db.add_task(self.proj["id"], "Write release notes", 0, None)
        spoken = self.say("QueueIntent")
        self.assertIn("Running now: 'Refactor the checkout page' in KodeAura7", spoken)
        self.assertIn("2 waiting in the queue. Next up: 'Fix the footer links' in Tushar Site, then 'Write release notes' in KodeAura7", spoken)
        spoken = self.say("WorkingOnIntent")
        self.assertIn("Octopus is working on 'Refactor the checkout page' in KodeAura7, with Claude Code", spoken)

    # ---- approvals
    def test_open_reads_a_waiting_approval_and_yes_approves_it(self):
        a = self.approval()
        spoken = self.say(kind="LaunchRequest", new=True)
        self.assertIn("1 thing waiting on you", spoken)
        self.assertIn("KodeAura7, working on 'Ship the footer fix', wants to run: git push origin main. Approve or deny?", spoken)
        self.assertEqual(self.last["response"]["card"]["title"], "Approval needed")
        spoken = self.say("AMAZON.YesIntent")
        self.assertEqual(db.get_approval(a["id"])["status"], "approved")
        self.assertIn("Approved. Octopus is carrying on. Nothing else is waiting on you.", spoken)

    def test_deny_skip_and_the_next_one(self):
        a1 = self.approval("rm -rf build")
        a2 = self.approval("npm publish", self.site)
        self.say("ApprovalsIntent")
        self.assertEqual(self.attrs["approval_id"], a1["id"])
        spoken = self.say("SkipIntent")
        self.assertIn("Tushar Site", spoken)
        self.assertEqual(self.attrs["approval_id"], a2["id"])
        spoken = self.say("DenyIntent")
        self.assertEqual(db.get_approval(a2["id"])["status"], "denied")
        self.assertIn("Denied by voice", db.get_approval(a2["id"])["note"])
        self.assertEqual(db.get_approval(a1["id"])["status"], "pending")         # skipped, still waiting in The Den
        self.assertIn("Nothing else is waiting", spoken)

    def test_the_approval_code_is_needed_when_set(self):
        state.set_prefs({"approval_pin": "4321"})
        a = self.approval()
        self.say("ApprovalsIntent")
        self.assertIn("approval code", self.say("AMAZON.YesIntent"))
        self.assertIn("doesn't match", self.say("PinIntent", code="1111"))
        self.assertEqual(db.get_approval(a["id"])["status"], "pending")
        self.say("PinIntent", code="4321")
        self.assertEqual(db.get_approval(a["id"])["status"], "approved")

    def test_a_spoken_code_in_free_speech_counts(self):
        state.set_prefs({"approval_pin": "4321"})
        a = self.approval()
        self.say("ApprovalsIntent")
        self.say("AMAZON.YesIntent")
        self.say("ChatIntent", utterance="four three two one")
        self.assertEqual(db.get_approval(a["id"])["status"], "approved")

    def test_high_risk_commands_are_called_out(self):
        self.approval("git push --force origin main")
        self.assertIn("high risk", self.say("ApprovalsIntent"))

    # ---- add a task
    def test_add_a_task_reads_it_back_and_queues_it_for_claude(self):
        self.assertIn("Which project", self.say("AddTaskIntent"))
        self.assertIn("What should it do", self.say("ChatIntent", utterance="code aura seven"))
        spoken = self.say("ChatIntent", utterance="fix the broken footer links on the pricing page")
        self.assertIn("I heard: 'fix the broken footer links on the pricing page'. Should I queue that in KodeAura7?", spoken)
        spoken = self.say("AMAZON.YesIntent")
        task = db.list_tasks(statuses=["queued"])[-1]
        self.assertEqual((task["prompt"], task["project_id"], task["lane"], task["agent_id"], task["source"]),
                         ("fix the broken footer links on the pricing page", self.proj["id"], "agent", "coding", "alexa"))
        self.assertIn("Queued in KodeAura7 for Claude Code", spoken)
        self.assertIn(task["id"], state.get()["watch"])

    def test_no_lets_you_say_it_again(self):
        self.say("AddTaskIntent", project="KodeAura7")
        self.say("ChatIntent", utterance="make the header red")
        self.assertIn("Tell me again", self.say("AMAZON.NoIntent"))
        self.say("ChatIntent", utterance="make the header blue")
        self.say("AMAZON.YesIntent")
        self.assertEqual([t["prompt"] for t in db.list_tasks(statuses=["queued"])], ["make the header blue"])

    # ---- push
    def test_push_uses_the_project_claude_last_worked_in_and_asks_first(self):
        last = db.add_task(self.site["id"], "Update the blog", 0, None)
        db.mark_running(last["id"])
        db.mark_done(last["id"], "Done")
        spoken = self.say("PushIntent")
        self.assertIn("Push the current code in Tushar Site to GitHub?", spoken)
        self.assertEqual(db.list_tasks(statuses=["queued"]), [])
        self.say("AMAZON.YesIntent")
        task = db.list_tasks(statuses=["queued"])[0]
        self.assertEqual((task["project_id"], task["lane"], task["prompt"]), (self.site["id"], "agent", skill.PUSH_PROMPT))

    def test_push_asks_which_project_when_it_cant_tell(self):
        self.assertIn("Which project should I push", self.say("PushIntent"))
        self.assertIn("KodeAura7", self.say("ChatIntent", utterance="kode aura 7"))
        self.say("AMAZON.YesIntent")
        self.assertEqual(db.list_tasks(statuses=["queued"])[0]["project_id"], self.proj["id"])

    # ---- discussion
    def test_a_discussion_is_one_chat_and_queue_it_hands_over_the_transcript(self):
        self.assertIn("let's talk it through for KodeAura7", self.say("DiscussIntent", project="KodeAura7"))
        self.answer_next_api_task("Nice. Should it support Google sign in, or email only?")
        spoken = self.say("ChatIntent", utterance="i want a login page for the admin area")
        self.assertIn("Should it support Google sign in", spoken)
        first = db.get_task(self.attrs["thread_id"])
        self.assertEqual((first["agent_id"], first["lane"], first["project_id"], first["source"]), ("voice", "api", self.proj["id"], "alexa"))

        self.answer_next_api_task("Got it, Google only. Say queue it when you're ready.")
        spoken = self.say("ChatIntent", utterance="google only and remember me for thirty days")
        self.assertIn("Google only", spoken)
        self.assertEqual(len(db.get_thread(first["id"])), 2)          # same chat, not a new one

        self.assertIn("with everything we just discussed", self.say("QueueItIntent"))
        self.say("AMAZON.YesIntent")
        job = [t for t in db.list_tasks(statuses=["queued"]) if t["lane"] == "agent"][0]
        self.assertEqual((job["project_id"], job["agent_id"]), (self.proj["id"], "coding"))
        self.assertIn("Tushar: i want a login page for the admin area", job["prompt"])
        self.assertIn("Octopus: Got it, Google only.", job["prompt"])

    def test_a_slow_answer_is_held_for_go_on(self):
        with mock.patch.object(skill, "REPLY_SECONDS", 0.3):
            self.say("DiscussIntent")
            spoken = self.say("ChatIntent", utterance="what should the api look like")
        self.assertIn("Still thinking", spoken)
        tid = self.attrs["pending_task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "A small REST API with three endpoints.")
        self.assertIn("A small REST API", self.say("ContinueIntent"))

    def test_a_held_answer_is_said_before_the_next_thing(self):
        with mock.patch.object(skill, "REPLY_SECONDS", 0.3):
            self.say("DiscussIntent")
            self.say("ChatIntent", utterance="how long will it take")
        tid = self.attrs["pending_task_id"]
        db.mark_running(tid)
        db.mark_done(tid, "About a day.")
        self.assertTrue(self.say("QueueIntent").startswith("About your earlier question: About a day."))

    def test_an_approval_arriving_mid_discussion_is_mentioned_once_and_the_chat_resumes(self):
        self.say("DiscussIntent", project="KodeAura7")
        self.approval()
        self.answer_next_api_task("Sure.")
        spoken = self.say("ChatIntent", utterance="the admin page needs a search box")
        self.assertTrue(spoken.startswith("Heads up: KodeAura7 needs your OK."))
        self.answer_next_api_task("Okay.")
        self.assertNotIn("Heads up", self.say("ChatIntent", utterance="and a filter by date"))
        self.say("ApprovalsIntent")
        self.assertIn("Back to our conversation", self.say("AMAZON.YesIntent"))
        self.assertEqual(self.attrs["mode"], "discuss")

    def test_a_new_session_soon_after_picks_the_discussion_back_up(self):
        self.say("DiscussIntent", project="KodeAura7")
        self.answer_next_api_task("Sounds good.")
        self.say("ChatIntent", utterance="it should be a dark theme")
        thread = self.attrs["thread_id"]
        self.attrs = {}
        self.assertIn("We were talking about", self.say(kind="LaunchRequest", new=True))
        self.answer_next_api_task("And the accent colour?")
        self.say("ChatIntent", utterance="use purple")
        self.assertEqual(self.attrs["thread_id"], thread)
        self.assertEqual(len(db.get_thread(thread)), 2)

    # ---- anything else
    def test_instant_commands_answer_at_once(self):
        self.assertIn("Nothing is running right now", self.say("ChatIntent", utterance="what's happening"))

    def test_free_speech_that_would_start_claude_code_asks_first(self):
        spoken = self.say("ChatIntent", utterance="fix the login bug in KodeAura7")
        self.assertIn("That's a job for Claude Code in KodeAura7", spoken)
        self.assertEqual(db.list_tasks(statuses=["queued"]), [])
        self.say("AMAZON.YesIntent")
        task = db.list_tasks(statuses=["queued"])[0]
        self.assertEqual((task["lane"], task["project_id"], task["source"]), ("agent", self.proj["id"], "alexa"))

    def test_a_question_is_answered_in_the_same_turn(self):
        self.answer_next_api_task("Paris is the capital of France.")
        self.assertIn("Paris", self.say("ChatIntent", utterance="what is the capital of france"))

    def test_a_follow_up_to_a_claude_task_is_read_back_first(self):
        self.say("ChatIntent", utterance="fix the login bug in KodeAura7")
        self.say("AMAZON.YesIntent")
        root = db.list_tasks(statuses=["queued"])[0]
        self.assertIn("Add that to", self.say("ChatIntent", utterance="and also add tests for it"))
        self.say("AMAZON.YesIntent")
        replies = [t for t in db.get_thread(root["id"]) if t["id"] != root["id"]]
        self.assertEqual([t["prompt"] for t in replies], ["and also add tests for it"])

    def test_stop_the_current_task_confirms(self):
        t = db.add_task(self.proj["id"], "Long refactor", 0, None)
        db.mark_running(t["id"])
        self.assertIn("Stop 'Long refactor' in KodeAura7?", self.say("StopTaskIntent"))
        with mock.patch.object(worker, "stop_task", return_value=True) as stop:
            self.assertIn("Stopped 'Long refactor'", self.say("AMAZON.YesIntent"))
        stop.assert_called_once_with(t["id"])

    def test_finished_tasks_from_alexa_are_reported_once(self):
        self.say("AddTaskIntent", project="KodeAura7")
        self.say("ChatIntent", utterance="update the readme")
        self.say("AMAZON.YesIntent")
        task = db.list_tasks(statuses=["queued"])[0]
        db.mark_running(task["id"])
        db.mark_done(task["id"], "Updated the README with setup steps.")
        self.attrs = {}
        spoken = self.say(kind="LaunchRequest", new=True)
        self.assertIn("While you were away: 'Update the readme' in KodeAura7 finished: Updated the README with setup steps.", spoken)
        self.assertNotIn("While you were away", self.say(kind="LaunchRequest", new=True))

    def test_session_end_and_skill_events(self):
        self.say(kind="LaunchRequest", new=True)
        self.assertEqual(skill.handle(envelope("SessionEndedRequest")), {"version": "1.0", "response": {}})
        ev = envelope("AlexaSkillEvent.ProactiveSubscriptionChanged")
        ev["request"]["body"] = {"subscriptions": [{"eventName": "AMAZON.MessageAlert.Activated"}]}
        skill.handle(ev)
        self.assertTrue(state.get()["subscribed"])
        ev["request"]["body"] = {}
        skill.handle(ev)
        self.assertFalse(state.get()["subscribed"])

    def test_project_matching(self):
        self.assertEqual(skill.match_project("code aura seven")["id"], self.proj["id"])
        self.assertEqual(skill.match_project("tushar's site")["id"], self.site["id"])
        self.assertEqual(skill.match_project("anything", resolved_id=str(self.site["id"]))["id"], self.site["id"])
        self.assertIsNone(skill.match_project("banana bread"))
        self.assertTrue(db.is_general_project(skill.match_project("general")))


# ------------------------------------------------------------------ notifications

class Notifications(TestBase):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("KodeAura7", tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"ALEXA_CLIENT_ID": "cid", "ALEXA_CLIENT_SECRET": "secret", "ALEXA_SKILL_ID": SKILL})
        p.start()
        self.addCleanup(p.stop)
        notifier._token = ("", 0.0)
        presence.reset()
        state.update(lambda s: s.update(user_id=ME, api_endpoint="https://api.eu.amazonalexa.com", locale="en-IN"))
        self.calls = []

        def fake_post(url, data, headers):
            self.calls.append((url, data, headers))
            if url == notifier.TOKEN_URL:
                return 200, json.dumps({"access_token": "tok", "expires_in": 3600})
            return 202, ""
        p = mock.patch.object(notifier, "_post", fake_post)
        p.start()
        self.addCleanup(p.stop)

    def approval(self):
        task = db.add_task(self.proj["id"], "Ship it", 0, None)
        return db.create_approval(task["id"], "Bash", json.dumps({"command": "git push"}))

    def events(self):
        return [json.loads(d) for u, d, h in self.calls if "proactiveEvents" in u]

    def test_an_approval_while_away_notifies_once(self):
        self.approval()
        self.assertEqual(notifier.check_once(), {"sent": True, "count": 1})
        (url, _, headers), = [c for c in self.calls if "proactiveEvents" in c[0]]
        self.assertEqual(url, "https://api.eu.amazonalexa.com/v1/proactiveEvents/stages/development")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        ev = self.events()[0]
        self.assertEqual(ev["event"]["name"], "AMAZON.MessageAlert.Activated")
        self.assertEqual(ev["event"]["payload"]["messageGroup"], {"creator": {"name": "Octopus"}, "count": 1})
        self.assertEqual(ev["relevantAudience"], {"type": "Unicast", "payload": {"user": ME}})
        self.assertNotIn("git push", json.dumps(ev))                    # details never go to Amazon
        self.assertIsNone(notifier.check_once())
        self.approval()
        self.assertEqual(notifier.check_once(), {"sent": True, "count": 2})
        self.assertEqual(len([c for c in self.calls if c[0] == notifier.TOKEN_URL]), 1)   # token reused

    def test_not_while_using_the_den_or_talking_to_alexa_or_asleep(self):
        self.approval()
        presence.seen("iPhone")
        self.assertIsNone(notifier.check_once())
        presence.reset()
        state.update(lambda s: s.update(last_request_at=state.now_iso()))
        self.assertIsNone(notifier.check_once())
        state.update(lambda s: s.pop("last_request_at"))
        db.start_sleep_mode(1)
        self.assertIsNone(notifier.check_once())
        db.end_sleep_mode()
        self.assertEqual(notifier.check_once()["sent"], True)            # away now: it goes out

    def test_switched_off_or_not_set_up(self):
        self.approval()
        state.set_prefs({"notify_approvals": False})
        self.assertIsNone(notifier.check_once())
        state.set_prefs({"notify_approvals": True})
        state.update(lambda s: s.update(subscribed=False))
        self.assertFalse(notifier.configured()[0])
        self.assertIsNone(notifier.check_once())
        state.update(lambda s: s.update(subscribed=True))
        with mock.patch.dict(os.environ, {"ALEXA_CLIENT_SECRET": ""}):
            self.assertIn("ALEXA_CLIENT_ID", notifier.configured()[1])
        self.assertEqual(self.events(), [])

    def test_project_notification_setting_is_respected(self):
        db.update_project_settings(self.proj["id"], notify_approvals=False)
        self.approval()
        self.assertIsNone(notifier.check_once())

    def test_a_failure_backs_off(self):
        self.approval()

        def refused(url, data, headers):
            if url == notifier.TOKEN_URL:
                return 200, json.dumps({"access_token": "tok", "expires_in": 3600})
            return 403, "forbidden"
        with mock.patch.object(notifier, "_post", refused):
            out = notifier.check_once()
        self.assertFalse(out["sent"])
        self.assertIn("allow notifications", out["error"])
        self.assertIsNone(notifier.check_once())                         # waits before trying again
        self.assertFalse(state.get()["last_notify"]["ok"])

    def test_finished_alexa_tasks_notify_too(self):
        task = db.add_task(self.proj["id"], "Update the readme", 0, None, source="alexa")
        state.watch_task(task["id"])
        self.assertIsNone(notifier.check_once())
        db.mark_running(task["id"])
        db.mark_done(task["id"], "Done.")
        self.assertEqual(notifier.check_once(), {"sent": True, "count": 1})
        self.assertIsNone(notifier.check_once())


# ------------------------------------------------------------------ over HTTP

class AlexaHttp(test_e2e.E2E):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"ALEXA_SKILL_ID": SKILL})
        p.start()
        self.addCleanup(p.stop)
        presence.reset()

    def post_alexa(self, body: bytes, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/alexa", data=body, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_unsigned_requests_are_refused_without_needing_the_api_key(self):
        status, _ = self.post_alexa(json.dumps(envelope("LaunchRequest")).encode())
        self.assertEqual(status, 400)
        self.assertEqual(state.get(), {})                                # nothing was handled

    def test_not_set_up_is_a_503(self):
        with mock.patch.dict(os.environ, {"ALEXA_SKILL_ID": ""}):
            self.assertEqual(self.post_alexa(b"{}")[0], 503)

    def test_a_verified_request_is_answered(self):
        with mock.patch.object(verify, "check_signature", lambda *a: None):
            status, out = self.post_alexa(json.dumps(envelope("LaunchRequest", new=True)).encode(),
                                          {"SignatureCertChainUrl": CERT_URL, "Signature-256": "x"})
        self.assertEqual(status, 200)
        self.assertIn("Octopus here", out["response"]["outputSpeech"]["text"])

    def test_a_request_for_another_skill_is_refused(self):
        env = envelope("LaunchRequest")
        env["context"]["System"]["application"]["applicationId"] = "amzn1.ask.skill.other"
        with mock.patch.object(verify, "check_signature", lambda *a: None):
            self.assertEqual(self.post_alexa(json.dumps(env).encode())[0], 400)

    def test_the_admin_endpoints_need_the_key_and_work(self):
        for method, path in (("GET", "/alexa/status"), ("PATCH", "/alexa/prefs"), ("POST", "/alexa/test-notification"),
                             ("DELETE", "/alexa/link"), ("POST", "/presence")):
            self.assertEqual(self.http(method, path, {}, key="wrong")["_status"], 401, path)
        import api
        api._failures.clear()
        self.assertFalse(self.http("GET", "/alexa/status")["presence"]["present"])
        self.http("POST", "/presence", {"device": "iPhone"})
        self.assertTrue(self.http("GET", "/alexa/status")["presence"]["present"])
        self.assertEqual(self.http("PATCH", "/alexa/prefs", {"approval_pin": "12"})["_status"], 400)
        out = self.http("PATCH", "/alexa/prefs", {"approval_pin": "4321", "notify_results": False})
        self.assertEqual(out["prefs"], {"notify_approvals": True, "notify_results": False, "approval_pin_set": True})
        self.assertEqual(self.http("POST", "/alexa/test-notification")["_status"], 409)   # no client id / secret here

    def test_from_the_internet_only_post_alexa_is_reachable(self):
        funnel = {"Tailscale-Funnel-Request": "?1", "X-API-Key": "wrong"}
        for method, path in (("GET", "/alexa/status"), ("DELETE", "/alexa/link"), ("GET", "/queue/status"),
                             ("GET", "/dashboard"), ("GET", "/alexa"), ("POST", "/alexa/")):
            for _ in range(6):                                           # 12+ wrong keys per path, were it checked
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=funnel)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(req, timeout=15)
                self.assertEqual(e.exception.code, 404, path)
        self.assertIn("presence", self.http("GET", "/alexa/status"))      # this Mac isn't locked out
        self.assertEqual(self.post_alexa(b"{}", {"Tailscale-Funnel-Request": "?1"})[0], 400)   # reaches verification


class Model(unittest.TestCase):
    def test_every_sample_belongs_to_one_intent_and_the_slots_exist(self):
        m = model.build(projects=[])["interactionModel"]["languageModel"]
        samples = [s for i in m["intents"] for s in i["samples"]]
        self.assertEqual(len(samples), len(set(samples)))
        for i in m["intents"]:
            slots = {s["name"] for s in i.get("slots", [])}
            for s in i["samples"]:
                for used in __import__("re").findall(r"\{(\w+)\}", s):
                    self.assertIn(used, slots, f"{i['name']}: {s}")
        self.assertEqual(model.spoken_forms("KodeAura7"), ["code aura seven", "kode aura 7", "kode aura seven"])

    def test_every_intent_in_the_model_has_a_handler(self):
        m = model.build(projects=[])["interactionModel"]["languageModel"]
        self.assertEqual({i["name"] for i in m["intents"]} - set(skill.INTENTS), set())


class Wake(unittest.TestCase):
    def test_waking_the_api_lane_is_harmless_when_it_isnt_running(self):
        with mock.patch.object(worker, "_api_lane_wake", None):
            worker.wake_api_lane()


if __name__ == "__main__":
    unittest.main()
