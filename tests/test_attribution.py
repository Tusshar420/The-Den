"""Which Claude account(s) and model(s) ran a chat - shown from what its tasks recorded (static/js/format.js
attribution helpers, run under node) and that the backend records and serves those fields."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import db
from tests.helpers import TestBase

FORMAT_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "format.js"

SCRIPT = """
import { attribution, attributionParts } from './format.mjs';
const cases = JSON.parse(process.argv[2]);
const model = id => 'M:' + id, acct = id => 'A:' + id;
console.log(JSON.stringify(cases.map(c => { const a = attribution(c); return [a, attributionParts(a, model, acct)]; })));
"""


def run(cases):
    # The dashboard has no package.json, so node only treats the file as an ES module under a .mjs name.
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "format.mjs").write_text(FORMAT_JS.read_text())
        (Path(d) / "run.mjs").write_text(SCRIPT)
        out = subprocess.run(["node", str(Path(d) / "run.mjs"), json.dumps(cases)],
                             capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr)
    out = out.stdout
    return json.loads(out)


def t(model, account=None, lane="agent", started="2026-01-01T00:00:00"):
    return {"model_id": model, "claude_account": account, "lane": lane, "started_at": started}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class AttributionJs(unittest.TestCase):
    def test_cases(self):
        r = run([
            [t("claude-code", "work")],
            [t("claude-code", "work"), t("gpt-6", lane="api"), t("claude-code", "personal"), t("gpt-6", lane="api")],
            [t("claude-code")],                                   # history from before accounts were stored
            [t("claude-code", "work"), t("claude-code")],         # mixed
            [t("gpt-6", lane="api")],                             # no Claude account involved at all
            [t("claude-code", "work", started=None)],             # never started: not counted
            [],
        ])
        self.assertEqual(r[0][1], {"models": "M:claude-code", "account": "Account: A:work"})
        self.assertEqual(r[1][0]["models"], ["claude-code", "gpt-6"])           # all models, first-use order, no repeats
        self.assertEqual(r[1][1], {"models": "M:claude-code · M:gpt-6", "account": "Accounts: A:work, A:personal"})
        self.assertEqual(r[2][1]["account"], "Account not recorded")
        self.assertEqual(r[3][1]["account"], "Account: A:work + unrecorded")
        self.assertEqual(r[4][1], {"models": "M:gpt-6", "account": ""})
        self.assertEqual(r[5][1], {"models": "", "account": ""})
        self.assertEqual(r[6][1], {"models": "", "account": ""})


class RecordedFields(TestBase):
    def test_account_and_models_reach_the_thread(self):
        p = db.add_project("p", "/tmp/attrib-p")
        root = db.add_task(p["id"], "hi", lane="agent", model_id="claude-code")
        self.assertTrue(db.claim_task(root["id"], "acct-1"))
        reply = db.add_task(p["id"], "again", thread_id=root["id"], lane="api", model_id="gpt-6")
        old = db.add_task(p["id"], "legacy", lane="agent", model_id="claude-code")
        got = {x["id"]: x for x in db.get_thread(root["id"])}
        self.assertEqual(got[root["id"]]["claude_account"], "acct-1")
        self.assertEqual((got[reply["id"]]["model_id"], got[reply["id"]]["claude_account"]), ("gpt-6", None))
        self.assertIsNone(db.get_task(old["id"])["claude_account"])


if __name__ == "__main__":
    unittest.main()
