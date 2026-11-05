"""The voices the Reader can read aloud with.

Kokoro-82M v1.0 voices, as vendored by scripts/vendor_tts.py. The first letter
of an id is the language Kokoro was trained on for that voice; the Reader's
worker uses it to choose the phonemizer (kokoro-js's own for English, eSpeak NG
for the rest). Grades are hexgrad's, from the model card: they are why the
default is af_heart and why the list leads with the better voices.
"""

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
