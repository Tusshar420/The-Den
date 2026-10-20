"""The Den 2.0 project page: files stay inside the project folder, git commits, ideas."""

import subprocess
from pathlib import Path

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_projects as dp


class ProjectFiles(TestBase):
    def setUp(self):
        super().setUp()
        self.root = Path(self._tmp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "a.py").write_text("print(1)\n")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "x.js").write_text("x")
        self.p = db.add_project("Proj", str(self.root))

    def test_tree_skips_dependency_folders(self):
        t = dp.tree(self.p["id"])
        paths = [e["path"] for e in t["entries"]]
        self.assertIn("src/a.py", paths)
        self.assertIn("src/", paths)
        self.assertFalse(any("node_modules" in p for p in paths))

    def test_read_and_save(self):
        self.assertEqual(dp.read_file(self.p["id"], "src/a.py")["text"], "print(1)\n")
        dp.write_file(self.p["id"], dp.FileIn(path="src/a.py", text="print(2)\n"))
        self.assertEqual((self.root / "src" / "a.py").read_text(), "print(2)\n")
        dp.write_file(self.p["id"], dp.FileIn(path="src/new/b.py", text="x", create=True))
        self.assertTrue((self.root / "src" / "new" / "b.py").exists())
        with self.assertRaises(HTTPException) as e:
            dp.write_file(self.p["id"], dp.FileIn(path="src/a.py", text="y", create=True))
        self.assertEqual(e.exception.status_code, 409)

    def test_paths_cannot_leave_the_folder(self):
        outside = Path(self._tmp.name) / "secret.txt"
        outside.write_text("no")
        (self.root / "link").symlink_to(outside)
        for bad in ("../secret.txt", "/etc/passwd", "link", "node_modules/x.js", ".git/config"):
            with self.assertRaises(HTTPException, msg=bad):
                dp.read_file(self.p["id"], bad)
            with self.assertRaises(HTTPException, msg=bad):
                dp.write_file(self.p["id"], dp.FileIn(path=bad, text="x"))
        self.assertEqual(outside.read_text(), "no")

    def test_commit_selected_files(self):
        g = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], capture_output=True, text=True, check=True)  # noqa: E731
        g("init", "-q")
        g("-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "start")
        (self.root / "b.txt").write_text("b\n")
        ch = dp.changes(self.p["id"])
        self.assertEqual({f["path"] for f in ch["files"]}, {"src/a.py", "b.txt"})
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "T"], check=True)
        out = dp.commit(self.p["id"], dp.CommitIn(message="add b", paths=["b.txt"]))
        self.assertTrue(out["sha"])
        left = {f["path"] for f in dp.changes(self.p["id"])["files"]}
        self.assertEqual(left, {"src/a.py"})


class Ideas(TestBase):
    def test_park_edit_and_send(self):
        p = db.add_project("Proj", self._tmp.name)
        i = dp.add_idea(p["id"], dp.IdeaIn(text="Widget store", size="Big"))
        self.assertEqual((i["status"], i["size"]), ("parked", "Big"))
        dp.update_idea(i["id"], dp.IdeaPatch(text="Widget store v2"))
        sent = dp.send_idea(i["id"], "queue")
        self.assertEqual(sent["status"], "queued")
        self.assertEqual(db.get_task(sent["task_id"])["prompt"], "Widget store v2")
        j = dp.add_idea(p["id"], dp.IdeaIn(text="Later"))
        self.assertEqual(dp.send_idea(j["id"], "backlog")["status"], "backlog")
        self.assertIn("Later", [t["text"] for t in db.list_todos(p["id"])])
        dp.delete_idea(j["id"])
        self.assertEqual([x["id"] for x in dp.list_ideas(p["id"])], [i["id"]])
