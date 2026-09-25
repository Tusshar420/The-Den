"""Octopus's HTTP endpoints, against the real server (see test_e2e.E2E)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_e2e

import api
import db
import providers
import router


class OctopusApi(test_e2e.E2E):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("KodeAura7", tempfile.mkdtemp())
        for p in (mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
                  mock.patch.object(router, "_llm_classify", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_every_new_endpoint_needs_the_key(self):
        for method, path in (("POST", "/octopus/plan"), ("POST", "/octopus/plans/1/run"), ("POST", "/octopus/plans/1/cancel"),
                             ("GET", "/routing"), ("GET", "/routing/1"), ("GET", "/octopus/overview"), ("GET", "/octopus/agents"),
                             ("GET", "/octopus/tools"), ("GET", "/octopus/providers"), ("PATCH", "/octopus/models/gpt-6-luna"),
                             ("GET", "/tasks/1/detail"), ("GET", "/tasks/1/diff"), ("GET", "/octopus/images/x.png")):
            api._failures.clear()          # this checks each endpoint has the key check, not the lockout
            self.assertEqual(self.http(method, path, {}, key="wrong")["_status"], 401, path)

    def test_request_plan_run_and_history(self):
        sent = self.http("POST", "/octopus", {"text": "Deploy KodeAura7 to production", "source": "dashboard"})
        self.assertEqual((sent["status"], sent["task"]), ("proposed", None))
        self.assertEqual(sent["plan"]["risk"], "high")
        self.assertEqual(sent["decision"]["agent"], "Coding Agent")
        rid = sent["routing_id"]
        self.assertEqual([r["id"] for r in self.http("GET", "/routing?status=proposed")], [rid])
        ran = self.http("POST", f"/octopus/plans/{rid}/run", {})
        self.assertEqual(ran["status"], "queued")
        self.assertEqual(self.http("POST", f"/octopus/plans/{rid}/run", {})["_status"], 409)
        self.assertEqual(self.http("POST", f"/octopus/plans/{rid}/cancel")["_status"], 409)
        row = self.http("GET", f"/routing/{rid}")
        self.assertEqual((row["status"], row["agent_label"], row["model_label"], row["task"]["id"]),
                         ("queued", "Coding Agent", "Claude Code", ran["task"]["id"]))
        self.assertEqual(self.http("GET", "/routing/9999")["_status"], 404)
        self.assertEqual(self.http("GET", "/routing?category=writing"), [])

    def test_the_dry_run_creates_nothing(self):
        plan = self.http("POST", "/octopus/plan", {"text": "Write me an essay about AI in education", "model": "gpt-6-luna"})
        self.assertEqual((plan["category"], plan["agent"]["id"], plan["model"]["id"], plan["overrides"]), ("writing", "writing", "gpt-6-luna", {"model": "gpt-6-luna"}))
        self.assertEqual(self.http("GET", "/routing"), [])
        self.assertEqual(self.http("GET", "/tasks"), [])
        self.assertEqual(self.http("POST", "/octopus/plan", {"text": "  "})["_status"], 400)
        self.assertEqual(self.http("POST", "/octopus/plan", {"text": "hi", "project_id": 999})["_status"], 404)

    def test_control_command_over_http_and_the_voice_result_endpoint(self):
        sent = self.http("POST", "/octopus", {"text": "what's happening?", "source": "voice"})
        self.assertEqual((sent["status"], sent["task"]["lane"]), ("done", "control"))
        result = self.http("GET", f"/octopus/result/{sent['task']['id']}")
        self.assertEqual((result["finished"], result["text"]), (True, sent["result"]["text"]))
        self.assertEqual(result["spoken"], sent["result"]["spoken"])

    def test_overview_agents_tools_providers(self):
        self.http("POST", "/octopus", {"text": "Fix the login bug in KodeAura7"})
        ov = self.http("GET", "/octopus/overview")
        self.assertEqual(set(ov), {"autonomy", "plan_review", "current", "queue", "approvals", "plans_waiting", "activity", "providers", "claude_window"})
        self.assertEqual((ov["autonomy"], ov["queue"]["waiting"]), ("execute", 1))
        agents = {a["id"]: a for a in self.http("GET", "/octopus/agents")}
        self.assertEqual(set(agents), {"coding", "writing", "research", "creative", "general", "personal", "operator", "system"})
        self.assertEqual([m["id"] for m in agents["coding"]["preferred_models"]], ["claude-code"])
        self.assertTrue(any(p["capability"] == "GIT_PUSH" and p["policy"] == "ask" for p in agents["coding"]["permissions"]))
        tools = {t["id"]: t for t in self.http("GET", "/octopus/tools")}
        self.assertFalse(tools["browser"]["enabled"])
        self.assertIn("coding", tools["git"]["agents"])
        pv = self.http("GET", "/octopus/providers")
        by_id = {g["id"]: g for g in pv["providers"]}
        self.assertEqual([g["id"] for g in pv["providers"][:3]], ["claude_code", "openai", "gemini"])
        self.assertTrue(by_id["openai"]["ready"])
        luna = next(m for m in by_id["openai"]["models"] if m["id"] == "gpt-6-luna")
        self.assertEqual((luna["available"], luna["usage"]["tasks"]), (True, 0))
        self.assertIn("writing", pv["categories"])

    def test_switching_a_model_off_from_the_ui_takes_it_out_of_routing(self):
        off = self.http("PATCH", "/octopus/models/gemini-3.8-flash", {"enabled": False})
        self.assertFalse(off["enabled"] and off["available"])
        self.assertIn("disabled", off["unavailable_reason"])
        plan = self.http("POST", "/octopus/plan", {"text": "Write an essay on tea"})
        self.assertNotEqual(plan["model"]["id"], "gemini-3.8-flash")
        self.assertEqual(self.http("PATCH", "/octopus/models/gemini-3.8-flash", {"enabled": True})["available"], True)
        self.assertEqual(self.http("PATCH", "/octopus/models/nope", {"enabled": True})["_status"], 404)

    def test_task_detail(self):
        sent = self.http("POST", "/octopus", {"text": "Fix the login bug in KodeAura7"})
        tid = sent["task"]["id"]
        d = self.http("GET", f"/tasks/{tid}/detail")
        self.assertEqual((d["agent"]["label"], d["model"]["label"], d["project"]["name"]), ("Coding Agent", "Claude Code", "KodeAura7"))
        self.assertEqual([t["label"] for t in d["tools"]], ["Filesystem", "Git", "Terminal"])
        self.assertEqual(d["routing"]["id"], sent["routing_id"])
        self.assertEqual(d["events"][0]["text"], "Received request")
        self.assertEqual((d["plan_steps"], d["progress"], d["files"], d["git"], d["approvals"]), ([], None, [], None, []))
        self.assertEqual(self.http("GET", "/tasks/9999/detail")["_status"], 404)
        self.assertFalse(self.http("GET", f"/tasks/{tid}/diff")["available"])

    def test_task_detail_shows_progress_and_approvals(self):
        tid = self.http("POST", "/octopus", {"text": "Fix the login bug in KodeAura7"})["task"]["id"]
        db.set_task_plan_steps(tid, [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"},
                                     {"content": "c", "status": "pending"}, {"content": "d", "status": "pending"}])
        db.create_approval(tid, "Bash", '{"command": "git push"}')
        d = self.http("GET", f"/tasks/{tid}/detail")
        self.assertEqual(d["progress"], {"done": 1, "total": 4, "percent": 25})
        self.assertEqual([(a["tool_name"], a["status"], a["describe"]) for a in d["approvals"]], [("Bash", "pending", "git push")])

    def test_settings_autonomy_and_plan_review(self):
        s = self.http("PATCH", "/settings", {"autonomy": "assist", "plan_review": "always"})
        self.assertEqual((s["autonomy"], s["plan_review"]), ("assist", "always"))
        self.assertEqual(self.http("PATCH", "/settings", {"autonomy": "reckless"})["_status"], 422)
        sent = self.http("POST", "/octopus", {"text": "Write an essay on tea"})
        self.assertEqual(sent["status"], "proposed")

    def test_images_are_served_safely(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(providers, "IMAGES_DIR", Path(d)):
            (Path(d) / "cat.png").write_bytes(b"\x89PNG")
            self.assertEqual(self.http("GET", "/octopus/images/nope.png")["_status"], 404)
            self.assertIn(self.http("GET", "/octopus/images/..%2Fsecrets")["_status"], (400, 404))   # never served
            self.assertEqual(self.http("GET", "/octopus/images/a$b.png")["_status"], 400)


if __name__ == "__main__":
    unittest.main()
