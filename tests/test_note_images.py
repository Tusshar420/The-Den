"""Den 2.1 Notes: a note keeps its pictures (upload ids) - checked, de-duplicated, and back after the Recycle bin."""

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import api
import den_bin
import pad
import uploads

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


class NoteImages(TestBase):
    def setUp(self):
        super().setUp()
        pad._ready = False                     # pad makes its table on first use - in this test's database too

    def tearDown(self):
        pad._ready = False
        super().tearDown()

    def test_pictures_round_trip(self):
        up = uploads.save(PNG)
        n = api.pad_create(api.PadNoteIn(title="Board", images=[up["id"], up["id"]]))
        self.assertEqual(n["images"], [up["id"]])
        self.assertEqual(api.pad_update(n["id"], api.PadNoteUpdateIn(images=[]))["images"], [])
        with self.assertRaises(HTTPException):
            api.pad_create(api.PadNoteIn(images=["not-an-upload"]))
        api.pad_update(n["id"], api.PadNoteUpdateIn(images=[up["id"]]))
        api.pad_delete(n["id"])
        item = next(i for i in den_bin.list_bin()["items"] if i["kind"] == "note")
        den_bin.restore("note", item["id"])
        self.assertEqual(pad.list_notes()[0]["images"], [up["id"]])
