"""Rich notes: the editor's document and the side panel's links/comments are kept with the note,
an outside text change drops the stale document, and only safe links are accepted."""

import tempfile
from pathlib import Path

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import api
import den_bin
import pad

OPS = [{"insert": "Buy milk"}, {"insert": "\n", "attributes": {"list": "unchecked"}},
       {"insert": "Call Sam"}, {"insert": "\n", "attributes": {"important": True}}]
META = {"links": [{"id": "l1", "url": "https://example.com/spec", "title": "Spec"}],
        "comments": [{"id": "c1", "text": "Check this", "at": "2026-10-20T10:00:00", "done": False}]}


class RichNotes(TestBase):
    def setUp(self):
        super().setUp()
        pad._ready = False

    def tearDown(self):
        pad._ready = False
        super().tearDown()

    def test_content_and_meta_round_trip(self):
        n = api.pad_create(api.PadNoteIn(title="Plan", body="- [ ] Buy milk\n!! Call Sam", content=OPS, meta=META))
        self.assertEqual(n["content"], OPS)
        self.assertEqual(n["meta"]["links"][0]["url"], "https://example.com/spec")
        n = api.pad_update(n["id"], api.PadNoteUpdateIn(title="Plan B"))
        self.assertEqual(n["content"], OPS)                       # untouched by a title change
        api.pad_delete(n["id"])
        item = next(i for i in den_bin.list_bin()["items"] if i["kind"] == "note")
        den_bin.restore("note", item["id"])
        back = pad.list_notes()[0]
        self.assertEqual((back["content"], back["meta"]), (OPS, META))

    def test_plain_notes_have_no_document(self):
        n = api.pad_create(api.PadNoteIn(title="Old", body="just text"))
        self.assertIsNone(n["content"])
        self.assertEqual(n["meta"], {})

    def test_text_change_without_document_drops_it(self):
        n = api.pad_create(api.PadNoteIn(body="a", content=OPS))
        n = api.pad_update(n["id"], api.PadNoteUpdateIn(body="rewritten elsewhere"))
        self.assertIsNone(n["content"])
        n = api.pad_update(n["id"], api.PadNoteUpdateIn(body="b", content=OPS))
        self.assertEqual(n["content"], OPS)

    def test_unsafe_links_refused(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "", "https://"):
            with self.assertRaises(HTTPException):
                api.pad_create(api.PadNoteIn(meta={"links": [{"id": "x", "url": url}]}))
        n = api.pad_create(api.PadNoteIn(meta={"links": [{"id": "m", "url": "mailto:sam@example.com"}]}))
        self.assertEqual(n["meta"]["comments"], [])

    def test_save_file_lists_loose_links(self):
        n = api.pad_create(api.PadNoteIn(title="Spec", body="See the doc", meta=META))
        with tempfile.TemporaryDirectory() as d:
            path = pad.save_file(pad.get_note(n["id"]), Path(d), None)
            self.assertIn("## Links\n\n- [Spec](https://example.com/spec)", path.read_text())
