"""Models added on the AI Providers page (octopus/models.py), against the real server (see test_e2e.E2E)."""

import os
from unittest import mock

from tests import test_e2e

import api
import db
import router
from octopus import plan as planner

NOVA = {"label": "GPT Nova", "provider": "openai", "model": "gpt-6-nova", "use_for": ["writing", "general"],
        "coding": 3, "reasoning": 5, "cost": 2, "speed": 4, "vision": True, "price_per_mtok": {"input": 1, "output": 4}}


class CustomModels(test_e2e.E2E):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
                  mock.patch.object(router, "_llm_classify", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_the_new_endpoints_need_the_key(self):
        for method, path in (("POST", "/octopus/models"), ("PUT", "/octopus/models/x"), ("DELETE", "/octopus/models/x")):
            api._failures.clear()
            self.assertEqual(self.http(method, path, {}, key="wrong")["_status"], 401, path)

    def test_add_routes_it_and_it_shows_on_the_page(self):
        m = self.http("POST", "/octopus/models", NOVA)
        self.assertEqual((m["id"], m["custom"], m["lane"], m["available"], m["price_per_mtok"]),
                         ("gpt-6-nova", True, "api", True, {"input": 1.0, "output": 4.0}))
        cats = router.registry()["routing"]["categories"]
        # joins ahead of Claude Code, which stays the last resort
        self.assertEqual(cats["writing"]["candidates"][-2:], ["gpt-6-nova", "claude-code"])
        self.assertIn("gpt-6-nova", cats["general"]["candidates"])
        self.assertNotIn("gpt-6-nova", cats["research"]["candidates"])
        pv = self.http("GET", "/octopus/providers")
        openai = next(g for g in pv["providers"] if g["id"] == "openai")
        self.assertIn("gpt-6-nova", [x["id"] for x in openai["models"]])
        self.assertIn("gpt-6-nova", pv["categories"]["writing"]["candidates"])
        self.assertNotIn("coding", next(p for p in pv["add"]["providers"] if p["id"] == "openai")["categories"]["text"])
        choice = planner.choose_model("writing", "Write me an essay")
        self.assertIn("gpt-6-nova", [r["id"] for r in choice["candidates"]])
        # a second one with the same model name gets its own id
        self.assertEqual(self.http("POST", "/octopus/models", NOVA)["id"], "gpt-6-nova-2")

    def test_bad_forms_are_refused_with_the_reason(self):
        for body, needle in (({**NOVA, "label": ""}, "name"),
                             ({**NOVA, "provider": "mistral"}, "provider"),
                             ({**NOVA, "model": ""}, "model's name"),
                             ({**NOVA, "model": "../../evil"}, "letters"),
                             ({**NOVA, "model": "--dangerously-skip-permissions", "provider": "claude_code"}, "letters"),
                             ({**NOVA, "use_for": ["coding"]}, "can't be used for coding"),
                             ({**NOVA, "use_for": ["assistant"]}, "can't be used for assistant"),
                             ({**NOVA, "coding": 9}, "coding"),
                             ({**NOVA, "provider": "claude_code", "modality": "image"}, "text model")):
            r = self.http("POST", "/octopus/models", body)
            self.assertEqual(r["_status"], 400, body)
            self.assertIn(needle, r["detail"], body)
        self.assertFalse(db.get_setting("custom_models"))

    def test_image_and_claude_code_models(self):
        img = self.http("POST", "/octopus/models", {"label": "Imagen", "provider": "gemini", "model": "imagen-5", "modality": "image",
                                                     "use_for": ["image"], "price_per_image": 0.02})
        self.assertEqual((img["modality"], img["lane"], img["price_per_image"], img["coding"]), ("image", "api", 0.02, 0))
        self.assertIn(img["id"], router.registry()["routing"]["categories"]["image"]["candidates"])
        cc = self.http("POST", "/octopus/models", {"label": "Claude Code (Sonnet)", "provider": "claude_code", "model": "sonnet",
                                                    "use_for": ["coding", "general"]})
        self.assertEqual((cc["id"], cc["lane"], cc["scarce"], cc["vision"]), ("claude-code-sonnet", "agent", True, True))
        self.assertEqual(router.registry()["routing"]["categories"]["coding"]["candidates"], ["claude-code", "claude-code-sonnet"])

    def test_edit_switch_off_and_remove(self):
        mid = self.http("POST", "/octopus/models", NOVA)["id"]
        m = self.http("PUT", f"/octopus/models/{mid}", {**NOVA, "label": "Nova", "use_for": ["simple"], "note": "trial key"})
        self.assertEqual((m["id"], m["label"], m["_note"]), (mid, "Nova", "trial key"))
        cats = router.registry()["routing"]["categories"]
        self.assertIn(mid, cats["simple"]["candidates"])
        self.assertNotIn(mid, cats["writing"]["candidates"])
        # a change that doesn't mention the note keeps it
        self.assertEqual(self.http("PUT", f"/octopus/models/{mid}", {"speed": 5})["_note"], "trial key")
        off = self.http("PATCH", f"/octopus/models/{mid}", {"enabled": False})
        self.assertEqual((off["enabled"], off["available"]), (False, False))
        self.assertEqual(self.http("DELETE", f"/octopus/models/{mid}"), {"ok": True})
        self.assertIsNone(router.get_model(mid))
        self.assertNotIn(mid, db.get_setting("model_overrides"))
        self.assertEqual(self.http("DELETE", f"/octopus/models/{mid}")["_status"], 404)

    def test_models_json_ones_cant_be_edited_or_removed_here(self):
        self.assertEqual(self.http("PUT", "/octopus/models/gpt-6-luna", NOVA)["_status"], 409)
        self.assertEqual(self.http("DELETE", "/octopus/models/gpt-6-luna")["_status"], 409)
        self.assertIsNotNone(router.get_model("gpt-6-luna"))
