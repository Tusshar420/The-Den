"""Settings > Sites: learning a website from a link, and listing what was learned."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_e2e

import api
import db
from octopus import sites


class Urls(unittest.TestCase):
    def test_bare_domain_gets_https(self):
        self.assertEqual(sites.normalize_url("example.com/a"), "https://example.com/a")

    def test_refuses_private_and_odd_links(self):
        for bad in ("", "ftp://example.com", "http://localhost:8787", "http://127.0.0.1", "http://192.168.1.5",
                    "https://user:pw@example.com", "https://nodot", "https://printer.local"):
            with self.assertRaises(sites.SiteError, msg=bad):
                sites.normalize_url(bad)


class SitesApi(test_e2e.E2E):
    def setUp(self):
        super().setUp()
        self.skills = Path(tempfile.mkdtemp())
        p = mock.patch.object(sites, "SKILLS_DIR", self.skills)
        p.start()
        self.addCleanup(p.stop)

    def test_needs_the_key(self):
        for method, path in (("GET", "/octopus/sites"), ("POST", "/octopus/sites/learn")):
            api._failures.clear()
            self.assertEqual(self.http(method, path, {}, key="wrong")["_status"], 401, path)

    def test_lists_learned_sites(self):
        d = self.skills / "site-demo"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: site-demo\ndescription: A 3D demo\n---\n# Demo\n- URL: https://demo.test\n- Analyzed: 2026-10-02\n")
        (self.skills / "other").mkdir()
        (self.skills / "other" / "SKILL.md").write_text("---\nname: other\n---\n")
        got = self.http("GET", "/octopus/sites")
        self.assertEqual([(s["id"], s["url"], s["analyzed"], s["description"]) for s in got],
                         [("site-demo", "https://demo.test", "2026-10-02", "A 3D demo")])

    def test_learn_queues_a_task(self):
        r = self.http("POST", "/octopus/sites/learn", {"url": "example.com"})
        self.assertEqual(r["url"], "https://example.com")
        task = db.get_task(r["task"]["id"])
        self.assertIn("learn-website", task["prompt"])
        self.assertIn("https://example.com", task["prompt"])

    def test_learn_refuses_a_private_link(self):
        r = self.http("POST", "/octopus/sites/learn", {"url": "http://localhost:8787"})
        self.assertEqual(r["_status"], 400)


if __name__ == "__main__":
    unittest.main()
