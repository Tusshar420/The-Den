"""modules/approval_reminder: one email per approval left unanswered, with the project in the
subject, the task and queue in the body and Approve / Deny links; follows the email switch."""

import importlib.util
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import db
import email_templates
from tests.helpers import ROOT, TestBase

_spec = importlib.util.spec_from_file_location("approval_reminder_module", ROOT / "modules/approval_reminder/module.py")
reminder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reminder)


class FakeCtx:
    def __init__(self, minutes=10):
        self.config, self.state, self.cleanups = {"minutes": minutes}, {}, []
        self.log = logging.getLogger("test.approval_reminder")

    def get_state(self, key, default=None):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = list(value)

    def add_cleanup(self, fn):
        self.cleanups.append(fn)


class ApprovalReminder(TestBase):
    def setUp(self):
        super().setUp()
        self.connect_google()
        self.ctx = FakeCtx()
        self.mod = reminder.Module()
        self.mod.initialize(self.ctx)
        self.addCleanup(lambda: [fn() for fn in self.ctx.cleanups])
        self.task = db.add_task(self.project["id"], "Deploy the site")
        db.mark_running(self.task["id"])
        self.approval = db.create_approval(self.task["id"], "Bash", json.dumps({"command": "git push origin main"}))

    def check(self, minutes):
        later = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        with mock.patch.object(self.mod, "send", return_value=True) as send:
            n = self.mod.check(later)
        return n, send

    def test_one_email_after_the_wait(self):
        self.assertEqual(self.check(5)[0], 0)
        n, send = self.check(11)
        self.assertEqual(n, 1)
        subject, plain, _ = send.call_args.args[0]
        self.assertEqual(subject, "Still waiting for your OK · General: Deploy the site")
        self.assertIn("Wants to: git push origin main", plain)
        self.assertIn(f"Task: #{self.task['id']} Deploy the site", plain)
        self.assertIn("paused until you answer", plain)
        self.assertEqual(self.check(30)[0], 0)          # only ever one per approval

    def test_skips_decided_and_cancelled(self):
        db.decide_approval(self.approval["id"], True)
        db.decide_approval(db.create_approval(self.task["id"], "Bash", "{}")["id"], False)
        db.timeout_approval(db.create_approval(self.task["id"], "Bash", "{}")["id"])
        other = db.add_task(self.project["id"], "Later")
        db.create_approval(other["id"], "Bash", "{}")
        db.cancel_task(other["id"])
        self.assertEqual(self.check(60)[0], 0)

    def test_minutes_and_email_switch(self):
        self.ctx.config["minutes"] = 20
        self.assertEqual(self.check(11)[0], 0)
        db.set_setting("email_events_off", ["approval_reminder"])
        self.assertEqual(self.check(25)[0], 0)
        self.assertEqual(self.ctx.get_state("reminded", []), [])   # not used up while off
        db.set_setting("email_events_off", [])
        self.assertEqual(self.check(25)[0], 1)

    def test_queue_and_approve_links(self):
        db.add_task(self.project["id"], "Next one")
        with mock.patch.dict(os.environ, {"DEN_URL": "http://100.1.2.3:8787", "API_KEY": "k"}):
            _, plain, page = self.check(11)[1].call_args.args[0]
        self.assertIn("1 task(s) waiting to start, 1 of them in this project", plain)
        self.assertIn(f"/approvals/{self.approval['id']}/quick/approve?t=", page)
        self.assertIn(f"/approvals/{self.approval['id']}/quick/deny?t=", page)
        self.assertIn("Approve: http://100.1.2.3:8787/approvals/", plain)

    def test_has_an_email_switch_only_while_running(self):
        self.assertIn("approval_reminder", email_templates.EVENTS)
        for fn in self.ctx.cleanups:
            fn()
        self.ctx.cleanups = []
        self.assertNotIn("approval_reminder", email_templates.EVENTS)
