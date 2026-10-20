"""A backlog item can carry pictures, which go onto its task when it is queued."""

from tests.helpers import TestBase  # first: clears the real API keys before api/worker import

from pathlib import Path

from fastapi import HTTPException

import api
import db
import uploads

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class TodoImages(TestBase):
    def setUp(self):
        super().setUp()
        old = uploads.UPLOAD_DIR
        uploads.UPLOAD_DIR = Path(self._tmp.name) / "uploads"
        self.addCleanup(lambda: setattr(uploads, "UPLOAD_DIR", old))
        self.app = db.add_project("shop", self._tmp.name)
        self.img = uploads.save(PNG)["id"]

    def add(self, **body):
        return api.create_todo(self.app["id"], api.TodoIn(text="Match this mockup", **body))

    def patch(self, todo, **body):
        return api.update_todo(todo["id"], api.TodoUpdateIn(**body))

    def run_all(self):
        return db.get_task(api.run_checked_todos(self.app["id"])[0]["task_id"])

    def test_added_with_pictures(self):
        self.assertEqual(self.add(images=[self.img])["images"], [self.img])
        self.assertIsNone(self.add()["images"])

    def test_unknown_picture_is_refused(self):
        for body in ({"images": ["0" * 32 + ".png"]}, {"images": ["../queue.db"]}):
            with self.assertRaises(HTTPException) as e:
                self.add(**body)
            self.assertEqual(e.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            self.patch(self.add(), images=["0" * 32 + ".png"])

    def test_pictures_can_be_replaced_and_removed(self):
        t = self.add(images=[self.img])
        other = uploads.save(PNG)["id"]
        self.assertEqual(self.patch(t, images=[self.img, other])["images"], [self.img, other])
        self.assertEqual(self.patch(t, text="Match this mockup v2")["images"], [self.img, other])  # text-only edit keeps them
        self.assertIsNone(self.patch(t, images=[])["images"])

    def test_queueing_carries_them_onto_the_task(self):
        self.patch(self.add(images=[self.img]), checked=True)
        task = self.run_all()
        self.assertEqual(task["images"], [self.img])
        self.assertEqual(task["lane"], "agent")

    def test_model_that_cant_read_pictures_falls_back_to_claude_code(self):
        self.patch(self.add(images=[self.img]), model="gpt-image-1", checked=True)
        task = self.run_all()
        self.assertEqual((task["model_id"], task["images"]), ("claude-code", [self.img]))

    def test_vision_model_is_kept(self):
        self.patch(self.add(images=[self.img]), model="gpt-6-sol", checked=True)
        task = self.run_all()
        self.assertEqual((task["model_id"], task["images"]), ("gpt-6-sol", [self.img]))
