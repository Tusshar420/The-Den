"""AI Providers page: your routing rules, the default model, and per-model settings."""

from unittest import mock

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import providers
import router


def api_model():
    return next(m for m in router.registry()["models"] if router.lane_for(m) == "api" and m.get("modality") != "image")


class Rules(TestBase):
    def setUp(self):
        super().setUp()
        self.m = api_model()
        self._avail = mock.patch.object(router, "availability", return_value=(True, "", None))
        self._avail.start()
        self.addCleanup(self._avail.stop)

    def test_first_matching_rule_wins(self):
        db.set_setting("routing_rules", [{"when": "mentions", "kw": "translate, hindi", "model": self.m["id"], "on": True},
                                         {"when": "mentions", "kw": "translate", "model": "claude-code", "on": True}])
        hit = router.matching_rule("Please translate this into Hindi")
        self.assertEqual(hit[0], 0)
        self.assertEqual(router.rule_model("translate it", None, "writing")[0], self.m["id"])

    def test_switched_off_and_non_matching_rules_are_skipped(self):
        db.set_setting("routing_rules", [{"when": "mentions", "kw": "summarize", "model": self.m["id"], "on": False},
                                         {"when": "is a coding task", "model": "claude-code", "on": True}])
        self.assertIsNone(router.matching_rule("summarize my notes", category="general"))
        self.assertEqual(router.matching_rule("fix the bug", category="coding")[0], 1)
        self.assertEqual(router.matching_rule("x", category="general", has_images=False), None)

    def test_default_model_only_for_answer_requests(self):
        db.set_setting("default_model", self.m["id"])
        self.assertEqual(router.rule_model("hello", None, "general")[0], self.m["id"])
        self.assertIsNone(router.rule_model("refactor", None, "coding"))

    def test_route_uses_rules_before_octopus(self):
        db.set_setting("routing_rules", [{"when": "mentions", "kw": "tl;dr", "model": self.m["id"], "on": True}])
        with mock.patch.object(router, "classify", return_value={"category": "general", "reason": "", "routed_by": "rules"}):
            out = router.route("tl;dr of the meeting")
        self.assertEqual((out["model_id"], out["routed_by"]), (self.m["id"], "rule"))


class ModelSettings(TestBase):
    def test_label_and_use_for_apply_to_built_in_models(self):
        m = api_model()
        db.set_setting("model_overrides", {m["id"]: {"label": "My model", "use_for": ["research"]}})
        self.assertEqual(router.get_model(m["id"])["label"], "My model")
        self.assertIn(m["id"], router.registry()["routing"]["categories"]["research"]["candidates"])

    def test_tuning_and_system_addon_reach_the_provider(self):
        m = {**api_model(), "provider": "openai", "model": "gpt-4.1-mini"}
        db.set_setting("model_overrides", {m["id"]: {"temperature": 0.3, "max_tokens": 900, "system_addon": "Be brief."}})
        seen = {}
        def fake(model, messages, instructions, timeout, web=False, temperature=None, max_tokens=None):
            seen.update(instructions=instructions, temperature=temperature, max_tokens=max_tokens)
            return providers.Completion("ok", 1, 1)
        with mock.patch.dict(providers.COMPLETERS, {"openai": fake}):
            self._old_complete(m, [{"role": "user", "content": "hi"}], "Base.")   # the real one, with a fake provider
        self.assertEqual(seen, {"instructions": "Base.\n\nBe brief.", "temperature": 0.3, "max_tokens": 900})


class CompatibleServers(TestBase):
    def test_add_a_mistral_style_model_and_call_it(self):
        import io
        import json
        import api_keys
        from octopus import models as model_admin
        self.addCleanup(lambda: api_keys.set_compat_key("x", None))
        m = model_admin.add({"label": "Mistral Large", "provider": "openai_compat", "model": "mistral-large-latest",
                             "base_url": "https://api.mistral.ai/v1/", "use_for": ["general"]})
        self.assertEqual(m["base_url"], "https://api.mistral.ai/v1")
        self.assertEqual(router.lane_for(router.get_model(m["id"])), "api")
        sent = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=0):
            sent.update(url=req.full_url, body=json.loads(req.data), auth=req.headers.get("Authorization"))
            return Resp(json.dumps({"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode())
        with mock.patch.object(api_keys, "compat_key", return_value="sk-test"), mock.patch("urllib.request.urlopen", fake_open):
            out = self._old_complete(router.get_model(m["id"]), [{"role": "user", "content": "hi"}], "Be brief.")
        self.assertEqual((out.text, out.input_tokens), ("OK", 3))
        self.assertEqual(sent["url"], "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(sent["body"]["messages"][0], {"role": "system", "content": "Be brief."})
        self.assertEqual(sent["auth"], "Bearer sk-test")

    def test_needs_an_address(self):
        from octopus import models as model_admin
        with self.assertRaises(model_admin.ModelError):
            model_admin.add({"label": "Local", "provider": "openai_compat", "model": "llama3.1:8b", "base_url": "not a url"})
