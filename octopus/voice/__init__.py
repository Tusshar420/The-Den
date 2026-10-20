"""
Voice for Octopus: speech-to-text and text-to-speech as pluggable providers.

Voice is only another way in and out. It never has a pipeline of its own:

    microphone -> STT provider -> text -> octopus.service.submit(source="voice") -> plan / queue / instant command
    answer text -> spoken_reply / clean_for_speech -> TTS provider -> speaker

Nothing here depends on one engine. A provider is a small class with an id,
a label, `available()` and one method; add a cloud provider by writing one and
calling register_stt / register_tts - the settings (voice_stt, voice_tts) just
name the id.

Built in:
    stt  browser   the browser's own speech recognition (Web Speech API) - runs on the device, no server work
         whisper   local Whisper through whisper.cpp (`whisper-cli`) - needs the binary and a model file
    tts  browser   the browser's own speechSynthesis - speaks on the phone / laptop you are using
         macos     the Mac's `say` - speaks on the Mac, or returns a .wav for any device to play

Which voice speaks: a voice picked by hand in the Ask box (voice_manual) wins until it is set back to automatic.
Otherwise the reply's language is detected (languages.py) and the voice chosen for that language on the Voice
page is used; a language with no choice gets the default voice (voice_tts_voice). Speed is voice_rate.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

import db
from octopus.voice.languages import BY_CODE, DEFAULT_LANGUAGE, LANGUAGES, describe as describe_languages, detect_language

SAY = "/usr/bin/say"
AFCONVERT = "/usr/bin/afconvert"
TRANSCRIBE_TIMEOUT = 90
SPEAK_MAX_CHARS = 600
SAY_WORDS_PER_MINUTE = 175          # `say` at speed 1.0


class VoiceError(Exception):
    """A voice provider can't do what was asked; the message says why in plain words."""


class Provider:
    id = ""
    label = ""
    description = ""
    # True: the browser does it - the server has nothing to run.
    client_side = False

    def available(self) -> tuple[bool, str]:
        return True, ""


class STT(Provider):
    def transcribe(self, audio: bytes, mime: str) -> str:
        raise VoiceError(f"{self.label} runs in the browser - the server doesn't transcribe for it")


class TTS(Provider):
    def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        raise VoiceError(f"{self.label} runs in the browser - the server doesn't synthesise for it")


# ------------------------------------------------------------------- helpers

def _run(cmd: list[str], timeout: int, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    except subprocess.TimeoutExpired:
        raise VoiceError(f"{Path(cmd[0]).name} took longer than {timeout}s")
    except OSError as e:
        raise VoiceError(f"could not run {cmd[0]}: {e}")


def clean_for_speech(text: str, limit: int = SPEAK_MAX_CHARS) -> str:
    """Chat answers are markdown; a voice should read the words, not the symbols."""
    t = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.DOTALL)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " an image ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+?(?=[.,;:!?)]*(?:\s|$))", "a link", t)
    t = re.sub(r"[`*_#>|~]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "…")


def macos_voices() -> list[dict[str, str]]:
    """[{name, language}] from `say -v ?` (empty off macOS)."""
    if not os.path.exists(SAY):
        return []
    try:
        out = _run([SAY, "-v", "?"], 10).stdout
    except VoiceError:
        return []
    voices = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s{2,}([a-z]{2}[_-][A-Za-z]{2,})\s+#", line)
        if m:
            voices.append({"name": m.group(1).strip(), "language": m.group(2)})
    return voices


def speech_rate() -> float:
    try:
        return min(2.0, max(0.5, float(db.get_setting("voice_rate"))))
    except (TypeError, ValueError):
        return 1.0


def _say_speed() -> list[str]:
    rate = speech_rate()
    return [] if rate == 1.0 else ["-r", str(round(SAY_WORDS_PER_MINUTE * rate))]


def voice_for(text: str) -> Optional[str]:
    """The macOS voice to read `text` in: the one picked by hand in the Ask box, else the one chosen for its
    language, else the default voice (None = the system's). A chosen voice that is no longer installed is
    ignored rather than breaking speech."""
    installed = {v["name"] for v in macos_voices()}
    manual = db.get_setting("voice_manual")
    if manual and manual in installed:
        return manual
    chosen = (db.get_setting("voice_language_voices") or {}).get(detect_language(text))
    if chosen and chosen in installed:
        return chosen
    return db.get_setting("voice_tts_voice") or None


# ----------------------------------------------------------------------- stt

class BrowserSTT(STT):
    id, label, client_side = "browser", "Browser speech recognition", True
    description = "Uses the speech recognition built into Safari / Chrome. Nothing is installed; audio handling is up to the browser."


class WhisperCppSTT(STT):
    id, label = "whisper", "Whisper (local, whisper.cpp)"
    description = "Transcribes on this Mac with whisper.cpp. Needs `whisper-cli` (brew install whisper-cpp) and a model file (WHISPER_MODEL)."
    BINARIES = ("whisper-cli", "whisper-cpp", "whisper.cpp")
    MODEL_DIRS = ("~/.cache/whisper", "~/.local/share/whisper", "/opt/homebrew/share/whisper-cpp", "/usr/local/share/whisper-cpp")
    MODEL_NAMES = ("ggml-base.en.bin", "ggml-small.en.bin", "ggml-base.bin", "ggml-small.bin", "ggml-tiny.en.bin")

    def binary(self) -> Optional[str]:
        explicit = os.environ.get("WHISPER_CLI")
        if explicit:
            return explicit if os.path.exists(explicit) else None
        return next((p for p in (shutil.which(b) for b in self.BINARIES) if p), None)

    def model(self) -> Optional[str]:
        explicit = os.environ.get("WHISPER_MODEL")
        if explicit:
            return explicit if os.path.exists(explicit) else None
        for d in self.MODEL_DIRS:
            for name in self.MODEL_NAMES:
                p = Path(d).expanduser() / name
                if p.exists():
                    return str(p)
        return None

    def available(self) -> tuple[bool, str]:
        if not self.binary():
            return False, "whisper.cpp isn't installed (brew install whisper-cpp, or set WHISPER_CLI)"
        if not self.model():
            return False, "no Whisper model file found (download one and set WHISPER_MODEL)"
        return True, ""

    def _to_wav(self, src: Path, dst: Path) -> None:
        if shutil.which("ffmpeg"):
            cmd = [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)]
        elif os.path.exists(AFCONVERT):
            cmd = [AFCONVERT, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(dst)]
        else:
            raise VoiceError("no audio converter (ffmpeg or afconvert) to prepare the recording")
        done = _run(cmd, 30)
        if done.returncode != 0 or not dst.exists():
            raise VoiceError("could not read the recording (" + (done.stderr.strip()[-160:] or "unsupported format; install ffmpeg for webm") + ")")

    def transcribe(self, audio: bytes, mime: str) -> str:
        ok, why = self.available()
        if not ok:
            raise VoiceError(why)
        if not audio:
            raise VoiceError("the recording was empty")
        ext = "m4a" if "mp4" in mime or "m4a" in mime or "aac" in mime else "webm" if "webm" in mime else "ogg" if "ogg" in mime else "wav"
        with tempfile.TemporaryDirectory() as d:
            src, wav = Path(d) / f"in.{ext}", Path(d) / "in16k.wav"
            src.write_bytes(audio)
            self._to_wav(src, wav)
            model = self.model()
            # Multilingual models default to English; "auto" lets them follow whichever language is being spoken.
            lang = [] if model.endswith(".en.bin") else ["-l", "auto"]
            done = _run([self.binary(), "-m", model, "-f", str(wav), "-nt", "-np"] + lang, TRANSCRIBE_TIMEOUT)
        if done.returncode != 0:
            raise VoiceError("whisper failed: " + (done.stderr.strip()[-200:] or f"exit {done.returncode}"))
        return re.sub(r"\s+", " ", done.stdout).strip()


# ----------------------------------------------------------------------- tts

class BrowserTTS(TTS):
    id, label, client_side = "browser", "Browser speech", True
    description = "Speaks with the voices on the device you are using (phone, laptop). Nothing is installed."


class MacOSTTS(TTS):
    id, label = "macos", "macOS voice (say)"
    description = "Uses the Mac's built-in voices. Can speak on the Mac's speakers, or send audio to any device."

    def available(self) -> tuple[bool, str]:
        return (True, "") if os.path.exists(SAY) else (False, "macOS `say` isn't available on this machine")

    def _voice(self, voice: Optional[str]) -> Optional[str]:
        voice = (voice or "").strip()
        if not voice:
            return None
        if voice not in {v["name"] for v in macos_voices()}:
            raise VoiceError(f"there's no macOS voice called '{voice}'")
        return voice

    def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        ok, why = self.available()
        if not ok:
            raise VoiceError(why)
        text = clean_for_speech(text)
        if not text:
            raise VoiceError("nothing to say")
        voice = self._voice(voice)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "speech.wav"
            cmd = [SAY] + _say_speed() + ["-o", str(out), "--data-format=LEI16@22050", "-f", "-"] + (["-v", voice] if voice else [])
            done = _run(cmd, 60, input_text=text)
            if done.returncode != 0 or not out.exists():
                raise VoiceError("say failed: " + (done.stderr.strip()[-160:] or f"exit {done.returncode}"))
            return out.read_bytes(), "audio/wav"

    def speak_aloud(self, text: str, voice: Optional[str] = None) -> None:
        """Speak on the Mac's own speakers and return at once."""
        ok, why = self.available()
        if not ok:
            raise VoiceError(why)
        text, voice = clean_for_speech(text), self._voice(voice)
        if not text:
            raise VoiceError("nothing to say")
        proc = subprocess.Popen([SAY] + _say_speed() + ["-f", "-"] + (["-v", voice] if voice else []), stdin=subprocess.PIPE, text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert proc.stdin is not None
        proc.stdin.write(text)
        proc.stdin.close()


# ------------------------------------------------------------------ registry

STT_PROVIDERS: dict[str, STT] = {}
TTS_PROVIDERS: dict[str, TTS] = {}


def register_stt(p: STT) -> None:
    STT_PROVIDERS[p.id] = p


def register_tts(p: TTS) -> None:
    TTS_PROVIDERS[p.id] = p


for _p in (BrowserSTT(), WhisperCppSTT()):
    register_stt(_p)
for _p in (BrowserTTS(), MacOSTTS()):
    register_tts(_p)


def _view(p: Provider, chosen: str) -> dict[str, Any]:
    ok, why = p.available()
    return {"id": p.id, "label": p.label, "description": p.description, "client_side": p.client_side,
            "available": ok, "reason": why, "selected": p.id == chosen}


def status() -> dict[str, Any]:
    stt, tts = db.get_setting("voice_stt"), db.get_setting("voice_tts")
    return {"stt": [_view(p, stt) for p in STT_PROVIDERS.values()], "tts": [_view(p, tts) for p in TTS_PROVIDERS.values()],
            "settings": {"voice_stt": stt, "voice_tts": tts, "voice_tts_voice": db.get_setting("voice_tts_voice"),
                         "voice_manual": db.get_setting("voice_manual"), "voice_speak_replies": db.get_setting("voice_speak_replies"),
                         "voice_rate": speech_rate(), "voice_language_voices": db.get_setting("voice_language_voices")},
            "voices": macos_voices(), "languages": describe_languages()}


def _pick(registry: dict[str, Any], provider_id: Optional[str], setting: str, kind: str) -> Any:
    pid = provider_id or db.get_setting(setting)
    p = registry.get(pid)
    if not p:
        raise VoiceError(f"unknown {kind} provider '{pid}'")
    return p


def transcribe(audio: bytes, mime: str, provider_id: Optional[str] = None) -> str:
    return _pick(STT_PROVIDERS, provider_id, "voice_stt", "speech-to-text").transcribe(audio, mime)


def synthesize(text: str, voice: Optional[str] = None, provider_id: Optional[str] = None) -> tuple[bytes, str]:
    p = _pick(TTS_PROVIDERS, provider_id, "voice_tts", "text-to-speech")
    return p.synthesize(text, voice if voice is not None else (voice_for(text) if p.id == "macos" else None))


def speak_on_mac(text: str, voice: Optional[str] = None) -> None:
    TTS_PROVIDERS["macos"].speak_aloud(text, voice if voice is not None else voice_for(text))
