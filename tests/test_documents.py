"""Project documents: stored safely on disk, listed per project, downloadable, deletable."""

import asyncio
import unittest
from pathlib import Path

from fastapi import HTTPException

import api
import db
import documents
from tests.helpers import TestBase


class DocumentCase(TestBase):
    def setUp(self):
        super().setUp()
        old = documents.DOCUMENT_DIR
        documents.DOCUMENT_DIR = Path(self._tmp.name) / "documents"
        self.addCleanup(lambda: setattr(documents, "DOCUMENT_DIR", old))
        self.pid = db.add_project("shop", self._tmp.name)["id"]

    def test_save_list_download_delete(self):
        doc = documents.save(self.pid, "spec.pdf", "application/pdf", b"%PDF-1")
        self.assertEqual((doc["name"], doc["size"]), ("spec.pdf", 6))
        self.assertNotIn("stored", doc)
        self.assertEqual([d["id"] for d in db.list_project_documents(self.pid)], [doc["id"]])
        self.assertEqual(documents.path_of(db.get_project_document(doc["id"])).read_bytes(), b"%PDF-1")
        api.delete_document(doc["id"])
        self.assertEqual(db.list_project_documents(self.pid), [])
        self.assertEqual(list((documents.DOCUMENT_DIR / str(self.pid)).iterdir()), [])

    def test_names_cannot_escape_or_be_empty(self):
        self.assertEqual(documents.clean_name("../../etc/passwd"), "passwd")
        self.assertEqual(documents.clean_name("C:\\x\\y.txt"), "y.txt")
        self.assertEqual(documents.clean_name("  "), "document")
        self.assertLessEqual(len(documents.clean_name("a" * 500 + ".pdf")), 120)

    def test_empty_and_oversize_refused(self):
        with self.assertRaises(ValueError):
            documents.save(self.pid, "a.txt", "", b"")
        with self.assertRaises(ValueError):
            documents.save(self.pid, "a.txt", "", b"x" * (documents.MAX_BYTES + 1))

    def test_documents_are_per_project(self):
        other = db.add_project("blog", self._tmp.name + "/blog")["id"]
        documents.save(self.pid, "a.txt", "", b"a")
        self.assertEqual(db.list_project_documents(other), [])

    def test_unknown_project_and_document_404(self):
        with self.assertRaises(HTTPException) as e:
            api.list_documents(9999)
        self.assertEqual(e.exception.status_code, 404)
        with self.assertRaises(HTTPException) as e:
            api.download_document(9999)
        self.assertEqual(e.exception.status_code, 404)

    def test_download_is_an_attachment(self):
        doc = documents.save(self.pid, "page.html", "text/html", b"<script>1</script>")
        res = api.download_document(doc["id"])
        self.assertEqual(res.media_type, "application/octet-stream")
        self.assertIn("attachment", res.headers["content-disposition"])


class TaskAccessCase(DocumentCase):
    """What a task working on the project is given."""

    def test_links_carry_the_real_names(self):
        a = documents.save(self.pid, "spec.pdf", "application/pdf", b"%PDF-1")
        b = documents.save(self.pid, "Spec.pdf", "application/pdf", b"%PDF-2")
        docs = documents.readable(self.pid)
        names = {d["id"]: p.name for d, p in docs}
        self.assertEqual(names, {a["id"]: "spec.pdf", b["id"]: "Spec (2).pdf"})
        self.assertEqual({p.read_bytes() for _, p in docs}, {b"%PDF-1", b"%PDF-2"})
        api.delete_document(a["id"])
        self.assertEqual([p.name for _, p in documents.readable(self.pid)], ["Spec (2).pdf"])
        view = documents.folder_of(self.pid) / documents.READABLE_DIR
        self.assertEqual([x.name for x in view.iterdir()], ["Spec (2).pdf"])

    def test_claude_code_is_told_and_may_open_the_folder(self):
        from octopus import execution
        documents.save(self.pid, "brief.md", "", b"# Brief")
        args = execution.claude_args({"project_id": self.pid})
        prompt = args[args.index("--append-system-prompt") + 1]
        self.assertIn("brief.md", prompt)
        self.assertIn(str(documents.folder_of(self.pid) / "files" / "brief.md"), prompt)
        self.assertEqual(args[-2:], ["--add-dir", str(documents.folder_of(self.pid))])

    def test_no_documents_or_no_file_tools_means_nothing_added(self):
        from octopus import execution
        self.assertNotIn("--add-dir", execution.claude_args({"project_id": self.pid}))
        documents.save(self.pid, "brief.md", "", b"# Brief")
        args = execution.claude_args({"project_id": self.pid, "tools": ["web"]})
        self.assertNotIn("--add-dir", args)
        self.assertFalse(any("brief.md" in a for a in args))

    def test_api_models_get_the_text_of_text_documents(self):
        documents.save(self.pid, "notes.txt", "text/plain", b"use blue buttons")
        documents.save(self.pid, "deck.pdf", "application/pdf", b"%PDF-1")
        documents.save(self.pid, "data.json", "", b"\x00binary")
        text = documents.prompt_for_api(self.pid)
        self.assertIn("=== notes.txt\nuse blue buttons", text)
        self.assertIn("can't open here: data.json, deck.pdf", text)
        self.assertEqual(documents.prompt_for_api(None), "")

    def test_api_text_is_capped(self):
        documents.save(self.pid, "big.txt", "", b"x" * (documents.TEXT_MAX_CHARS + 500))
        documents.save(self.pid, "later.txt", "", b"y")
        text = documents.prompt_for_api(self.pid)
        self.assertIn("big.txt (cut short)", text)
        self.assertLess(len(text), documents.TEXT_MAX_CHARS + 500)


if __name__ == "__main__":
    unittest.main()
