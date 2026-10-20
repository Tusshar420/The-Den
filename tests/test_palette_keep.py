"""New task sheet "Keep" checkbox. The dashboard has no browser test harness, so this checks the wiring in the
source: the checkbox is rendered and toggles, a successful send with Keep clears the prompt and stays on the sheet,
unchecked still closes it and auto-opens the chat, and a failed send leaves the prompt alone."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static" / "js"
APP = (STATIC / "app.js").read_text()
SHEETS = (STATIC / "views" / "sheets.js").read_text()


def submit_palette():
    return APP[APP.index("async function submitPalette()"):APP.index("async function submitAsk()")]


class PaletteKeep(unittest.TestCase):
    def test_checkbox_is_rendered_and_toggles(self):
        self.assertRegex(SHEETS, r'role="checkbox" aria-checked="\$\{!!pal\.keep\}" data-action="palKeep"[^\n]*>Keep</button>')
        self.assertIn("palKeep: () => { const p = ui().palette; if (p) { p.keep = !p.keep;", APP)

    def test_keep_clears_prompt_and_stays_on_the_form(self):
        body = submit_palette()
        keep = body[body.index("if (pal.keep"):body.index("set({ sheet: null, palette: null })")]
        self.assertIn("text: ''", keep)
        self.assertIn("images: []", keep)
        self.assertIn("focus: 'palInput'", keep)
        self.assertNotIn("sheet: null", keep)
        self.assertNotIn("armAutoOpen", keep)

    def test_unchecked_keeps_existing_navigation(self):
        body = submit_palette()
        self.assertRegex(body, r"set\(\{ sheet: null, palette: null \}\);\s+if \(sentId != null\) armAutoOpen\(sentId\);")

    def test_errors_do_not_clear_the_prompt(self):
        body = submit_palette()
        catch = body[body.index("} catch (e)"):]
        self.assertIn("error = 'Could not send: '", catch)
        self.assertNotIn("text = ''", catch)
        # the prompt is only cleared after the awaited send has succeeded
        self.assertLess(body.index("await octoApi.send"), body.index("text: ''"))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_sources_parse(self):
        for name in ("app.js", "views/sheets.js"):
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "x.mjs"
                f.write_text((STATIC / name).read_text())
                r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
