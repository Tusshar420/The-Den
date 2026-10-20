"""Den 2.1 chat sharing is view-only: a link shows one chat read-only, turning it off kills it, and nobody
can be given chat access yet."""

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_share


class Share(TestBase):
    def setUp(self):
        super().setUp()
        den_share._ready = False

    def chat(self):
        t = db.add_task(self.project["id"], "fix the <script>alert(1)</script> page", 0, None, lane="agent")
        db.mark_done(t["id"], "Fixed & tested.")
        return t

    def test_link_shows_the_chat_read_only_and_escaped(self):
        t = self.chat()
        v = den_share.set_link(t["id"], True)
        html = den_share.page(v["path"].rsplit("/", 1)[1])
        self.assertIn("Fixed &amp; tested.", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_turning_the_link_off_kills_it(self):
        t = self.chat()
        token = den_share.set_link(t["id"], True)["path"].rsplit("/", 1)[1]
        den_share.set_link(t["id"], False)
        with self.assertRaises(HTTPException):
            den_share.page(token)
        with self.assertRaises(HTTPException):
            den_share.page("")

    def test_only_view_access_for_now(self):
        t = self.chat()
        with self.assertRaises(HTTPException) as e:
            den_share.add_member(t["id"], den_share.MemberIn(name="Abhishek", role="chat"))
        self.assertEqual(e.exception.status_code, 400)
        v = den_share.add_member(t["id"], den_share.MemberIn(name="Abhishek Rao"))
        self.assertEqual(v["members"][0]["role"], "view")
        self.assertEqual(den_share.all_shared(), {str(t["id"]): 1})
        self.assertEqual(den_share.remove_member(t["id"], "Abhishek Rao")["members"], [])

    def test_a_reply_shares_its_whole_chat(self):
        t = self.chat()
        r = db.add_task(self.project["id"], "and the footer", 0, t["id"], lane="agent")
        self.assertEqual(den_share.set_link(r["id"], True)["root_id"], t["id"])
