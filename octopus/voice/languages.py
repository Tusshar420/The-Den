"""
The languages a reply can be spoken in, and how to tell which one a piece of text is.

The Voice page lets you pick a voice per language; this decides which language a reply is in. Two cheap
signals, no model: the writing system (Devanagari, Arabic, Kana...) and, for languages written in the Latin
alphabet, common little words. Romanised Hindi ("kya haal hai") reads as English - it has no signal to tell it apart.

The table is sent to the dashboard (GET /voice) so the browser detects with exactly the same rules.
"""

import re
from typing import Any, NamedTuple

DEFAULT_LANGUAGE = "en"
SCRIPT_SHARE = 0.3         # this much of the letters in a script -> that language


class Language(NamedTuple):
    code: str
    label: str
    sample: str             # what "Play" says - must itself be detected as this language
    script: str = ""        # regex for its writing system (empty: Latin alphabet)
    words: frozenset = frozenset()


def _w(s: str) -> frozenset:
    return frozenset(s.split())


# Order matters for scripts: Japanese (kana) before Chinese, since Japanese text is full of Han characters too.
LANGUAGES: tuple[Language, ...] = (
    Language("en", "English", "Hello, this is how I sound when I read your answers.",
             words=_w("the and is are was you your this that with for have not what from they will would there their")),
    Language("hi", "Hindi", "नमस्ते, मैं आपके जवाब इसी आवाज़ में पढ़कर सुनाऊँगा।", script=r"[\u0900-\u097F]"),
    Language("bn", "Bengali", "নমস্কার, আমি আপনার উত্তরগুলো এই কণ্ঠে পড়ে শোনাব।", script=r"[\u0980-\u09FF]"),
    Language("ta", "Tamil", "வணக்கம், உங்கள் பதில்களை நான் இந்தக் குரலில் படிப்பேன்.", script=r"[\u0B80-\u0BFF]"),
    Language("te", "Telugu", "నమస్కారం, మీ సమాధానాలను నేను ఈ స్వరంలో చదువుతాను.", script=r"[\u0C00-\u0C7F]"),
    Language("ar", "Arabic", "مرحبا، هكذا أبدو عندما أقرأ لك الإجابات.", script=r"[\u0600-\u06FF]"),
    Language("ru", "Russian", "Здравствуйте, вот так я звучу, когда читаю вам ответы.", script=r"[\u0400-\u04FF]"),
    Language("ja", "Japanese", "こんにちは、これが答えを読み上げるときの声です。", script=r"[\u3040-\u30FF]"),
    Language("ko", "Korean", "안녕하세요, 답변을 읽어 드릴 때 이런 목소리로 말합니다.", script=r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]"),
    Language("zh", "Chinese", "你好，这就是我为你朗读回答时的声音。", script=r"[\u4E00-\u9FFF]"),
    Language("es", "Spanish", "Hola, así es como sueno cuando leo tus respuestas para ti.",
             words=_w("el la los las de que y en un una es por con para no se su como pero más esto está son muy también")),
    Language("fr", "French", "Bonjour, voici comment je sonne quand je vous lis les réponses.",
             words=_w("le la les des et est une que qui dans pour pas vous nous ce sont avec sur mais très aussi bonjour")),
    Language("de", "German", "Hallo, so klinge ich, wenn ich dir die Antworten vorlese.",
             words=_w("der die das und ist nicht ein eine ich sie es mit für auf den dem zu auch sind wie hallo")),
    Language("pt", "Portuguese", "Olá, é assim que eu soo quando leio as respostas para você.",
             words=_w("o os as de que e um uma não para com por você é são mais muito está também isso olá")),
    Language("it", "Italian", "Ciao, ecco come suono quando ti leggo le risposte.",
             words=_w("il lo gli le di che e un una non per con sono è più molto anche questo come ciao")),
)
BY_CODE = {lang.code: lang for lang in LANGUAGES}


def _plain(text: str) -> str:
    """Drop what isn't prose: code, links. (They are English-looking words in any language's answer.)"""
    t = re.sub(r"```.*?```|`[^`]*`|https?://\S+", " ", text, flags=re.DOTALL)
    return t


def detect_language(text: str) -> str:
    """The language code `text` is written in; DEFAULT_LANGUAGE when there is no clear signal."""
    t = _plain(text)
    letters = re.findall(r"[^\W\d_]", t)
    if not letters:
        return DEFAULT_LANGUAGE
    for lang in LANGUAGES:
        if lang.script and len(re.findall(lang.script, t)) / len(letters) >= SCRIPT_SHARE:
            return lang.code
    words = re.findall(r"[^\W\d_]+", t.lower())
    scores = sorted(((sum(w in lang.words for w in words), lang.code) for lang in LANGUAGES if lang.words), reverse=True)
    (best, code), (second, _) = scores[0], scores[1]
    return code if best >= 2 and best > second else DEFAULT_LANGUAGE


def describe() -> list[dict[str, Any]]:
    """The table as the dashboard needs it, to detect and to offer a sample."""
    return [{"code": lang.code, "label": lang.label, "sample": lang.sample, "script": lang.script,
             "words": sorted(lang.words)} for lang in LANGUAGES]
