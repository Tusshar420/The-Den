"""Den 2.1 Projects: links with a pinned flag, saved beside the project record."""

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

from fastapi import HTTPException

import db
import den_project_links
from den_project_links import LinksIn


class ProjectLinks(TestBase):
    def setUp(self):
        super().setUp()
        den_project_links._ready = False
        self.site = db.add_project("Site", self._tmp.name + "/site")
        den_project_links.build_router(lambda: None)        # the routes build without the live app

    def get(self):
        return den_project_links.links(self.site["id"])["links"]

    def put(self, links, pid=None):
        return den_project_links.save(pid or self.site["id"], LinksIn(links=links))["links"]

    def refused(self, fn):
        with self.assertRaises(HTTPException) as e:
            fn()
        return e.exception.status_code

    def test_existing_links_are_pinned_by_kind(self):
        db.update_project_record(self.site["id"], {"links": [{"label": "Repo", "url": "https://github.com/me/site"},
                                                             {"label": "Docs", "url": "https://docs.site.dev"}]})
        out = self.get()
        self.assertEqual([(x["kind"], x["pinned"]) for x in out], [("GitHub", True), ("Docs", False)])

    def test_save_keeps_the_record_and_the_pins(self):
        body = {"links": [{"label": "GitHub", "url": "github.com/me/site", "pinned": False},
                          {"label": "Docs", "url": "https://docs.site.dev", "pinned": True},
                          {"label": "", "url": "https://example.com/x"}]}
        out = self.put(body["links"])
        self.assertEqual([(x["url"], x["pinned"]) for x in out],
                         [("https://github.com/me/site", False), ("https://docs.site.dev", True), ("https://example.com/x", False)])
        self.assertEqual(out[2]["kind"], "Other")
        # The record itself still holds plain {label, url} - what the CRM and reports read.
        self.assertEqual(db.get_project(self.site["id"])["links"][0], {"label": "GitHub", "url": "https://github.com/me/site"})
        # Removing a link drops its pin too.
        self.put([body["links"][1]])
        self.assertEqual(den_project_links._pins(self.site["id"]), {"https://docs.site.dev": True})

    def test_a_bad_url_changes_nothing(self):
        db.update_project_record(self.site["id"], {"links": [{"label": "Docs", "url": "https://docs.site.dev"}]})
        bad = [{"label": "Live site", "url": "javascript:alert(1)", "pinned": True}]
        self.assertEqual(self.refused(lambda: self.put(bad)), 400)
        self.assertEqual(len(db.get_project(self.site["id"])["links"]), 1)

    def test_general_and_missing_projects_are_refused(self):
        self.assertEqual(self.refused(lambda: self.put([], pid=self.project["id"])), 400)
        self.assertEqual(self.refused(lambda: den_project_links.links(9999)), 404)
