"""The rules engine: determinism, fail-safe defaults, tier mapping.

These tests encode the single most important behaviour in the system:
an unclear answer NEVER resolves toward a lower-urgency outcome.
"""
from app.engine import rules
from app.engine.questions import MATERNAL_QUESTIONS, NEWBORN_QUESTIONS


def _all_no(track):
    qs = MATERNAL_QUESTIONS if track == "maternal" else NEWBORN_QUESTIONS
    return {q.id: rules.NO for q in qs}


class TestDeterminism:
    def test_same_input_same_output(self):
        answers = _all_no("maternal")
        answers["m_bleeding"] = rules.YES
        first = rules.evaluate("maternal", answers)
        for _ in range(50):
            assert rules.evaluate("maternal", answers) == first

    def test_pure_function_no_side_effects(self):
        answers = _all_no("newborn")
        snapshot = dict(answers)
        rules.evaluate("newborn", answers)
        assert answers == snapshot  # engine must not mutate its input


class TestEmergencyTier:
    def test_red_yes_is_emergency(self):
        for q in MATERNAL_QUESTIONS:
            if q.severity != "red":
                continue
            answers = _all_no("maternal")
            answers[q.id] = rules.YES
            d = rules.evaluate("maternal", answers)
            assert d.tier == rules.TIER_EMERGENCY, q.id
            assert q.sign_key in d.flagged

    def test_red_unclear_is_emergency(self):
        """THE critical safety property: ambiguity on a red sign escalates."""
        for q in MATERNAL_QUESTIONS:
            if q.severity != "red":
                continue
            answers = _all_no("maternal")
            answers[q.id] = rules.UNCLEAR
            d = rules.evaluate("maternal", answers)
            assert d.tier == rules.TIER_EMERGENCY, q.id

    def test_newborn_red_unclear_is_emergency(self):
        for q in NEWBORN_QUESTIONS:
            if q.severity != "red":
                continue
            answers = _all_no("newborn")
            answers[q.id] = rules.UNCLEAR
            d = rules.evaluate("newborn", answers)
            assert d.tier == rules.TIER_EMERGENCY, q.id

    def test_missing_answer_treated_as_unclear(self):
        """An unanswered question can never resolve downward."""
        answers = _all_no("maternal")
        del answers["m_fits"]
        d = rules.evaluate("maternal", answers)
        assert d.tier == rules.TIER_EMERGENCY

    def test_none_value_treated_as_unclear(self):
        answers = _all_no("maternal")
        answers["m_bleeding"] = None
        d = rules.evaluate("maternal", answers)
        assert d.tier == rules.TIER_EMERGENCY

    def test_unrecognised_value_treated_as_positive(self):
        answers = _all_no("newborn")
        answers["n_fits"] = "maybe?"
        d = rules.evaluate("newborn", answers)
        assert d.tier == rules.TIER_EMERGENCY


class TestUrgentTier:
    def test_amber_yes_is_urgent(self):
        answers = _all_no("maternal")
        answers["m_swelling"] = rules.YES
        d = rules.evaluate("maternal", answers)
        assert d.tier == rules.TIER_URGENT
        assert "swelling" in d.flagged

    def test_amber_unclear_is_urgent(self):
        answers = _all_no("newborn")
        answers["n_cord"] = rules.UNCLEAR
        d = rules.evaluate("newborn", answers)
        assert d.tier == rules.TIER_URGENT

    def test_repeated_unclear_never_reassurance(self):
        """Two+ unclear answers on amber questions still escalate."""
        answers = _all_no("maternal")
        answers["m_swelling"] = rules.UNCLEAR
        answers["m_vomiting"] = rules.UNCLEAR
        d = rules.evaluate("maternal", answers)
        assert d.tier == rules.TIER_URGENT

    def test_red_beats_amber(self):
        answers = _all_no("maternal")
        answers["m_swelling"] = rules.YES
        answers["m_bleeding"] = rules.YES
        d = rules.evaluate("maternal", answers)
        assert d.tier == rules.TIER_EMERGENCY


class TestReassuranceTier:
    def test_all_clear_no_is_reassurance(self):
        d = rules.evaluate("maternal", _all_no("maternal"))
        assert d.tier == rules.TIER_REASSURANCE
        assert d.flagged == ()
        assert d.unclear_count == 0
        assert d.rule == "ALL_CLEAR_NEGATIVE"

    def test_reassurance_still_carries_guidance(self):
        """The likely-fine tier must never feel dismissive: real guidance."""
        d = rules.evaluate("newborn", _all_no("newborn"))
        assert d.tier == rules.TIER_REASSURANCE
        assert "r_not_dismissive" in d.guidance
        assert "g_breastfeed" in d.guidance
        assert "r_danger_awareness_baby" in d.guidance


class TestGuidanceMapping:
    def test_emergency_guidance_starts_with_stay_on_line(self):
        answers = _all_no("maternal")
        answers["m_bleeding"] = rules.YES
        d = rules.evaluate("maternal", answers)
        assert d.guidance[0] == "g_stay_on_line"
        assert "g_bleeding" in d.guidance
        assert "g_ready_transport" in d.guidance

    def test_urgent_guidance_includes_watch_and_callback(self):
        answers = _all_no("maternal")
        answers["m_vomiting"] = rules.YES
        d = rules.evaluate("maternal", answers)
        assert "u_contact_today" in d.guidance
        assert "u_watch_danger" in d.guidance
        assert "u_call_back" in d.guidance


class TestFailSafe:
    def test_fail_safe_is_emergency(self):
        d = rules.evaluate_fail_safe("maternal")
        assert d.tier == rules.TIER_EMERGENCY
        assert d.rule == "FAIL_SAFE_ENGINE_ERROR"

    def test_unknown_track_raises_and_fail_safe_covers(self):
        """evaluate() rejects an unknown track; the service layer catches and
        applies evaluate_fail_safe -- verified here at the unit level."""
        import pytest
        with pytest.raises(ValueError):
            rules.evaluate("geriatric", {})
        d = rules.evaluate_fail_safe(None)
        assert d.tier == rules.TIER_EMERGENCY
