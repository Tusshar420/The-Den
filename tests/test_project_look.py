"""A project's colour and picture: checked, stored safely, replaced and removed cleanly."""

import unittest
from pathlib import Path

from fastapi import HTTPException

import api
import crm
import db
import project_images
from tests.helpers import TestBase

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class LookCase(TestBase):
    def setUp(self):
        super().setUp()
        old = project_images.IMAGE_DIR
        project_images.IMAGE_DIR = Path(self._tmp.name) / "images"
        self.addCleanup(lambda: setattr(project_images, "IMAGE_DIR", old))
        self.pid = db.add_project("shop", self._tmp.name)["id"]

    def test_color_checked_and_saved(self):
        self.assertEqual(crm.clean({"color": " #ABCDEF "}, {}), {"color": "#abcdef"})
        self.assertEqual(crm.clean({"color": ""}, {}), {"color": None})
        for bad in ("red", "#fff", "#12345g", 5):
            with self.assertRaises(crm.RecordError):
                crm.clean({"color": bad}, {})
        db.update_project_record(self.pid, {"color": "#abcdef"})
        self.assertEqual(db.get_project(self.pid)["color"], "#abcdef")

    def test_image_not_settable_through_the_record(self):
        with self.assertRaises(crm.RecordError):
            crm.clean({"image": "x.png"}, {})

    def test_save_replace_remove(self):
        name = project_images.save(self.pid, PNG)
        self.assertTrue(name.endswith(".png"))
        db.update_project_record(self.pid, {"image": name})
        out = api.delete_project_image(self.pid)
        self.assertIsNone(out["image"])
        self.assertEqual(list(project_images.IMAGE_DIR.iterdir()), [])

    def test_only_pictures_accepted(self):
        for bad in (b"", b"<svg onload=alert(1)>", b"%PDF-1"):
            with self.assertRaises(ValueError):
                project_images.save(self.pid, bad)
        with self.assertRaises(ValueError):
            project_images.save(self.pid, PNG + b"0" * project_images.MAX_BYTES)

    def test_remove_ignores_odd_names(self):
        project_images.remove("../../x.png")
        project_images.remove(None)

    def test_unknown_project(self):
        with self.assertRaises(HTTPException):
            api.delete_project_image(99999)


if __name__ == "__main__":
    unittest.main()
