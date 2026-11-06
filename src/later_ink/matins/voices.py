"""The voices the Reader can read aloud with.

Kokoro-82M v1.0 voices, as vendored by scripts/vendor_tts.py. The first letter
of an id is the language Kokoro was trained on for that voice; the Reader's
worker uses it to choose the phonemizer (kokoro-js's own for English, eSpeak NG
for the rest). Grades are hexgrad's, from the model card: they are why the
default is af_heart and why the list leads with the better voices.
"""

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    language: str


DEFAULT_VOICE = "af_heart"

VOICES: tuple[Voice, ...] = (
    Voice("af_heart", "Heart", "English (US)"),
    Voice("af_bella", "Bella", "English (US)"),
    Voice("af_nicole", "Nicole", "English (US)"),
    Voice("af_aoede", "Aoede", "English (US)"),
    Voice("af_kore", "Kore", "English (US)"),
    Voice("af_sarah", "Sarah", "English (US)"),
    Voice("af_nova", "Nova", "English (US)"),
    Voice("af_sky", "Sky", "English (US)"),
    Voice("af_alloy", "Alloy", "English (US)"),
    Voice("af_jessica", "Jessica", "English (US)"),
    Voice("af_river", "River", "English (US)"),
    Voice("am_michael", "Michael", "English (US)"),
    Voice("am_fenrir", "Fenrir", "English (US)"),
    Voice("am_puck", "Puck", "English (US)"),
    Voice("am_echo", "Echo", "English (US)"),
    Voice("am_eric", "Eric", "English (US)"),
    Voice("am_liam", "Liam", "English (US)"),
    Voice("am_onyx", "Onyx", "English (US)"),
    Voice("am_santa", "Santa", "English (US)"),
    Voice("am_adam", "Adam", "English (US)"),
    Voice("bf_emma", "Emma", "English (UK)"),
    Voice("bf_isabella", "Isabella", "English (UK)"),
    Voice("bf_alice", "Alice", "English (UK)"),
    Voice("bf_lily", "Lily", "English (UK)"),
    Voice("bm_george", "George", "English (UK)"),
    Voice("bm_fable", "Fable", "English (UK)"),
    Voice("bm_lewis", "Lewis", "English (UK)"),
    Voice("bm_daniel", "Daniel", "English (UK)"),
    Voice("ff_siwis", "Siwis", "French"),
    Voice("if_sara", "Sara", "Italian"),
    Voice("im_nicola", "Nicola", "Italian"),
    Voice("ef_dora", "Dora", "Spanish"),
    Voice("em_alex", "Alex", "Spanish"),
    Voice("em_santa", "Santa", "Spanish"),
    Voice("pf_dora", "Dora", "Portuguese (BR)"),
    Voice("pm_alex", "Alex", "Portuguese (BR)"),
    Voice("pm_santa", "Santa", "Portuguese (BR)"),
)

VOICE_IDS = frozenset(voice.id for voice in VOICES)


def valid_voice(value: str | None) -> str | None:
    """The voice id if Matins ships it, else None."""
    value = (value or "").strip()
    return value if value in VOICE_IDS else None


# One voice per language. The first letter of a Kokoro voice id is its language
# (a/b: US/UK English); a catalogue picks a voice for each language it reads,
# and an article is read with the voice of the language it is written in.
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("fr", "French"),
    ("it", "Italian"),
    ("es", "Spanish"),
    ("pt", "Portuguese"),
)
LANGUAGE_NAMES = dict(LANGUAGES)
_PREFIX = {"a": "en", "b": "en", "f": "fr", "i": "it", "e": "es", "p": "pt"}
# A language nobody chose a voice for still reads in that language.
DEFAULTS = {"en": DEFAULT_VOICE, "fr": "ff_siwis", "it": "if_sara", "es": "ef_dora", "pt": "pf_dora"}


def language_of(voice_id: str) -> str:
    return _PREFIX.get(voice_id[:1], "en")


def voices_for(language: str) -> list[Voice]:
    return [voice for voice in VOICES if language_of(voice.id) == language]


def parse_choices(raw: str | None) -> dict[str, str]:
    """The stored per-language choices. An older catalogue stored one voice id:
    that is its language's choice."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {language_of(raw): raw} if raw in VOICE_IDS else {}
    if not isinstance(data, dict):
        return {}
    return {
        lang: voice
        for lang, voice in data.items()
        if lang in LANGUAGE_NAMES and voice in VOICE_IDS and language_of(voice) == lang
    }


def dump_choices(choices: dict[str, str]) -> str | None:
    return json.dumps(choices, sort_keys=True) if choices else None


def voice_for(language: str, choices: dict[str, str]) -> str:
    return choices.get(language) or DEFAULTS.get(language, DEFAULT_VOICE)


# The most frequent short words of each language, minus the ones they share.
# A news article has dozens of them in its first paragraphs; that is enough.
_MARKERS = {
    "en": set("the and of to is that with for was on are this have from be by it".split()),
    "fr": set("le les des est une dans pour du qui avec pas sur au ce sont aux leur".split()),
    "it": set("il che di della sono gli per non una nel alla anche dei delle è ha".split()),
    "es": set("el los las del por que una con para es como más pero sus fue ha".split()),
    "pt": set("não uma do da os em para com ao mais são também foi pelo pela".split()),
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_language(text: str) -> str | None:
    """en, fr, it, es or pt, or None when the text is too short to tell."""
    words = [w.casefold() for w in _WORD.findall(text[:4000])]
    scores = {lang: sum(1 for w in words if w in marks) for lang, marks in _MARKERS.items()}
    best = max(scores, key=lambda lang: scores[lang])
    return best if scores[best] >= 3 else None
