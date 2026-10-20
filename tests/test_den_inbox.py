"""The Den 2.0 Inbox: mail, reviews and unread answers in one list, with archive / snooze kept by the Den."""

from unittest import mock

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_inbox

MAIL = [{"id": "m1", "from_name": "Abhishek Rao", "from_email": "abhishek@example.com", "subject": "Demo Thursday",
         "snippet": "Can we also show stage history?", "date": "2026-10-10T09:00:00+00:00", "unread": True},
        {"id": "m2", "from_name": "GitHub", "from_email": "noreply@github.com", "subject": "CI passed",
         "snippet": "All checks passed", "date": "2026-10-10T08:00:00+00:00", "unread": False}]


class Inbox(TestBase):
    def setUp(self):
        super().setUp()
        den_inbox._ready = False
        den_inbox._cache.update(at=0.0, data=None)
        mail = mock.MagicMock()
        mail.search.return_value = MAIL
        mail.get.side_effect = lambda i: {**next(m for m in MAIL if m["id"] == i), "body": "Hi Tushar,\n\nThursday works."}
        for target, value in ((den_inbox, "_mail_connected"), (den_inbox.mail_providers, "email")):
            p = mock.patch.object(target, value, return_value=True if value == "_mail_connected" else mail)
            p.start()
            self.addCleanup(p.stop)
        self.mail = mail

    def keys(self):
        return [i["key"] for i in den_inbox.inbox(refresh=True)["items"]]

    def test_mail_needs_you_until_read(self):
        items = den_inbox.inbox(refresh=True)["items"]
        self.assertEqual([(i["key"], i["needs"]) for i in items], [("e:m1", True), ("e:m2", False)])
        den_inbox.mail_detail("m1")
        self.assertFalse(den_inbox.inbox(refresh=True)["items"][0]["needs"])

    def test_archive_snooze_and_restore(self):
        den_inbox._set("e:m1", archived_at=db._now())
        den_inbox._set("e:m2", snooze_until="2999-01-01T09:00:00+00:00")
        self.assertEqual(self.keys(), [])
        den_inbox._set("e:m2", snooze_until="2000-01-01T09:00:00+00:00")   # snooze over - back in the list
        self.assertEqual(self.keys(), ["e:m2"])
        den_inbox._set("e:m1", archived_at=None)
        self.assertEqual(self.keys(), ["e:m1", "e:m2"])

    def test_a_task_needing_review_is_listed(self):
        t = db.add_task(self.project["id"], "write paper set B", 0, None, lane="agent")
        db.mark_error(t["id"], "Tests failed in section C")
        review = next(i for i in den_inbox.inbox(refresh=True)["items"] if i["kind"] == "review")
        self.assertEqual((review["key"], review["needs"]), (f"c:{t['id']}", True))

    def test_reply_quotes_the_original_and_is_remembered(self):
        out = den_inbox.mail_reply("m1", den_inbox.ReplyIn(body="Yes, I'll add it."))
        self.assertEqual(out["to"], ["abhishek@example.com"])
        to, subject, body, cc, reply_to = self.mail.send.call_args[0][:5]
        self.assertEqual((subject, reply_to), ("Re: Demo Thursday", "m1"))
        self.assertTrue(body.startswith("Yes, I'll add it."))
        self.assertTrue(den_inbox.inbox(refresh=True)["items"][0]["replied"])

    def test_no_mail_account_still_lists_the_rest(self):
        with mock.patch.object(den_inbox, "_mail_connected", return_value=False):
            out = den_inbox.inbox(refresh=True)
        self.assertEqual((out["items"], out["mail"]["connected"]), ([], False))
