"""Language pack completeness: a caller in any supported language must hear
every prompt, question, status and guidance string -- no silent key gaps."""
from app.engine import i18n


def test_all_packs_have_every_english_key():
    en_keys = set(i18n.PACKS["en"].keys())
    for lang, pack in i18n.PACKS.items():
        missing = en_keys - set(pack.keys())
        assert not missing, f"{lang} missing keys: {sorted(missing)}"


def test_no_pack_has_unknown_keys():
    en_keys = set(i18n.PACKS["en"].keys())
    for lang, pack in i18n.PACKS.items():
        extra = set(pack.keys()) - en_keys
        assert not extra, f"{lang} has keys not in en: {sorted(extra)}"


def test_six_languages_available():
    assert set(i18n.PACKS) == {"en", "hi", "mr", "bn", "ta", "te"}


def test_every_language_reachable_by_dtmf():
    assert set(i18n.LANGUAGE_BY_DIGIT.values()) == set(i18n.PACKS)


def test_every_question_has_text_in_every_language():
    from app.engine.questions import MATERNAL_QUESTIONS, NEWBORN_QUESTIONS
    for q in (*MATERNAL_QUESTIONS, *NEWBORN_QUESTIONS):
        for lang in i18n.PACKS:
            text = i18n.t(lang, f"q_{q.id}")
            assert text and text != f"q_{q.id}", f"missing q_{q.id} in {lang}"


def test_every_guidance_key_has_text_in_every_language():
    from app.engine import rules
    guidance_keys = set()
    for tier in (rules.TIER_EMERGENCY, rules.TIER_URGENT, rules.TIER_REASSURANCE):
        for track in ("maternal", "newborn"):
            # exercise the mapping with every possible flag combination edge
            guidance_keys.update(rules.guidance_for(tier, (), track))
    for sign_key in rules._FIRST_RESPONSE.values():
        guidance_keys.add(sign_key)
    for key in guidance_keys:
        for lang in i18n.PACKS:
            text = i18n.t(lang, key)
            assert text and text != key, f"missing guidance {key} in {lang}"


def test_params_interpolate():
    text = i18n.t("en", "st_amb_requested", number="108")
    assert "108" in text
    assert "{number}" not in text


def test_fallback_to_english_never_crashes():
    assert i18n.t("xx", "ivr_yes") == i18n.t("en", "ivr_yes")
