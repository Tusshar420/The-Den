"""Voice: providers (no real microphone, speaker or model) and the endpoints that feed the shared pipeline."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_e2e
from tests.helpers import TestBase

import db
import router
from octopus import service, voice


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Speech(unittest.TestCase):
    def test_markdown_is_not_read_out(self):
        text = "# Result\n\nSee **this** [page](http://x.com/a) or http://y.com.\n```py\nprint(1)\n```\n- one\n- two ![pic](/octopus/images/a.png)"
        out = voice.clean_for_speech(text)
        self.assertEqual(out, "Result See this page or a link. (code omitted) one two an image")

    def test_long_text_is_cut_at_a_sentence(self):
        out = voice.clean_for_speech("First sentence here. " * 60, limit=100)
        self.assertLessEqual(len(out), 100)
        self.assertTrue(out.endswith("."))


class Languages(TestBase):
    def test_every_sample_is_detected_as_its_own_language(self):
        from octopus.voice import languages
        for lang in languages.LANGUAGES:
            self.assertEqual(languages.detect_language(lang.sample), lang.code, lang.label)

    def test_detection_ignores_code_and_falls_back_to_english(self):
        from octopus.voice import languages
        self.assertEqual(languages.detect_language("Fixed it in `foo.py` - see https://example.com/a"), "en")
        self.assertEqual(languages.detect_language("東京へ行く"), "ja")            # Han characters plus kana is Japanese, not Chinese
        self.assertEqual(languages.detect_language("kya haal hai"), "en")          # romanised Hindi has no signal
        self.assertEqual(languages.detect_language("12345 !!!"), "en")
        self.assertEqual(languages.detect_language("Aquí está el archivo que pediste, es muy útil"), "es")

    def test_the_voice_for_a_reply_follows_its_language(self):
        installed = [{"name": "Samantha", "language": "en_US"}, {"name": "Lekha", "language": "hi_IN"}]
        with mock.patch.object(voice, "macos_voices", lambda: installed):
            db.set_setting("voice_tts_voice", "Samantha")
            self.assertEqual(voice.voice_for("Hello there, this is the answer"), "Samantha")
            self.assertEqual(voice.voice_for("नमस्ते, यह जवाब है"), "Samantha")        # no voice chosen for Hindi: the default
            db.set_setting("voice_language_voices", {"hi": "Lekha", "fr": "Thomas"})
            self.assertEqual(voice.voice_for("नमस्ते, यह जवाब है"), "Lekha")
            self.assertEqual(voice.voice_for("Bonjour, voici la réponse que vous avez demandée"), "Samantha")   # Thomas isn't installed
            db.set_setting("voice_tts_voice", "")
            self.assertIsNone(voice.voice_for("Hello there, this is the answer"))

    def test_a_voice_picked_by_hand_beats_the_language_voices_until_it_is_cleared(self):
        installed = [{"name": "Samantha", "language": "en_US"}, {"name": "Lekha", "language": "hi_IN"}]
        with mock.patch.object(voice, "macos_voices", lambda: installed):
            db.set_setting("voice_language_voices", {"hi": "Lekha"})
            db.set_setting("voice_manual", "")
            self.assertEqual(voice.voice_for("नमस्ते, यह जवाब है"), "Lekha")           # automatic: Hindi gets its own voice
            db.set_setting("voice_manual", "Samantha")
            self.assertEqual(voice.voice_for("नमस्ते, यह जवाब है"), "Samantha")        # picked by hand: used for every language
            db.set_setting("voice_manual", "Gone")
            self.assertEqual(voice.voice_for("नमस्ते, यह जवाब है"), "Lekha")           # no longer installed: ignored
            db.set_setting("voice_manual", "")
            db.set_setting("voice_language_voices", {})

    def test_speed_reaches_say(self):
        t, seen = voice.MacOSTTS(), []

        def fake_run(cmd, timeout, input_text=None):
            seen.append(cmd)
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"RIFFwav")
            return done()
        with mock.patch.object(voice, "_run", fake_run), mock.patch.object(t, "available", lambda: (True, "")):
            t.synthesize("Hello there")
            db.set_setting("voice_rate", 1.5)
            t.synthesize("Hello there")
            db.set_setting("voice_rate", 9)                                        # out of range is clamped, not passed to say
            t.synthesize("Hello there")
        self.assertNotIn("-r", seen[0])
        self.assertEqual(seen[1][1:3], ["-r", "262"])
        self.assertEqual(seen[2][1:3], ["-r", "350"])


class Providers(TestBase):
    def test_status_lists_providers_with_honest_availability(self):
        s = voice.status()
        stt = {p["id"]: p for p in s["stt"]}
        self.assertEqual((stt["browser"]["available"], stt["browser"]["client_side"], stt["browser"]["selected"]), (True, True, True))
        self.assertFalse(stt["whisper"]["available"] and "WHISPER_CLI" not in os.environ and voice.WhisperCppSTT().binary() is None)
        self.assertEqual(s["settings"]["voice_tts"], "browser")
        self.assertIn("voices", s)

    def test_a_browser_provider_does_not_run_on_the_server(self):
        with self.assertRaisesRegex(voice.VoiceError, "runs in the browser"):
            voice.transcribe(b"x", "audio/webm", "browser")
        with self.assertRaisesRegex(voice.VoiceError, "runs in the browser"):
            voice.synthesize("hi", provider_id="browser")
        with self.assertRaisesRegex(voice.VoiceError, "unknown speech-to-text provider 'nope'"):
            voice.transcribe(b"x", "audio/webm", "nope")

    def test_the_selected_providers_come_from_settings(self):
        db.set_setting("voice_stt", "whisper")
        with mock.patch.object(voice.WhisperCppSTT, "available", lambda self: (False, "not installed")):
            with self.assertRaisesRegex(voice.VoiceError, "not installed"):
                voice.transcribe(b"audio", "audio/wav")

    def test_a_new_provider_can_be_plugged_in_without_touching_octopus(self):
        class Cloud(voice.STT):
            id, label = "cloudstt", "Cloud STT"

            def transcribe(self, audio, mime):
                return "hello from the cloud"
        voice.register_stt(Cloud())
        self.addCleanup(voice.STT_PROVIDERS.pop, "cloudstt")
        db.set_setting("voice_stt", "cloudstt")
        self.assertEqual(voice.transcribe(b"x", "audio/wav"), "hello from the cloud")

    def test_whisper_cpp_runs_the_binary_on_a_converted_recording(self):
        w = voice.WhisperCppSTT()
        calls = []

        def fake_run(cmd, timeout, input_text=None):
            calls.append(cmd)
            if "whisper" in cmd[0]:
                return done("  Fix the login   bug.\n")
            Path(cmd[-1]).write_bytes(b"RIFF")            # the converter wrote a wav
            return done()
        with mock.patch.object(w, "binary", lambda: "/bin/whisper-cli"), mock.patch.object(w, "model", lambda: "/m/ggml.bin"), \
                mock.patch.object(voice, "_run", fake_run), mock.patch("shutil.which", lambda n: None):
            text = w.transcribe(b"audio-bytes", "audio/mp4")
        self.assertEqual(text, "Fix the login bug.")
        self.assertEqual(calls[0][:5], [voice.AFCONVERT, "-f", "WAVE", "-d", "LEI16@16000"])
        self.assertEqual(calls[1][:3], ["/bin/whisper-cli", "-m", "/m/ggml.bin"])
        self.assertEqual(calls[1][-2:], ["-l", "auto"])            # a multilingual model follows the spoken language

    def test_whisper_english_only_model_gets_no_language_flag(self):
        w = voice.WhisperCppSTT()
        calls = []

        def fake_run(cmd, timeout, input_text=None):
            calls.append(cmd)
            if "whisper" in cmd[0]:
                return done("hello")
            Path(cmd[-1]).write_bytes(b"RIFF")
            return done()
        with mock.patch.object(w, "binary", lambda: "/bin/whisper-cli"), mock.patch.object(w, "model", lambda: "/m/ggml-base.en.bin"), \
                mock.patch.object(voice, "_run", fake_run), mock.patch("shutil.which", lambda n: None):
            w.transcribe(b"audio-bytes", "audio/mp4")
        self.assertNotIn("-l", calls[1])

    def test_whisper_reports_what_is_missing(self):
        w = voice.WhisperCppSTT()
        with mock.patch.dict(os.environ, {"WHISPER_CLI": "/nope"}):
            self.assertIn("isn't installed", w.available()[1])
        with tempfile.NamedTemporaryFile() as f, mock.patch.dict(os.environ, {"WHISPER_CLI": f.name, "WHISPER_MODEL": "/nope"}):
            self.assertIn("no Whisper model", w.available()[1])
            with self.assertRaisesRegex(voice.VoiceError, "no Whisper model"):
                w.transcribe(b"x", "audio/wav")

    def test_macos_tts_makes_a_wav_and_only_accepts_real_voices(self):
        t = voice.MacOSTTS()
        seen = {}

        def fake_run(cmd, timeout, input_text=None):
            seen.update(cmd=cmd, text=input_text)
            if "-o" in cmd:
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"RIFFwav")
            return done()
        with mock.patch.object(voice, "_run", fake_run), mock.patch.object(voice, "macos_voices", lambda: [{"name": "Samantha", "language": "en_US"}]), \
                mock.patch.object(t, "available", lambda: (True, "")):
            audio, mime = t.synthesize("**Done.** See http://x.com", "Samantha")
            self.assertEqual((audio, mime), (b"RIFFwav", "audio/wav"))
            self.assertEqual(seen["text"], "Done. See a link")
            self.assertEqual(seen["cmd"][-2:], ["-v", "Samantha"])
            with self.assertRaisesRegex(voice.VoiceError, "no macOS voice called '-o /etc/passwd'"):
                t.synthesize("hi", "-o /etc/passwd")
            with self.assertRaisesRegex(voice.VoiceError, "nothing to say"):
                t.synthesize("***")

    def test_macos_voices_parse(self):
        out = "Albert              en_US    # Hello! My name is Albert.\nSoumya              kn_IN    # Hello!\nGood News           en_US    # Hi\n"
        with mock.patch.object(voice, "_run", lambda *a, **k: done(out)), mock.patch("os.path.exists", lambda p: True):
            self.assertEqual([v["name"] for v in voice.macos_voices()], ["Albert", "Soumya", "Good News"])


class SpokenReplies(TestBase):
    def setUp(self):
        super().setUp()
        self.proj = db.add_project("KodeAura7", "/tmp/k")
        db.add_project("Other", "/tmp/o")
        for p in (mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k", "GEMINI_API_KEY": "k"}),
                  mock.patch.object(router, "_llm_classify", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_what_is_said_for_each_outcome(self):
        self.assertEqual(service.submit("Fix the login bug in KodeAura7")["spoken"],
                         "On it. The Coding Agent is handling that with Claude Code in KodeAura7.")
        self.assertEqual(service.submit("Fix the bug")["spoken"], "Which project is this for? KodeAura7 or Other?")
        service.cancel_plan(db.pending_plan()["id"])
        self.assertEqual(service.submit("Deploy KodeAura7 to production")["spoken"],
                         "Here's my plan: the Coding Agent on Claude Code in KodeAura7. It involves deploy or publish and touch production. "
                         "Say yes to go ahead, or no to cancel.")
        asked = service.submit("what's happening")
        self.assertEqual(asked["spoken"], asked["result"]["spoken"])


class VoiceApi(test_e2e.E2E):
    def setUp(self):
        super().setUp()
        db.add_project("KodeAura7", tempfile.mkdtemp())
        for p in (mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k", "GEMINI_API_KEY": "k"}),
                  mock.patch.object(router, "_llm_classify", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def raw(self, path, data, ctype):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="POST", data=data,
                                     headers={"X-API-Key": self.KEY, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read(), r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type")

    def test_auth(self):
        for method, path in (("GET", "/voice"), ("POST", "/voice/transcribe"), ("POST", "/voice/speak"), ("POST", "/voice/command")):
            self.assertEqual(self.http(method, path, {}, key="wrong")["_status"], 401, path)

    def test_status_endpoint(self):
        s = self.http("GET", "/voice")
        self.assertEqual({p["id"] for p in s["stt"]}, {"browser", "whisper"})
        self.assertEqual({p["id"] for p in s["tts"]}, {"browser", "macos"})
        self.assertEqual(self.http("PATCH", "/settings", {"voice_tts": "macos", "voice_speak_replies": False})["voice_tts"], "macos")
        self.assertEqual(self.http("GET", "/voice")["settings"]["voice_speak_replies"], False)

    def test_language_voices_and_speed_are_settings(self):
        s = self.http("GET", "/voice")
        self.assertEqual((s["settings"]["voice_rate"], s["settings"]["voice_language_voices"]), (1.0, {}))
        self.assertEqual({lang["code"] for lang in s["languages"]} >= {"en", "hi", "es"}, True)
        saved = self.http("PATCH", "/settings", {"voice_language_voices": {"hi": " Lekha ", "fr": ""}, "voice_rate": 1.25})
        self.assertEqual((saved["voice_language_voices"], saved["voice_rate"]), ({"hi": "Lekha"}, 1.25))
        self.assertEqual(self.http("PATCH", "/settings", {"voice_language_voices": {"xx": "Nobody"}})["_status"], 422)
        self.assertEqual(self.http("PATCH", "/settings", {"voice_rate": 5})["_status"], 422)
        self.assertEqual(self.http("PATCH", "/settings", {"voice_language_voices": {}})["voice_language_voices"], {})

    def test_a_spoken_command_goes_through_the_same_pipeline_as_typed_text(self):
        typed = self.http("POST", "/octopus", {"text": "what's happening?"})
        spoken = self.http("POST", "/voice/command", {"text": "Octopus, what's happening?"})
        self.assertEqual((spoken["status"], spoken["transcript"]), ("done", "Octopus, what's happening?"))
        self.assertEqual(spoken["result"]["text"], typed["result"]["text"])
        self.assertEqual(self.http("GET", f"/routing/{spoken['routing_id']}")["source"], "voice")

    def test_a_spoken_conversation_keeps_its_context(self):
        first = self.http("POST", "/voice/command", {"text": "Check KodeAura7 for problems"})
        self.assertEqual(first["spoken"], "On it. The Coding Agent is handling that with Claude Code in KodeAura7.")
        root = first["task"]["id"]
        db.mark_running(root)
        db.mark_done(root, "I found three issues.")
        second = self.http("POST", "/voice/command", {"text": "Fix the second one"})
        self.assertEqual(second["task"]["thread_id"], root)
        self.assertEqual(self.http("POST", "/voice/command", {"text": "Run the tests afterward"})["task"]["thread_id"], root)

    def test_yes_by_voice_runs_the_plan_that_was_read_out(self):
        proposed = self.http("POST", "/voice/command", {"text": "Deploy KodeAura7 to production"})
        self.assertEqual(proposed["status"], "proposed")
        self.assertTrue(proposed["spoken"].endswith("Say yes to go ahead, or no to cancel."))
        ran = self.http("POST", "/voice/command", {"text": "Yes, go ahead"})
        self.assertEqual((ran["status"], ran["routing_id"]), ("queued", proposed["routing_id"]))

    def test_audio_needs_a_server_side_provider(self):
        status, body, _ = self.raw("/voice/command", b"\x00\x01", "audio/webm")     # default stt is the browser
        self.assertEqual(status, 409)
        self.assertIn(b"runs in the browser", body)
        self.assertEqual(self.raw("/voice/transcribe", b"", "audio/webm")[0], 400)

    def test_audio_is_transcribed_then_submitted(self):
        with mock.patch.object(voice, "transcribe", lambda audio, mime, provider=None: "Octopus, what's happening?"):
            status, body, _ = self.raw("/voice/command", b"\x00\x01", "audio/webm")
        self.assertEqual(status, 200)
        self.assertIn(b'"transcript":"Octopus, what', body)
        with mock.patch.object(voice, "transcribe", lambda audio, mime, provider=None: "hello there"):
            status, body, _ = self.raw("/voice/transcribe", b"\x00", "audio/webm")
        self.assertEqual((status, body), (200, b'{"text":"hello there"}'))

    def test_speak(self):
        self.assertEqual(self.http("POST", "/voice/speak", {"text": "hi", "provider": "browser"})["_status"], 409)
        with mock.patch.object(voice, "synthesize", lambda text, voice=None, provider_id=None: (b"RIFFx", "audio/wav")):
            self.assertEqual(self.raw("/voice/speak", b'{"text": "hi"}', "application/json")[::2], (200, "audio/wav"))
        with mock.patch.object(voice, "speak_on_mac") as speak:
            self.assertEqual(self.http("POST", "/voice/speak", {"text": "hi", "on": "mac"}), {"ok": True})
        speak.assert_called_once_with("hi", None)


if __name__ == "__main__":
    unittest.main()
