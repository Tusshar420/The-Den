"""Email: fixed templates render without a model, nothing is sent unless configured or enabled,
and restarts / project changes email the right event."""

import unittest
from unittest import mock

import api
import db
import email_templates
import mailer
import restart_control
from tests.helpers import TestBase


class MailerCase(TestBase):
    def configure(self):
        self.connect_google()

    def sent(self, **kw):
        return mock.patch.object(mailer, "_deliver", return_value=True, **kw)


class Templates(unittest.TestCase):
    def test_every_event_renders_and_escapes(self):
        for event in email_templates.EVENTS:
            subject, plain, page = email_templates.render(event, {"name": "<b>x</b>", "changed": "a.py"})
            self.assertTrue(subject and plain and page)
        _, plain, page = email_templates.render("project_created", {"name": "<b>x</b>"})
        self.assertIn("<b>x</b>", plain)
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;", page)


class Sending(MailerCase):
    def test_no_config_is_a_quiet_noop(self):
        with self.sent() as deliver:
            self.assertFalse(mailer.send("test", wait=True))
        deliver.assert_not_called()

    def test_toggle(self):
        self.configure()
        self.assertEqual(mailer.recipient(), "tusshartg420@gmail.com")
        with self.sent() as deliver:
            self.assertTrue(mailer.send("project_created", wait=True, name="A"))
            db.set_setting("email_events_off", ["project_created"])
            self.assertFalse(mailer.send("project_created", wait=True, name="A"))
            self.assertTrue(mailer.send("project_created", wait=True, force=True, name="A"))
        self.assertEqual(deliver.call_count, 2)

    def test_sends_html_through_gmail(self):
        self.configure()
        with mock.patch.object(mailer.GmailProvider, "send") as gmail:
            self.assertTrue(mailer.send("test", wait=True))
        to, subject, body = gmail.call_args.args
        self.assertEqual(to, ["tusshartg420@gmail.com"])
        self.assertTrue(subject.startswith("[The Den]"))
        self.assertIn("<div", gmail.call_args.kwargs["html"])

    def test_failure_is_swallowed(self):
        self.configure()
        with mock.patch.object(mailer.GmailProvider, "send", side_effect=mailer.ProviderError("down")):
            self.assertFalse(mailer.send("test", wait=True))


class Restarts(MailerCase):
    def test_requested_then_completed(self):
        with mock.patch.object(mailer, "send") as send:
            mailer.note_restart_requested(False, [3, 4])
            self.assertEqual(send.call_args.args[0], "restart_requested")
            self.assertIn("#3", send.call_args.kwargs["detail"])
            self.assertTrue(mailer.RESTART_MARK.exists())
            mailer.on_started("2026-10-03T10:00:00", 2)
            self.assertEqual(send.call_args.args[0], "restart_completed")
            self.assertFalse(mailer.RESTART_MARK.exists())
            mailer.on_started("2026-10-03T10:05:00")
            self.assertEqual(send.call_args.args[0], "unexpected_restart")

    def test_needs_restart_waits_for_quiet_and_sends_once(self):
        self.configure()
        mailer._notified_mtime = 0.0
        files = [(1000.0, "api.py")]
        with mock.patch.object(mailer, "_changed_sources", return_value=files), self.sent() as deliver:
            self.assertFalse(mailer.check_needs_restart(500.0, 1100.0))      # still being edited
            self.assertTrue(mailer.check_needs_restart(500.0, 1000.0 + mailer.QUIET_SECONDS))
            self.assertFalse(mailer.check_needs_restart(500.0, 5000.0))      # already told
        with mock.patch.object(mailer, "_changed_sources", return_value=[]):
            self.assertFalse(mailer.check_needs_restart(500.0, 9000.0))

    def test_on_startup_returns_released_count(self):
        old = restart_control.HELD_FILE
        restart_control.HELD_FILE = mailer.RESTART_MARK.parent / "held.json"
        self.addCleanup(lambda: setattr(restart_control, "HELD_FILE", old))
        self.assertEqual(restart_control.on_startup(), 0)


class ProjectEvents(MailerCase):
    def test_lifecycle_emails(self):
        with mock.patch.object(mailer, "send") as send:
            p = api.create_project(api.ProjectIn(name="Acme"))
            self.assertEqual(send.call_args.args[0], "project_created")
            api.update_project(p["id"], api.ProjectActiveIn(status="in_progress"))
            send.reset_mock()
            api.update_project(p["id"], api.ProjectActiveIn(status="completed"))
            self.assertEqual(send.call_args.args[0], "project_completed")
            api.update_project(p["id"], api.ProjectActiveIn(status="archived"))
            self.assertEqual(send.call_args.args[0], "project_archived")
            send.reset_mock()
            api.update_project(p["id"], api.ProjectActiveIn(name="Acme 2"))
            send.assert_not_called()
            api.delete_project(p["id"], api.ProjectDeleteIn(confirm="Acme 2"))
            self.assertEqual(send.call_args.args[0], "project_deleted")


if __name__ == "__main__":
    unittest.main()
