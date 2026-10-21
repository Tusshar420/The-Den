"""dev_server module: working out a project's run command from its files."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("dev_server_module", Path(__file__).parent.parent / "modules" / "dev_server" / "module.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DetectTests(unittest.TestCase):
    def folder(self, files):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, text in files.items():
            Path(d.name, name).write_text(text)
        return d.name

    def test_npm_dev_script(self):
        p = self.folder({"package.json": json.dumps({"scripts": {"dev": "vite"}})})
        self.assertEqual(mod.detect(p, 4100)["command"], "npm run dev")

    def test_pnpm_when_lockfile(self):
        p = self.folder({"package.json": json.dumps({"scripts": {"dev": "next dev"}}), "pnpm-lock.yaml": ""})
        self.assertEqual(mod.detect(p, 4100)["command"], "pnpm dev")

    def test_start_script_when_no_dev(self):
        p = self.folder({"package.json": json.dumps({"scripts": {"start": "node ."}})})
        self.assertEqual(mod.detect(p, 4100)["command"], "npm start")

    def test_salesforce_style_project_has_nothing_to_run(self):
        p = self.folder({"package.json": json.dumps({"scripts": {"test": "jest"}})})
        self.assertIsNone(mod.detect(p, 4100))

    def test_php_and_static_html(self):
        self.assertIn("php -S", mod.detect(self.folder({"index.php": ""}), 4100)["command"])
        self.assertIn("http.server", mod.detect(self.folder({"index.html": ""}), 4100)["command"])

    def test_missing_folder(self):
        self.assertIsNone(mod.detect("/no/such/folder", 4100))


if __name__ == "__main__":
    unittest.main()
