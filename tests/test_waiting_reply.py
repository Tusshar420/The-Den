"""Den 2.1: a run that ends by asking the owner something waits for the answer instead of counting as finished."""

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_views
from octopus import execution


class WaitingForAnswer(TestBase):
    def test_marker_and_question_are_spotted(self):
        self.assertEqual(execution.split_waiting("Which project?\n\n[WAITING FOR YOU]"), ("Which project?", True))
        self.assertTrue(execution.split_waiting("Done the first part.\n\nShould I also update the tests?")[1])
        self.assertFalse(execution.split_waiting("All done - tests pass.")[1])
        self.assertIn("[WAITING FOR YOU]", execution.HOUSE_RULES)

    def test_waiting_chat_needs_you_until_answered(self):
        t = db.add_task(self.project["id"], "add a board", 0, None, lane="agent")
        db.mark_done(t["id"], "dnd-kit or native drag?")
        db.set_awaiting_reply(t["id"], True)
        chat = db.list_chats(status="needs")["chats"][0]
        self.assertTrue(chat["needs_you"] and chat["awaiting_reply"])
        db.add_task(self.project["id"], "dnd-kit", 0, t["id"], lane="agent")          # the answer
        self.assertFalse(db.get_task(t["id"])["awaiting_reply"])

    def test_its_done_clears_it(self):
        t = db.add_task(self.project["id"], "tidy up", 0, None, lane="agent")
        db.mark_done(t["id"], "Want me to delete the old files too?")
        db.set_awaiting_reply(t["id"], True)
        den_views.build_router(lambda: None)
        r = next(x for x in den_views.build_router(lambda: None).routes if x.path == "/den/tasks/{task_id}/settled")
        r.endpoint(t["id"])
        self.assertFalse(db.get_task(t["id"])["awaiting_reply"])
        self.assertEqual(db.list_chats(status="needs")["chats"], [])
