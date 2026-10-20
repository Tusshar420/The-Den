"""Den 2.1 workspaces are working areas inside one Den: add one, put a project in it, and deleting it moves
its projects instead of losing them."""

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import api
import db
import den_areas as da


class Areas(TestBase):
    def test_add_use_rename_delete(self):
        a = da.add(da.AreaIn(name="Cmentor"))
        self.assertEqual(a["id"], "cmentor")
        self.assertIn("cmentor", db.project_categories())
        p = api.create_project(api.ProjectIn(name="Portal", category="cmentor"))
        p = p.get("project", p)
        self.assertEqual(db.get_project(p["id"])["category"], "cmentor")
        self.assertEqual(next(x for x in da.view() if x["id"] == "cmentor")["projects"], 1)
        self.assertEqual(da.update("cmentor", da.AreaPatch(name="Cmentor Ltd"))["name"], "Cmentor Ltd")
        out = da.remove("cmentor", "work")
        self.assertEqual((out["moved"], out["to"]), (1, "work"))
        self.assertEqual(db.get_project(p["id"])["category"], "work")
        self.assertNotIn("cmentor", db.project_categories())

    def test_unknown_workspace_is_refused(self):
        with self.assertRaises(HTTPException):
            api.create_project(api.ProjectIn(name="X", category="nowhere"))

    def test_names_are_unique_and_one_must_stay(self):
        with self.assertRaises(HTTPException):
            da.add(da.AreaIn(name="work"))
        da.remove("work", "personal")
        with self.assertRaises(HTTPException):
            da.remove("personal", None)
