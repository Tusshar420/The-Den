"""Home's chat panel (Den 2.1): renaming a chat sticks, and a chat exports as Markdown."""

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_views


class ChatDetails(TestBase):
    def test_rename_sticks_and_the_namer_leaves_it(self):
        t = db.add_task(self.project["id"], "write the docs", 0, None, lane="agent")
        self.assertEqual(den_views.rename_chat(t["id"], "  Docs   sprint ")["title"], "Docs sprint")
        db.set_thread_title(t["id"], "Something else", "ai", only_if_by="text")      # what the auto-namer does
        self.assertEqual(db.get_task(t["id"])["title"], "Docs sprint")

    def test_export_has_every_message(self):
        t = db.add_task(self.project["id"], "write the docs", 0, None, lane="agent")
        db.mark_done(t["id"], "Docs written.")
        md = den_views.export_chat(t["id"])
        self.assertIn("write the docs", md)
        self.assertIn("Docs written.", md)
