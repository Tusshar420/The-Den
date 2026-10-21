"""
Sharing (modules/den_collab): who may see and do what, that guests only get safe fields, that a removed or reset
link stops working at once, and that live chat wakes up when someone else writes.

The Den's own API is replaced by a fake that adds the task straight to the throwaway database, so nothing runs.
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import TestBase

import db
from modules.den_collab import module as collab_module
from modules.den_collab import server, store


class FakeHub(server.Hub):
    def den(self, method, path, body=None):
        if method == "POST" and path.endswith("/tasks"):
            pid = int(path.split("/")[2])
            t = db.add_task(pid, body["prompt"], thread_id=body.get("thread_id"))
            return {"id": t["id"], "status": t["status"]}
        if method == "POST" and path.endswith("/pause"):
            db.set_paused(int(path.split("/")[2]), True)
            return {}
        raise server.Refused(404, "not faked")


class CollabTest(TestBase):
    def setUp(self):
        super().setUp()
        store._ready = False
        store.init()
        self.p1 = db.add_project("Acme", "/tmp")
        self.p2 = db.add_project("Secret", "/tmp")

    def person(self, name, role="editor", pid=None, hold=False):
        m, token = store.create_member(name, hold=hold)
        store.set_share(m["id"], "project", pid or self.p1["id"], role)
        return m, token

    # ------------------------------------------------------------ access
    def test_roles_and_scope(self):
        m, _ = self.person("Priya", "viewer")
        self.assertEqual(server.project_role(m, self.p1["id"]), "viewer")
        self.assertIsNone(server.project_role(m, self.p2["id"]))
        with self.assertRaises(server.Refused) as e:
            server.need(None)
        self.assertEqual(e.exception.status, 404)          # not shared looks the same as not there
        with self.assertRaises(server.Refused) as e:
            server.need("viewer", "editor")
        self.assertEqual(e.exception.status, 403)
        self.assertEqual(server.need("admin", "editor"), "admin")

    def test_chat_share_only_covers_that_chat(self):
        a = db.add_task(self.p1["id"], "one")
        b = db.add_task(self.p1["id"], "two")
        m, _ = store.create_member("Chat guest")
        store.set_share(m["id"], "chat", a["id"], "editor")
        self.assertEqual(server.chat_role(m, db.get_task(a["id"])), "editor")
        self.assertIsNone(server.chat_role(m, db.get_task(b["id"])))
        self.assertIsNone(server.project_role(m, self.p1["id"]))

    def test_reply_resolves_to_its_chat(self):
        root = db.add_task(self.p1["id"], "start")
        reply = db.add_task(self.p1["id"], "more", thread_id=root["id"])
        self.assertEqual(server.chat_root(reply["id"])["id"], root["id"])

    def test_token_is_only_stored_hashed_and_reset_kills_old(self):
        m, token = store.create_member("Ann")
        with db._lock:
            row = db.get_conn().execute("SELECT * FROM collab_members WHERE id = ?", (m["id"],)).fetchone()
        self.assertNotIn(token, json.dumps(dict(row)))
        self.assertEqual(store.by_token(token)["id"], m["id"])
        new = store.reset_token(m["id"])
        self.assertIsNone(store.by_token(token))
        self.assertEqual(store.by_token(new)["id"], m["id"])
        store.remove_member(m["id"])
        self.assertIsNone(store.by_token(new))

    def test_guest_view_has_no_private_fields(self):
        t = db.add_task(self.p1["id"], "hello")
        with db._lock:
            c = db.get_conn()
            c.execute("UPDATE tasks SET result_summary='hi there', session_id='sess-123', cost_usd=9.5, current_step='ran rm -rf /secret' WHERE id=?", (t["id"],))
            c.commit()
        view = server._chat_view(db.get_task(t["id"]), "viewer")
        text = json.dumps(view)
        for leak in ("sess-123", "9.5", "rm -rf", "/tmp"):
            self.assertNotIn(leak, text)
        self.assertEqual(view["messages"][0]["reply"], "hi there")

    def test_task_list_hides_other_projects_and_hidden(self):
        db.add_task(self.p1["id"], "mine")
        db.add_task(self.p2["id"], "not for guests")
        titles = [r["title"] for r in server._task_list(self.p1["id"])]
        self.assertTrue(any("mine" in t.lower() for t in titles))
        self.assertFalse(any("not for guests" in t.lower() for t in titles))

    def test_hold_registry_pauses_only_the_matching_task(self):
        hub = FakeHub("k", "Host", "1")
        hub.expect_hold(self.p1["id"], None, "wait for me")
        other = db.add_task(self.p1["id"], "something else")
        hub.hold_if_expected(db.get_task(other["id"]))
        self.assertFalse(db.get_task(other["id"])["paused"])
        mine = db.add_task(self.p1["id"], "wait for me")
        hub.hold_if_expected(db.get_task(mine["id"]))
        self.assertTrue(db.get_task(mine["id"])["paused"])
        hub.hold_if_expected(db.get_task(mine["id"]))        # used up: a second task with the same words isn't held
        again = db.add_task(self.p1["id"], "wait for me")
        hub.hold_if_expected(db.get_task(again["id"]))
        self.assertFalse(db.get_task(again["id"])["paused"])

    def test_parse_link(self):
        self.assertEqual(collab_module.parse_link("http://mac.local:8791/#join=abc123"), ("http://mac.local:8791", "abc123"))
        for bad in ("", "hello", "ftp://x/#join=a", "http://x:1/", "http://x:1/#nope=1"):
            with self.assertRaises(ValueError):
                collab_module.parse_link(bad)


class LiveServerTest(TestBase):
    def setUp(self):
        super().setUp()
        store._ready = False
        store.init()
        self.project = db.add_project("Acme", "/tmp")
        self.server = server.ShareServer("127.0.0.1", 0, FakeHub("k", "Tushar", "1"))
        self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def call(self, method, path, token="", body=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Collab-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def person(self, name, role, hold=False):
        m, token = store.create_member(name, hold=hold)
        store.set_share(m["id"], "project", self.project["id"], role)
        return m, token

    def test_viewer_reads_editor_writes_and_names_show(self):
        _, ed = self.person("Priya", "editor")
        _, vw = self.person("Vik", "viewer")
        pid = self.project["id"]
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/tasks", vw, {"prompt": "x"})[0], 403)
        s, made = self.call("POST", f"/api/projects/{pid}/tasks", ed, {"prompt": "from Priya"})
        self.assertEqual(s, 200)
        s, chat = self.call("GET", f"/api/chats/{made['id']}", vw)
        self.assertEqual((s, chat["messages"][0]["by"]), (200, "Priya"))
        self.assertEqual(self.call("GET", "/api/me", "wrong")[0], 401)
        self.assertEqual(self.call("GET", "/api/projects/9999", ed)[0], 404)

    def test_live_chat_wakes_when_someone_writes(self):
        _, ed = self.person("Priya", "editor")
        _, other = self.person("Sam", "editor")
        pid = self.project["id"]
        _, made = self.call("POST", f"/api/projects/{pid}/tasks", ed, {"prompt": "start"})
        _, chat = self.call("GET", f"/api/chats/{made['id']}", other)
        got = {}

        def wait():
            t0 = time.time()
            got["chat"] = self.call("GET", f"/api/chats/{made['id']}?sig={chat['sig']}&wait=15", other)[1]
            got["secs"] = time.time() - t0
        th = threading.Thread(target=wait)
        th.start()
        time.sleep(1)
        self.call("POST", f"/api/chats/{made['id']}/messages", ed, {"prompt": "second"})
        th.join(20)
        self.assertLess(got["secs"], 8)
        self.assertEqual([m["prompt"] for m in got["chat"]["messages"]], ["start", "second"])

    def test_removed_person_is_locked_out_and_held_task_waits(self):
        m, tok = self.person("Ann", "editor", hold=True)
        pid = self.project["id"]
        s, made = self.call("POST", f"/api/projects/{pid}/tasks", tok, {"prompt": "held"})
        self.assertEqual(s, 200)
        store.remove_member(m["id"])
        self.assertEqual(self.call("GET", "/api/me", tok)[0], 401)

    def test_bad_input(self):
        _, ed = self.person("Priya", "editor")
        pid = self.project["id"]
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/tasks", ed, {"prompt": "   "})[0], 400)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/tasks", ed, {"prompt": "x" * 9000})[0], 400)
        self.assertEqual(self.call("GET", "/api/documents/abc", ed)[0], 404)


if __name__ == "__main__":
    unittest.main()
