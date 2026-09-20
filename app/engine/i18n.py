"""Internationalisation for the call flow.

Every string a caller can hear or read lives in a language pack under
``app/engine/lang/``. Packs are static data -- no runtime translation, no
LLM involvement anywhere near the decision path.

Fallback rule: a missing key falls back to English and is recorded in
``missing_keys`` so gaps are visible instead of silently shipping.
"""
from __future__ import annotations

from .lang import bn, en, hi, mr, ta, te

PACKS: dict[str, dict[str, str]] = {
    "en": en.STRINGS,
    "hi": hi.STRINGS,
    "mr": mr.STRINGS,
    "bn": bn.STRINGS,
    "ta": ta.STRINGS,
    "te": te.STRINGS,
}

# DTMF digit -> language, spoken in this exact order at call start.
LANGUAGE_BY_DIGIT: dict[str, str] = {
    "1": "hi",
    "2": "en",
    "3": "mr",
    "4": "bn",
    "5": "ta",
    "6": "te",
}

# BCP-47 codes for browser speech synthesis in the web companion.
SPEECH_LOCALE: dict[str, str] = {
    "en": "en-IN",
    "hi": "hi-IN",
    "mr": "mr-IN",
    "bn": "bn-IN",
    "ta": "ta-IN",
    "te": "te-IN",
}

LANGUAGE_NAMES: dict[str, str] = {
    "hi": "हिन्दी",
    "en": "English",
    "mr": "मराठी",
    "bn": "বাংলা",
    "ta": "தமிழ்",
    "te": "తెలుగు",
}

missing_keys: set[tuple[str, str]] = set()


def t(lang: str | None, key: str, **params) -> str:
    """Resolve ``key`` in ``lang``; fall back to English; interpolate params."""
    pack = PACKS.get(lang or "en", PACKS["en"])
    text = pack.get(key)
    if text is None:
        text = PACKS["en"].get(key, key)
        if (lang or "en") != "en":
            missing_keys.add((lang or "en", key))
    if params:
        try:
            text = text.format(**params)
        except (KeyError, IndexError):
            pass  # never let a formatting bug break a live call
    return text


def supported_languages() -> list[str]:
    return list(PACKS.keys())
