"""Message centre: chats grouped by task, newest message first, searchable and filterable."""

import db
from tests.helpers import TestBase


class MessageCentre(TestBase):
    def setUp(self):
        super().setUp()
        self.other = db.add_project("Website", self._tmp.name, category="personal")

    def chat(self, project, prompt, reply=None, at="2026-09-24T10:00:00+00:00", lane="agent", title=None):
        """A finished chat whose one message landed at `at`."""
        t = db.add_task(project["id"], prompt, lane=lane, title=title)
        self.stamp(t["id"], at)
        if reply is not None:
            db.mark_done(t["id"], reply)
            self.stamp(t["id"], at)
        return t

    def stamp(self, task_id, at):
        conn = db.get_conn()
        conn.execute("UPDATE tasks SET created_at = ?, finished_at = ? WHERE id = ?", (at, at, task_id))
        conn.commit()

    def reply(self, root, prompt, reply, at="2026-09-24T10:30:00+00:00"):
        t = db.add_task(root["project_id"], prompt, thread_id=root["id"])
        db.mark_done(t["id"], reply)
        self.stamp(t["id"], at)
        return t

    def ids(self, **kw):
        return [c["id"] for c in db.list_chats(**kw)["chats"]]

    def test_a_reply_belongs_to_its_chat_and_lifts_it_to_the_top(self):
        old = self.chat(self.project, "fix the login bug", "Fixed it", at="2026-09-20T10:00:00+00:00")
        new = self.chat(self.project, "write the changelog", "Done", at="2026-09-22T10:00:00+00:00")
        self.assertEqual(self.ids(), [new["id"], old["id"]])
        self.reply(old, "also add a test", "Test added", at="2026-09-24T09:00:00+00:00")
        out = db.list_chats()
        self.assertEqual([c["id"] for c in out["chats"]], [old["id"], new["id"]])
        self.assertEqual(out["total"], 2)
        top = out["chats"][0]
        self.assertEqual(top["turns"], 2)
        self.assertEqual(top["preview"], {"role": "reply", "text": "Test added"})

    def test_unanswered_message_previews_your_prompt(self):
        t = db.add_task(self.project["id"], "still waiting on this")
        chat = db.list_chats()["chats"][0]
        self.assertEqual((chat["id"], chat["status"]), (t["id"], "queued"))
        self.assertEqual(chat["preview"], {"role": "you", "text": "still waiting on this"})

    def test_search_needs_every_word_across_the_whole_chat(self):
        root = self.chat(self.project, "deploy the site", "Deployed to staging")
        self.reply(root, "now roll back", "Rolled back cleanly", at="2026-09-24T11:00:00+00:00")
        self.chat(self.project, "deploy the docs", "Docs are live")
        self.assertEqual(self.ids(q="deploy staging"), [root["id"]])   # words in different messages
        self.assertEqual(self.ids(q="ROLLED"), [root["id"]])            # case-blind
        self.assertEqual(self.ids(q="deploy nonsense"), [])

    def test_search_matches_project_name_and_title(self):
        a = self.chat(self.project, "x", "y", title="Quarterly report")
        b = self.chat(self.other, "z", "w", at="2026-09-25T10:00:00+00:00")
        self.assertEqual(self.ids(q="quarterly"), [a["id"]])
        self.assertEqual(self.ids(q="website"), [b["id"]])

    def test_search_returns_the_matching_message_with_context(self):
        root = self.chat(self.project, "start", "first answer")
        self.reply(root, "second", "the " + "filler " * 30 + "needle " + "filler " * 30)
        snip = db.list_chats(q="needle")["chats"][0]["snippet"]
        self.assertEqual(snip["role"], "reply")
        self.assertIn("needle", snip["text"])
        self.assertTrue(snip["text"].startswith("…") and snip["text"].endswith("…"))
        self.assertIsNone(db.list_chats()["chats"][0]["snippet"])

    def test_wildcards_in_the_search_are_plain_text(self):
        pct = self.chat(self.project, "went up 50% today", "ok")
        self.chat(self.project, "went up 500 today", "ok", at="2026-09-25T10:00:00+00:00")
        self.assertEqual(self.ids(q="50%"), [pct["id"]])
        self.assertEqual(self.ids(q="_"), [])

    def test_status_filters_go_by_the_latest_message(self):
        done = self.chat(self.project, "a", "fine")
        failed = self.chat(self.project, "b", None, at="2026-09-24T11:00:00+00:00")
        db.mark_error(failed["id"], "boom", needs_review=False)
        queued = db.add_task(self.project["id"], "c")
        self.assertEqual(self.ids(status="done"), [done["id"]])
        self.assertEqual(self.ids(status="error"), [failed["id"]])
        self.assertEqual(self.ids(status="active"), [queued["id"]])
        self.assertEqual(self.ids(status="needs"), [failed["id"]])
        # Answering a failed chat with a good reply takes it off the failed list.
        self.reply(failed, "try again", "worked", at="2026-09-24T12:00:00+00:00")
        self.assertEqual(self.ids(status="error"), [])

    def test_a_pending_approval_means_needs_you(self):
        t = db.add_task(self.project["id"], "run the tests")
        db.mark_running(t["id"])
        self.assertEqual(self.ids(status="needs"), [])
        db.create_approval(t["id"], "Bash", '{"command": "npm test"}')
        chat = db.list_chats(status="needs")["chats"][0]
        self.assertEqual((chat["id"], chat["needs_you"]), (t["id"], True))

    def test_project_and_lane_filters(self):
        a = self.chat(self.project, "a", "x")
        b = self.chat(self.other, "b", "x", lane="api", at="2026-09-25T10:00:00+00:00")
        self.assertEqual(self.ids(project_ids=[self.project["id"]]), [a["id"]])
        self.assertEqual(self.ids(project_ids=[]), [])
        self.assertEqual(self.ids(lane="api"), [b["id"]])
        self.assertEqual(self.ids(lane="agent", project_ids=[self.other["id"]]), [])

    def test_paging_reports_the_full_total(self):
        for i in range(5):
            self.chat(self.project, f"chat {i}", "ok", at=f"2026-09-2{i}T10:00:00+00:00")
        first = db.list_chats(limit=2)
        self.assertEqual((len(first["chats"]), first["total"]), (2, 5))
        rest = db.list_chats(limit=2, offset=2)["chats"]
        self.assertFalse({c["id"] for c in first["chats"]} & {c["id"] for c in rest})
