"""The plan builder: classification -> project -> agent -> model -> tools -> permissions -> review."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.helpers import TestBase

import db
import router
from assistant import capabilities as cap
from octopus import plan, registry

KEYS = {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}


class Registries(unittest.TestCase):
    def test_every_agent_category_exists_and_has_a_model_or_control(self):
        cats = router.registry()["routing"]["categories"]
        for agent in registry.agents():
            for c in agent["categories"]:
                self.assertIn(c, cats, f"{agent['id']} handles '{c}' but models.json has no such category")

    def test_every_agent_tool_and_permission_is_defined(self):
        for agent in registry.agents():
            for t in agent["tools"]:
                self.assertIsNotNone(registry.get_tool(t), f"{agent['id']} lists unknown tool {t}")
            for c in agent["permissions"]:
                self.assertIn(c, cap.CAPABILITIES, f"{agent['id']} lists unknown capability {c}")
        for t in registry.tools():
            for c in t["capabilities"]:
                self.assertIn(c, cap.CAPABILITIES, f"tool {t['id']} lists unknown capability {c}")

    def test_only_the_tools_a_request_needs(self):
        coding = registry.get_agent("coding")
        self.assertEqual([t["id"] for t in registry.select_tools(coding, "coding", "fix the login bug")],
                         ["filesystem", "git", "terminal"])
        self.assertIn("github", [t["id"] for t in registry.select_tools(coding, "coding", "fix it and open a PR")])
        self.assertEqual(registry.select_tools(registry.get_agent("writing"), "writing", "an essay"), [])

    def test_a_disabled_or_not_allowed_tool_is_never_given(self):
        # the browser tool is off (no MCP server yet) even though the words match
        self.assertNotIn("browser", [t["id"] for t in registry.select_tools(registry.get_agent("coding"), "coding", "open the browser")])


class Projects(TestBase):
    def test_find_project_by_name_variants_and_longest_wins(self):
        a = db.add_project("kode-aura", "/tmp/a")
        b = db.add_project("kode-aura-api", "/tmp/b")
        self.assertEqual(plan.find_project("fix login in Kode Aura")["id"], a["id"])
        self.assertEqual(plan.find_project("check kode-aura-api please")["id"], b["id"])
        self.assertIsNone(plan.find_project("fix the login bug"))
        self.assertIsNone(plan.find_project("kodeaurax is unrelated"))

    def test_general_is_never_a_match(self):
        self.assertIsNone(plan.find_project("something in General"))


class Planning(TestBase):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("KodeAura7", "/tmp/kodeaura7")
        env = mock.patch.dict(os.environ, KEYS)
        env.start()
        self.addCleanup(env.stop)
        no_llm = mock.patch.object(router, "_llm_classify", lambda *a, **k: None)   # keyword rules, no model call
        no_llm.start()
        self.addCleanup(no_llm.stop)

    def test_coding_request_becomes_the_coding_agent_on_claude_code(self):
        p = plan.build_plan("Fix the login bug in KodeAura7.")
        self.assertEqual((p["category"], p["agent"]["id"], p["model"]["id"], p["lane"]), ("coding", "coding", "claude-code", "agent"))
        self.assertEqual(p["project"]["name"], "KodeAura7")
        self.assertEqual([t["id"] for t in p["tools"]], ["filesystem", "git", "terminal"])
        self.assertFalse(p["show_plan"])
        self.assertIn("project files", p["reason"])

    def test_writing_request_uses_a_cheap_model_no_tools_no_project(self):
        p = plan.build_plan("Write me a 1500-word essay about AI in education.")
        self.assertEqual((p["category"], p["agent"]["id"], p["lane"]), ("writing", "writing", "api"))
        self.assertNotEqual(p["model"]["id"], "claude-code", "a writing task must not spend the Claude Code allowance")
        self.assertEqual(p["tools"], [])
        self.assertIsNone(p["needs_input"])

    def test_image_request_goes_to_the_creative_agent_and_image_model(self):
        p = plan.build_plan("Generate a futuristic image of an AI assistant.")
        self.assertEqual((p["category"], p["agent"]["id"], p["model"]["id"]), ("image", "creative", "gpt-image-1"))
        self.assertEqual([t["id"] for t in p["tools"]], ["image_generation"])
        self.assertEqual(p["est_cost_usd"], 0.04)

    def test_no_change_request_runs_claude_code_in_plan_mode(self):
        p = plan.build_plan("Check why the contact form is failing in KodeAura7, but don't change anything yet.")
        self.assertEqual((p["category"], p["permission_mode"], p["read_only"]), ("coding", "plan", True))
        self.assertIn("only investigate", p["reason"])

    def test_an_action_on_a_named_project_is_coding_and_creative_writing_is_writing(self):
        for text in ("Add dark mode to KodeAura7", "Update the footer in KodeAura7", "Speed up KodeAura7's build"):
            self.assertEqual(plan.build_plan(text)["category"], "coding", text)
        for text in ("Write a haiku about the sea", "Write a tagline for my bakery", "Draft a tweet about our launch"):
            self.assertEqual(plan.build_plan(text)["category"], "writing", text)
        self.assertEqual(plan.build_plan("What is the capital of France?")["category"], "simple")

    def test_diagnostic_wording_alone_is_a_coding_request(self):
        p = plan.build_plan("Why is the contact form failing?")
        self.assertEqual(p["category"], "coding")

    def test_risky_wording_shows_the_plan_and_flags_the_capability(self):
        p = plan.build_plan("Deploy KodeAura7 to production")
        self.assertEqual(p["risk"], "high")
        self.assertTrue(p["show_plan"])
        self.assertEqual({x["capability"] for x in p["permissions"] if x["flagged"]}, {"DEPLOY", "PRODUCTION"})

    def test_a_coding_request_with_no_project_asks_which_one(self):
        p = plan.build_plan("Fix the login bug")
        self.assertEqual(p["needs_input"]["kind"], "project")
        self.assertEqual([o["name"] for o in p["needs_input"]["options"]], ["KodeAura7"])
        self.assertTrue(p["show_plan"])
        with self.assertRaises(ValueError):
            plan.queue_plan(p)
        again = plan.with_project(p, self.proj["id"])
        self.assertIsNone(again["needs_input"])
        self.assertEqual(again["project"]["id"], self.proj["id"])

    def test_with_no_registered_project_it_falls_back_to_general(self):
        db.set_project_active(self.proj["id"], False)
        p = plan.build_plan("Fix the login bug")
        self.assertIsNone(p["needs_input"])
        self.assertEqual(p["project"]["name"], "General")

    def test_autonomy_changes_claude_code_mode_and_review(self):
        db.set_setting("autonomy", "assist")
        p = plan.build_plan("Fix the login bug in KodeAura7")
        self.assertEqual(p["permission_mode"], "plan")
        self.assertTrue(p["show_plan"])
        db.set_setting("autonomy", "execute")
        p = plan.build_plan("Fix the login bug in KodeAura7")
        self.assertEqual((p["permission_mode"], p["show_plan"]), ("acceptEdits", False))

    def test_plan_review_always_shows_every_plan(self):
        db.set_setting("plan_review", "always")
        self.assertTrue(plan.build_plan("Fix the login bug in KodeAura7")["show_plan"])
        self.assertTrue(plan.build_plan("Write an essay on tea")["show_plan"])

    def test_plan_review_never_skips_everyday_plans_but_not_risky_ones(self):
        db.set_setting("autonomy", "assist")
        self.assertTrue(plan.build_plan("Fix the login bug in KodeAura7")["show_plan"])
        db.set_setting("plan_review", "never")
        self.assertFalse(plan.build_plan("Fix the login bug in KodeAura7")["show_plan"])
        self.assertFalse(plan.build_plan("Clean up my downloads folder on this Mac")["show_plan"], "the System Agent's always-show is skipped too")
        self.assertTrue(plan.build_plan("Deploy KodeAura7 to production")["show_plan"], "risky still stops for you")
        self.assertTrue(plan.build_plan("Fix the login bug")["show_plan"], "so does a question Octopus needs answered")

    def test_a_denied_capability_blocks_the_plan(self):
        cap.set_policy("GIT_PUSH", cap.DENY)
        p = plan.build_plan("Fix the login bug in KodeAura7 and push it")
        self.assertIn("Push to a remote", p["blocked"])
        with self.assertRaises(PermissionError):
            plan.queue_plan(p)

    def test_you_can_override_the_model_and_the_agent(self):
        p = plan.build_plan("Write a haiku about rain", force_model="gpt-6-luna")
        self.assertEqual((p["model"]["id"], p["overrides"]), ("gpt-6-luna", {"model": "gpt-6-luna"}))
        self.assertIn("you picked model gpt-6-luna", p["route_reason"])
        p = plan.build_plan("Summarise this for me", force_agent="research")
        self.assertEqual((p["agent"]["id"], p["category"]), ("research", "research"))
        with self.assertRaises(ValueError):
            plan.build_plan("x", force_model="nope")

    def test_a_chat_continues_with_its_own_agent_model_and_project(self):
        root = db.add_task(self.proj["id"], "Check KodeAura7", 0, None, lane="agent", model_id="claude-code", category="coding",
                           agent_id="coding")
        p = plan.build_plan("Fix the second one", thread_root=db.get_task(root["id"]))
        self.assertEqual((p["category"], p["agent"]["id"], p["model"]["id"], p["project"]["id"], p["classified_by"]),
                         ("coding", "coding", "claude-code", self.proj["id"], "thread"))
        self.assertEqual(p["thread_id"], root["id"])
        self.assertFalse(p["show_plan"])

    def test_queue_plan_records_the_decision_on_the_task(self):
        p = plan.build_plan("Fix the login bug in KodeAura7")
        routing = db.add_routing(p["request"], p, status="queued")
        task = plan.queue_plan(p, routing_id=routing["id"])
        self.assertEqual((task["agent_id"], task["lane"], task["model_id"], task["permission_mode"], task["routing_id"]),
                         ("coding", "agent", "claude-code", "acceptEdits", routing["id"]))
        self.assertEqual(task["tools"], ["filesystem", "git", "terminal"])
        self.assertEqual(task["project_id"], self.proj["id"])


class Scoring(TestBase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, KEYS)
        env.start()
        self.addCleanup(env.stop)

    def ranks(self, category, text="hello"):
        c = plan.choose_model(category, text)
        return c["model"]["id"], {r["id"]: r for r in c["candidates"]}

    def test_a_simple_question_takes_the_cheap_fast_model_and_saves_claude(self):
        best, rows = self.ranks("simple")
        self.assertIn(best, ("gpt-6-luna", "gemini-3.8-flash"))
        self.assertLess(rows["claude-code"]["score"], rows[best]["score"])
        self.assertIn("scarcity", rows["claude-code"]["parts"])

    def test_writing_prefers_strength_over_the_absolute_cheapest(self):
        best, rows = self.ranks("writing")
        self.assertNotEqual(best, "claude-code")
        self.assertGreater(rows["gemini-3.8-flash"]["score"], rows["gpt-6-luna"]["score"])

    def test_a_busy_claude_window_pushes_it_further_down(self):
        _, before = self.ranks("general")
        soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.set_setting("claude_usage", {"windows": {"five_hour": {"utilization": 0.95, "resets_at": soon}}})
        _, after = self.ranks("general")
        self.assertLess(after["claude-code"]["score"], before["claude-code"]["score"])

    def test_track_record_moves_the_score(self):
        _, before = self.ranks("general")
        for _ in range(4):
            t = db.add_task(self.project["id"], "q", 0, None, lane="api", model_id="gpt-6-luna", category="general")
            db.mark_running(t["id"])
            db.mark_error(t["id"], "boom", needs_review=False)
        _, after = self.ranks("general")
        self.assertLess(after["gpt-6-luna"]["score"], before["gpt-6-luna"]["score"])
        self.assertEqual(after["gpt-6-luna"]["history"]["done"], 0)

    def test_rate_limited_and_unready_models_are_skipped_with_the_reason(self):
        db.set_model_rate_limited("gemini-3.8-flash", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "429")
        c = plan.choose_model("writing", "essay")
        self.assertNotEqual(c["model"]["id"], "gemini-3.8-flash")
        self.assertTrue(any("Gemini 3.8 Flash (rate-limited" in s for s in c["skipped"]))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}):
            c = plan.choose_model("writing", "essay")
            self.assertEqual(c["model"]["id"], "claude-code", "the last-resort fallback still answers")

    def test_a_non_transferable_category_waits_instead_of_hopping(self):
        # Claude Code's limit shows up as a rate-limited task in the agent lane
        t = db.add_task(self.project["id"], "big job", 0, None)
        db.mark_running(t["id"])
        db.mark_rate_limited(t["id"], (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        c = plan.choose_model("coding", "fix it")
        self.assertEqual(c["model"]["id"], "claude-code")
        self.assertTrue(c["waiting"])

    def test_research_needs_a_model_that_can_search(self):
        real = router.registry
        with mock.patch.object(router, "registry", lambda: _without_web(real())):
            c = plan.choose_model("research", "latest react version")
        self.assertEqual(c["model"]["id"], "claude-code", "only Claude Code (with its WebSearch tool) can still do it")
        self.assertTrue(any("can't web search" in s for s in c["skipped"]))

    def test_a_request_too_big_for_a_model_is_skipped(self):
        big = "x" * 4_000_000
        c = plan.choose_model("general", big)
        self.assertNotIn("gpt-6-luna", [r["id"] for r in c["candidates"] if r["available"]])

    def test_cost_estimate_uses_word_count_and_price(self):
        sol = router.get_model("gpt-6-sol")
        est = plan.estimate_cost(sol, "Write a 1000-word essay", "writing")
        self.assertAlmostEqual(est, (len("Write a 1000-word essay") / 3 * 2.0 + 1400 * 10.0) / 1_000_000, places=4)
        self.assertIsNone(plan.estimate_cost(router.get_model("claude-code"), "x", "coding"))
        self.assertIsNone(plan.estimate_cost(router.get_model("gemini-3.8-flash"), "x", "writing"))


def _without_web(reg):
    reg = dict(reg)
    reg["models"] = [{**m, "web_search": False} if m["id"] != "claude-code" else m for m in reg["models"]]
    return reg


if __name__ == "__main__":
    unittest.main()
