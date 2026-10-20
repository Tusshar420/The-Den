"""Project environments: one project, several folders; a task runs in the one it names."""

import os
import tempfile

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

from fastapi import HTTPException

import api
import db


class ProjectEnvs(TestBase):
    def setUp(self):
        super().setUp()
        self.dev = tempfile.mkdtemp()

    def test_task_runs_in_its_env_folder_else_the_project_folder(self):
        env = api.create_env(self.project["id"], api.EnvIn(name="dev", path=self.dev))
        plain = db.add_task(self.project["id"], "a", 0)
        in_env = db.add_task(self.project["id"], "b", 0, env_id=env["id"])
        self.assertEqual(db.task_workdir(plain, self.project), self.project["path"])
        self.assertEqual(db.task_workdir(in_env, self.project), os.path.realpath(self.dev) if env["path"] != self.dev else self.dev)

    def test_reply_keeps_the_chat_env(self):
        env = api.create_env(self.project["id"], api.EnvIn(name="dev", path=self.dev))
        root = api.create_task(self.project["id"], api.TaskIn(prompt="hi", env_id=env["id"]))
        reply = api.create_task(self.project["id"], api.TaskIn(prompt="more", thread_id=root["id"]))
        self.assertEqual(reply["env_id"], env["id"])

    def test_rejects_duplicates_missing_folders_and_foreign_envs(self):
        pid = self.project["id"]
        api.create_env(pid, api.EnvIn(name="dev", path=self.dev))
        with self.assertRaises(HTTPException) as dup:
            api.create_env(pid, api.EnvIn(name="dev", path=self.dev))
        self.assertEqual(dup.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            api.create_env(pid, api.EnvIn(name="x", path="/no/such/folder"))
        with self.assertRaises(HTTPException):
            api.create_task(pid, api.TaskIn(prompt="x", env_id=9999))

    def test_deleting_an_env_sends_its_tasks_back_to_the_project_folder(self):
        env = api.create_env(self.project["id"], api.EnvIn(name="dev", path=self.dev))
        t = db.add_task(self.project["id"], "b", 0, env_id=env["id"])
        api.delete_env(self.project["id"], env["id"])
        self.assertEqual(db.task_workdir(db.get_task(t["id"]), self.project), self.project["path"])

    def test_todos_and_phase_items_run_in_their_env(self):
        pid = self.project["id"]
        env = api.create_env(pid, api.EnvIn(name="dev", path=self.dev))
        todo = api.create_todo(pid, api.TodoIn(text="fix it", env_id=env["id"]))
        self.assertEqual(todo["env_id"], env["id"])
        db.run_checked_todos(pid, only_ids=[todo["id"]])
        task = db.get_task(db.get_todo(todo["id"])["task_id"])
        self.assertEqual(task["env_id"], env["id"])
        phase = api.add_phase(pid, api.PhaseIn(name="P1", todos=["one"], env_id=env["id"]))
        self.assertEqual(phase["env_id"], env["id"])
        self.assertTrue(all(db.get_todo(i)["env_id"] == env["id"] for i in phase["todo_ids"]))
        with self.assertRaises(HTTPException):
            api.create_todo(pid, api.TodoIn(text="x", env_id=9999))

    def test_main_label_and_env_names_stay_unique(self):
        pid = self.project["id"]
        api.create_env(pid, api.EnvIn(name="dev", path=self.dev))
        with self.assertRaises(HTTPException):
            api.create_env(pid, api.EnvIn(name="main", path=self.dev))
        with self.assertRaises(HTTPException):
            api.update_project(pid, api.ProjectActiveIn(env_label="Dev"))
        api.update_project(pid, api.ProjectActiveIn(env_label="CMDev"))
        self.assertEqual(db.get_project(pid)["env_label"], "CMDev")

    def test_merging_a_project_makes_it_an_env_and_moves_its_work(self):
        pid = db.add_project("Ellisporter", tempfile.mkdtemp())["id"]
        other = db.add_project("EP-Partial", self.dev)
        chat = db.add_task(other["id"], "old chat", 0)
        with db._lock:
            db.get_conn().execute("UPDATE tasks SET status = 'done', session_id = 'sess-1' WHERE id = ?", (chat["id"],))
            db.get_conn().commit()
        reply = db.add_task(other["id"], "follow-up", 0, thread_id=chat["id"])
        todo = db.add_todo(other["id"], "backlog item")
        phase = db.add_phase(other["id"], "Their phase")
        db.add_project_note(other["id"], "a note")
        db.add_phase(pid, "Our phase")
        out = api.merge_project(pid, api.MergeIn(source_id=other["id"], env_name="Partial"))
        env = out["env"]
        self.assertEqual((env["name"], env["path"], env["project_id"]), ("Partial", self.dev, pid))
        self.assertIsNone(db.get_project(other["id"]))
        for t in (chat, reply):
            moved = db.get_task(t["id"])
            self.assertEqual((moved["project_id"], moved["env_id"]), (pid, env["id"]))
            self.assertEqual(db.task_workdir(moved, db.get_project(pid)), self.dev)
        self.assertEqual(db.get_task(chat["id"])["session_id"], "sess-1")   # same folder, so the session still resumes
        self.assertEqual(db.get_todo(todo["id"])["env_id"], env["id"])
        moved_phase = db.get_phase(phase["id"])
        self.assertEqual((moved_phase["project_id"], moved_phase["env_id"], moved_phase["position"]), (pid, env["id"], 1))
        self.assertEqual([n["text"] for n in db.list_project_notes(pid)], ["a note"])
        # The name is now taken, and a project can't be merged into itself.
        third = db.add_project("EP-Other", tempfile.mkdtemp())
        with self.assertRaises(HTTPException):
            api.merge_project(pid, api.MergeIn(source_id=third["id"], env_name="partial"))
        with self.assertRaises(HTTPException):
            api.merge_project(pid, api.MergeIn(source_id=pid, env_name="Self"))

    def test_merge_refuses_while_the_source_is_running(self):
        target = db.add_project("Ellisporter", tempfile.mkdtemp())
        other = db.add_project("Busy", self.dev)
        db.mark_running(db.add_task(other["id"], "x", 0)["id"])
        with self.assertRaises(HTTPException) as e:
            api.merge_project(target["id"], api.MergeIn(source_id=other["id"], env_name="Busy"))
        self.assertEqual(e.exception.status_code, 409)
        self.assertIsNotNone(db.get_project(other["id"]))

    def test_files_and_cards_follow_the_env(self):
        import den_projects
        pid = self.project["id"]
        with open(os.path.join(self.dev, "only-in-dev.txt"), "w") as f:
            f.write("hi")
        env = api.create_env(pid, api.EnvIn(name="dev", path=self.dev))
        names = [e["path"] for e in den_projects.tree(pid, env["id"])["entries"]]
        self.assertIn("only-in-dev.txt", names)
        self.assertEqual(den_projects.read_file(pid, "only-in-dev.txt", env["id"])["text"], "hi")
        card = next(c for c in den_projects.project_states() if c["id"] == pid)
        self.assertEqual([e["name"] for e in card["envs"]], ["Main", "dev"])
