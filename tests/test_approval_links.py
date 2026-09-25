"""Approve / Deny buttons on approval pushes: signed links, the page they open."""

import json
import os
from unittest import mock

from tests.helpers import TestBase

import api
import approval_links
import db

ENV = {"API_KEY": "test-key", "DEN_URL": "http://100.1.2.3:8787/"}


class ApprovalLinks(TestBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        task = db.add_task(self.project["id"], "tidy up", 0)
        self.approval = db.create_approval(task["id"], "Bash", json.dumps({"command": "git push origin main"}))

    def open(self, decision, t=None, approval_id=None):
        aid = approval_id or self.approval["id"]
        return api.quick_decide(aid, decision, approval_links.token(aid, decision) if t is None else t)

    def test_buttons(self):
        acts = approval_links.actions(self.approval["id"])
        self.assertEqual([a["title"] for a in acts], ["Approve", "Deny", "Open dashboard"])
        self.assertTrue(acts[0]["url"].startswith(f"http://100.1.2.3:8787/approvals/{self.approval['id']}/quick/approve?t="))
        self.assertEqual(acts[2]["url"], "http://100.1.2.3:8787/dashboard")
        self.assertEqual([a["title"] for a in approval_links.actions()], ["Open dashboard"])
        with mock.patch.dict(os.environ, {"DEN_URL": ""}):
            self.assertEqual(approval_links.actions(self.approval["id"]), [])
        with mock.patch.dict(os.environ, {"API_KEY": ""}):
            self.assertEqual([a["title"] for a in approval_links.actions(self.approval["id"])], ["Open dashboard"])

    def test_approve(self):
        page = self.open("approve")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Approved", page.body.decode())
        self.assertIn("git push origin main", page.body.decode())
        self.assertEqual(db.get_approval(self.approval["id"])["status"], "approved")

    def test_deny(self):
        self.assertIn("Denied", self.open("deny").body.decode())
        self.assertEqual(db.get_approval(self.approval["id"])["status"], "denied")

    def test_used_link_changes_nothing(self):
        self.open("deny")
        page = self.open("approve")
        self.assertIn("Already decided", page.body.decode())
        self.assertEqual(db.get_approval(self.approval["id"])["status"], "denied")

    def test_forged_links_are_refused(self):
        deny_token = approval_links.token(self.approval["id"], "deny")
        other = db.create_approval(self.approval["task_id"], "Bash", json.dumps({"command": "rm -rf build"}))
        for page in (self.open("approve", t=deny_token),                      # deny's token on approve
                     self.open("approve", t="0" * 32),
                     self.open("approve", t=""),
                     self.open("approve", t=approval_links.token(other["id"], "approve")),  # another approval's token
                     self.open("maybe")):
            self.assertEqual(page.status_code, 403)
        self.assertEqual(db.get_approval(self.approval["id"])["status"], "pending")
        with mock.patch.dict(os.environ, {"API_KEY": ""}):                    # nothing to sign with: no links work
            self.assertEqual(self.open("approve", t=approval_links.token(self.approval["id"], "approve")).status_code, 403)

    def test_command_is_escaped(self):
        a = db.create_approval(self.approval["task_id"], "Bash", json.dumps({"command": "echo '<script>x</script>'"}))
        body = self.open("approve", approval_id=a["id"]).body.decode()
        self.assertNotIn("<script>x", body)
        self.assertIn("&lt;script&gt;", body)
