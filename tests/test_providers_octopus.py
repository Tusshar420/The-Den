"""Web-search grounding and image generation in providers.py, against faked HTTP (no keys, no network)."""

import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import providers

PNG = base64.b64encode(b"\x89PNG-fake").decode()


class FakeResponse(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Web(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k", "GEMINI_API_KEY": "k"})
        env.start()
        self.addCleanup(env.stop)

    def sent(self, urlopen):
        return json.loads(urlopen.call_args[0][0].data)

    def test_openai_gets_the_web_search_tool_only_when_asked(self):
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: FakeResponse({"output_text": "hi", "usage": {}})) as u:
            providers.openai_complete("m", [{"role": "user", "content": "x"}])
            self.assertNotIn("tools", self.sent(u))
            providers.complete({"id": "m", "provider": "openai", "model": "m"}, [{"role": "user", "content": "x"}], web=True)
            self.assertEqual(self.sent(u)["tools"], [{"type": "web_search"}])

    def test_gemini_grounding_lists_its_sources(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "React 20 is out."}]},
                                   "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://react.dev", "title": "React"}},
                                                                               {"web": {"uri": "https://react.dev", "title": "dup"}}]}}]}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)) as u:
            out = providers.complete({"id": "g", "provider": "gemini", "model": "g"}, [{"role": "user", "content": "x"}], web=True)
            self.assertEqual(self.sent(u)["tools"], [{"google_search": {}}])
        self.assertEqual(out.text, "React 20 is out.\n\nSources:\n- React: https://react.dev")

    def test_claude_code_ignores_web(self):
        with mock.patch.dict(providers.COMPLETERS, {"claude_code": lambda *a, **k: providers.Completion("ok")}):
            self.assertEqual(providers.complete({"id": "claude-code", "provider": "claude_code"}, [], web=True).text, "ok")


class Images(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k", "GEMINI_API_KEY": "k"})
        env.start()
        self.addCleanup(env.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = mock.patch.object(providers, "IMAGES_DIR", Path(self.dir.name))
        d.start()
        self.addCleanup(d.stop)

    def test_openai_image_is_saved_and_returned_as_markdown(self):
        cfg = {"id": "gpt-image-1", "label": "GPT Image", "provider": "openai", "model": "gpt-image-1", "modality": "image"}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"data": [{"b64_json": PNG}]})) as u:
            out = providers.generate_image(cfg, "A futuristic AI assistant!")
            body = json.loads(u.call_args[0][0].data)
        self.assertEqual((body["model"], body["prompt"]), ("gpt-image-1", "A futuristic AI assistant!"))
        (saved,) = list(Path(self.dir.name).iterdir())
        self.assertEqual(saved.read_bytes(), b"\x89PNG-fake")
        self.assertRegex(saved.name, r"^\d+-a-futuristic-ai-assistant\.png$")
        self.assertTrue(out.text.startswith(f"![A futuristic AI assistant!](/octopus/images/{saved.name})"))

    def test_gemini_image_reads_inline_data(self):
        cfg = {"id": "gemini-image", "label": "Gemini Image", "provider": "gemini", "model": "gm", "modality": "image"}
        payload = {"candidates": [{"content": {"parts": [{"text": "here"}, {"inlineData": {"mimeType": "image/jpeg", "data": PNG}}]}}]}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            out = providers.generate_image(cfg, "a cat")
        self.assertTrue(next(Path(self.dir.name).iterdir()).name.endswith(".jpg"))
        self.assertIn("Made with Gemini Image", out.text)

    def test_failures_are_provider_errors(self):
        cfg = {"id": "gpt-image-1", "provider": "openai", "model": "gpt-image-1", "modality": "image"}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"data": [{}]})):
            with self.assertRaisesRegex(providers.ProviderError, "returned no image"):
                providers.generate_image(cfg, "x")
        err = urllib.error.HTTPError("u", 429, "rate", {"retry-after": "5"}, io.BytesIO(b'{"error": {"message": "slow down"}}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(providers.ProviderError) as ctx:
                providers.generate_image(cfg, "x")
        self.assertTrue(ctx.exception.rate_limited)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(providers.ProviderError, "OPENAI_API_KEY"):
                providers.generate_image(cfg, "x")

    def test_image_cost_is_the_flat_per_image_price(self):
        self.assertEqual(providers.estimate_cost({"modality": "image", "price_per_image": 0.04}, None, None), 0.04)


if __name__ == "__main__":
    unittest.main()
