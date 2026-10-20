"""Pictures attached to prompts: stored safely, carried through every route into a task,
and handed to Claude Code (as paths) and to API models (as image parts)."""

import asyncio
import base64
import unittest
from pathlib import Path

from fastapi import HTTPException, Request

import api
import db
import providers
import uploads
import worker
from octopus import execution, plan as planner, service
from tests.helpers import TestBase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class UploadCase(TestBase):
    def setUp(self):
        super().setUp()
        old = uploads.UPLOAD_DIR
        uploads.UPLOAD_DIR = Path(self._tmp.name) / "uploads"
        self.addCleanup(lambda: setattr(uploads, "UPLOAD_DIR", old))


class Storing(UploadCase):
    def test_saves_by_what_the_bytes_are_not_what_they_claim(self):
        got = uploads.save(PNG)
        self.assertRegex(got["id"], r"^[0-9a-f]{32}\.png$")
        self.assertEqual(got["mime"], "image/png")
        self.assertEqual(uploads.path_of(got["id"]).read_bytes(), PNG)
        self.assertTrue(uploads.save(JPEG)["id"].endswith(".jpg"))

    def test_rejects_files_it_cannot_tell(self):
        for bad in (b"", b"\x00\x01\x02 not a known kind of file", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 old office file with no name"):
            with self.assertRaises(ValueError):
                uploads.save(bad)

    def test_text_is_kept_as_text_and_never_shown_as_a_page(self):
        # Markup or a script is stored as plain text and only ever downloaded (see api.get_upload).
        for text, name in ((b"<svg onload=alert(1)>", "x.svg"), (b"#!/bin/sh\nrm -rf ~", "run.sh"), (b"a,b\n1,2", "data.csv")):
            got = uploads.save(text, name)
            self.assertFalse(uploads.is_image(got["id"]))
            self.assertEqual(got["mime"], "text/plain")
        self.assertTrue(uploads.save(b"a,b\n1,2", "data.csv")["id"].endswith(".csv"))

    def test_rejects_oversize(self):
        with self.assertRaises(ValueError):
            uploads.save(PNG + b"\x00" * uploads.MAX_BYTES)

    def test_an_id_can_only_name_an_upload(self):
        secret = Path(self._tmp.name) / "secret.png"
        secret.write_bytes(PNG)
        for bad in ("../secret.png", "/etc/passwd", "", "a" * 32 + ".png", "x" * 32 + ".exe", None, 5):
            self.assertIsNone(uploads.path_of(bad), bad)

    def test_validate(self):
        good = uploads.save(PNG)["id"]
        self.assertEqual(uploads.validate([good, good]), [good])
        self.assertEqual(uploads.validate(None), [])
        with self.assertRaises(ValueError):
            uploads.validate(["0" * 32 + ".png"])
        with self.assertRaises(ValueError):
            uploads.validate([uploads.save(PNG)["id"] for _ in range(uploads.MAX_PER_PROMPT + 1)])

    @staticmethod
    def request(body: bytes, declared_length=None):
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        length = len(body) if declared_length is None else declared_length
        return Request({"type": "http", "method": "POST", "path": "/uploads", "query_string": b"",
                        "headers": [(b"content-length", str(length).encode())]}, receive)

    def test_upload_endpoint_takes_the_raw_file(self):
        got = asyncio.run(api.upload_image(self.request(PNG)))
        self.assertEqual(uploads.path_of(got["id"]).read_bytes(), PNG)
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(api.upload_image(self.request(b"\x00\x01 not a known kind of file")))
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:                 # refused before the body is read
            asyncio.run(api.upload_image(self.request(PNG, declared_length=uploads.MAX_BYTES + 1)))
        self.assertEqual(cm.exception.status_code, 413)

    def test_the_size_cap_holds_without_a_content_length(self):
        """A chunked body has no Content-Length, and one can also understate it."""
        def chunked(parts, headers):
            queue = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1} for i, p in enumerate(parts)]

            async def receive():
                return queue.pop(0)
            return Request({"type": "http", "method": "POST", "path": "/uploads", "query_string": b"",
                            "headers": headers}, receive)

        big = [PNG + b"\x00" * (uploads.MAX_BYTES // 2), b"\x00" * (uploads.MAX_BYTES // 2 + 1)]
        for headers in ([], [(b"content-length", b"10")]):
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(api.upload_image(chunked(big, headers)))
            self.assertEqual(cm.exception.status_code, 413)
        got = asyncio.run(api.upload_image(chunked([PNG[:10], PNG[10:]], [])))       # small chunked upload still works
        self.assertEqual(uploads.path_of(got["id"]).read_bytes(), PNG)
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(api.upload_image(chunked([PNG], [(b"content-length", b"abc")])))
        self.assertEqual(cm.exception.status_code, 400)

    def test_get_upload_endpoint(self):
        got = uploads.save(PNG)
        self.assertEqual(api.get_upload(got["id"]).media_type, "image/png")
        with self.assertRaises(HTTPException) as cm:
            api.get_upload("../etc/passwd")
        self.assertEqual(cm.exception.status_code, 404)


class IntoTasks(UploadCase):
    def setUp(self):
        super().setUp()
        self.img = uploads.save(PNG)["id"]

    def test_new_task_and_reply_keep_their_pictures(self):
        root = api.create_task(self.project["id"], api.TaskIn(prompt="what is this", images=[self.img]))
        self.assertEqual(db.get_task(root["id"])["images"], [self.img])
        reply = api.create_task(self.project["id"], api.TaskIn(prompt="and this", thread_id=root["id"], images=[self.img]))
        self.assertEqual(reply["images"], [self.img])
        plain = api.create_task(self.project["id"], api.TaskIn(prompt="no picture"))
        self.assertIsNone(plain["images"])

    def test_unknown_picture_is_a_400(self):
        with self.assertRaises(HTTPException) as cm:
            api.create_task(self.project["id"], api.TaskIn(prompt="x", images=["0" * 32 + ".png"]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_octopus_queues_with_pictures_and_plan_keeps_them(self):
        out = service.submit("what does this screenshot show", images=[self.img], source="dashboard")
        if out["status"] == "queued":
            self.assertEqual(db.get_task(out["task"]["id"])["images"], [self.img])
        else:                                    # held for a look: the plan still has them, and Run passes them on
            self.assertEqual(out["plan"]["images"], [self.img])
            run = service.run_plan(out["routing_id"])
            self.assertEqual(db.get_task(run["task"]["id"])["images"], [self.img])

    def test_octopus_rejects_a_missing_picture(self):
        with self.assertRaises(service.ServiceError) as cm:
            service.submit("look", images=["0" * 32 + ".png"])
        self.assertEqual(cm.exception.status, 400)


class Routing(TestBase):
    def test_a_model_that_cannot_read_pictures_is_skipped(self):
        models = planner.router.registry().get("models", [])
        blind = [m["id"] for m in models if not m.get("vision") and m.get("provider") not in ("claude_code",)]
        if not blind:
            self.skipTest("every configured model can read pictures")
        choice = planner.choose_model("simple", "what is this", candidate_ids=blind, vision=True)
        self.assertIsNone(choice["model"])
        self.assertIn("can't read pictures", " ".join(choice["skipped"]))


class ForClaudeCode(UploadCase):
    def test_prompt_lists_the_files(self):
        img = uploads.save(PNG)["id"]
        text = uploads.prompt_for_agent("fix this layout", [img])
        self.assertTrue(text.startswith("fix this layout"))
        self.assertIn(str(uploads.path_of(img)), text)
        self.assertEqual(uploads.prompt_for_agent("no pictures", None), "no pictures")
        self.assertEqual(uploads.prompt_for_agent("gone", ["0" * 32 + ".png"]), "gone")

    def test_it_can_read_the_upload_folder_even_without_the_filesystem_tool(self):
        img = uploads.save(PNG)["id"]
        task = {"tools": ["web"], "images": [img]}
        args = execution.claude_args(task)
        self.assertEqual(args[-2:], ["--add-dir", str(uploads.UPLOAD_DIR)])
        denied = args[args.index("--disallowedTools") + 1].split(",")
        self.assertNotIn("Read", denied)
        self.assertIn("Write", denied)
        plain = execution.claude_args({"tools": ["web"]})
        self.assertNotIn("--add-dir", plain)
        self.assertIn("Read", plain[plain.index("--disallowedTools") + 1].split(","))


class ForApiModels(UploadCase):
    def test_thread_history_carries_pictures(self):
        img = uploads.save(PNG)["id"]
        root = db.add_task(self.project["id"], "look", 0, None, lane="api", model_id="gpt-6-sol", images=[img])
        db.mark_running(root["id"])
        db.mark_done(root["id"], "a cat")
        reply = db.add_task(self.project["id"], "what colour", 0, root["id"], lane="api", model_id="gpt-6-sol")
        msgs = worker._api_messages(db.get_task(reply["id"]))
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[0]["images"], [img])
        self.assertNotIn("images", msgs[2])

    def test_openai_and_gemini_get_image_parts_and_plain_text_is_unchanged(self):
        img = uploads.save(PNG)["id"]
        b64 = base64.b64encode(PNG).decode()
        msgs = [{"role": "user", "content": "hi", "images": [img]}, {"role": "assistant", "content": "ok"}]
        oa = providers._openai_input(msgs)
        self.assertEqual(oa[0]["content"][0], {"type": "input_text", "text": "hi"})
        self.assertEqual(oa[0]["content"][1], {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"})
        self.assertEqual(oa[1], {"role": "assistant", "content": "ok"})
        gm = providers._gemini_contents(msgs)
        self.assertEqual(gm[0]["parts"][1], {"inlineData": {"mimeType": "image/png", "data": b64}})
        self.assertEqual(gm[1], {"role": "model", "parts": [{"text": "ok"}]})

    def test_a_deleted_picture_is_a_clear_error(self):
        img = uploads.save(PNG)["id"]
        uploads.path_of(img).unlink()
        with self.assertRaises(providers.ProviderError):
            providers._openai_input([{"role": "user", "content": "hi", "images": [img]}])

    def test_the_text_only_cli_refuses_pictures(self):
        with self.assertRaises(providers.ProviderError):
            providers.claude_cli_complete(None, [{"role": "user", "content": "hi", "images": ["x"]}])


if __name__ == "__main__":
    unittest.main()
