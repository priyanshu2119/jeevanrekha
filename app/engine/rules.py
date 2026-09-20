"""The deterministic triage rules engine.

This module is the single source of truth for "does this combination of
answers count as an emergency". It is:

* PURE      - no I/O, no database, no network, no randomness, no LLM.
* TOTAL     - every possible input maps to exactly one of the three tiers.
* FAIL-SAFE - an unclear/ambiguous/missing answer is treated as a positive
              red-flag answer. It can never resolve toward a lower tier.
* AUDITABLE - the decision carries the exact rule that fired.

Cost asymmetry encoded in every rule below: a false alarm costs a wasted
trip; a false reassurance can cost a life.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .questions import QUESTION_INDEX, Question, questions_for

# Answer values understood by the engine.
YES = "yes"
NO = "no"
UNCLEAR = "unclear"

TIER_EMERGENCY = "emergency"
TIER_URGENT = "urgent"
TIER_REASSURANCE = "reassurance"


@dataclass(frozen=True)
class TriageDecision:
    tier: str
    flagged: tuple[str, ...]          # danger-sign keys that fired
    unclear_count: int
    rule: str                         # human-readable rule that decided this
    guidance: tuple[str, ...] = field(default=())


def _positive(value: str | None) -> bool:
    """Safety default: anything that is not an explicit clear 'no' counts as
    positive. This includes YES, UNCLEAR, None (unanswered) and any
    unrecognised value -- ambiguity never resolves downward."""
    return value != NO


def evaluate(track: str, answers: dict[str, str | None]) -> TriageDecision:
    """Evaluate a completed (or partially completed) answer set.

    ``answers`` maps question_id -> "yes" | "no" | "unclear" | None.
    Missing questions are treated as unclear (fail-safe), so calling this
    early can only ever over-escalate, never under-escalate.
    """
    questions: tuple[Question, ...] = questions_for(track)

    red_flags: list[str] = []
    amber_flags: list[str] = []
    unclear_count = 0

    for q in questions:
        value = answers.get(q.id)
        if value == UNCLEAR or value is None:
            unclear_count += 1

        if not _positive(value):
            continue  # explicit clear "no"

        if q.severity == "red":
            red_flags.append(q.sign_key)
        else:
            amber_flags.append(q.sign_key)

    if red_flags:
        tier = TIER_EMERGENCY
        rule = "RED_POSITIVE:" + ",".join(red_flags)
    elif amber_flags:
        tier = TIER_URGENT
        rule = "AMBER_POSITIVE:" + ",".join(amber_flags)
    elif unclear_count >= 2:
        # Belt-and-braces: a caller who is repeatedly unsure never lands in
        # the reassurance tier even if no single question escalated.
        tier = TIER_URGENT
        rule = "REPEATED_UNCLEAR"
    else:
        tier = TIER_REASSURANCE
        rule = "ALL_CLEAR_NEGATIVE"

    flagged = tuple(red_flags + amber_flags)
    return TriageDecision(
        tier=tier,
        flagged=flagged,
        unclear_count=unclear_count,
        rule=rule,
        guidance=guidance_for(tier, flagged, track),
    )


def evaluate_fail_safe(track: str | None) -> TriageDecision:
    """Used only when the normal engine path itself raised. The caller still
    gets a defined outcome -- the most protective one -- and the incident is
    logged by the caller of this function."""
    return TriageDecision(
        tier=TIER_EMERGENCY,
        flagged=(),
        unclear_count=0,
        rule="FAIL_SAFE_ENGINE_ERROR",
        guidance=guidance_for(TIER_EMERGENCY, (), track or "maternal"),
    )


# --------------------------------------------------------------------------
# Guidance mapping (keys resolved to text by i18n; content is standard
# publicly-available health-education material, never clinical dosing)
# --------------------------------------------------------------------------

_FIRST_RESPONSE = {
    "bleeding": "g_bleeding",
    "fits": "g_fits",
    "breathing": "g_breathing",
    "fever": "g_fever",
    "headache_vision": "g_headache",
    "abdominal_pain": "g_pain",
    "fetal_movement": "g_movement",
    "water_leaking": "g_leaking",
    "not_feeding": "g_not_feeding",
    "fast_breathing": "g_fast_breathing",
    "body_hot": "g_hot_baby",
    "body_cold": "g_cold_baby",
    "lethargy": "g_sleepy_baby",
    "jaundice": "g_jaundice",
    "cord_infection": "g_cord",
    "loose_stools": "g_stools",
    "swelling": "g_swelling",
    "vomiting": "g_vomiting",
}


def guidance_for(tier: str, flagged: tuple[str, ...] | list[str], track: str) -> tuple[str, ...]:
    keys: list[str] = []
    if tier == TIER_EMERGENCY:
        keys.append("g_stay_on_line")
        for sign in flagged:
            k = _FIRST_RESPONSE.get(sign)
            if k and k not in keys:
                keys.append(k)
        keys.append("g_no_food_drink")
        keys.append("g_ready_transport")
    elif tier == TIER_URGENT:
        keys.append("u_contact_today")
        for sign in flagged:
            k = _FIRST_RESPONSE.get(sign)
            if k and k not in keys:
                keys.append(k)
        keys.append("u_watch_danger")
        keys.append("u_call_back")
    else:
        keys.append("r_not_dismissive")
        if track == "newborn":
            keys += ["g_breastfeed", "g_keep_warm", "g_cord_care", "g_vaccine", "r_danger_awareness_baby"]
        else:
            keys += ["g_rest", "g_meals", "g_iron", "g_fluids", "g_anc", "g_delivery_plan", "r_danger_awareness"]
    return tuple(keys)


__all__ = [
    "YES", "NO", "UNCLEAR",
    "TIER_EMERGENCY", "TIER_URGENT", "TIER_REASSURANCE",
    "TriageDecision", "evaluate", "evaluate_fail_safe", "guidance_for",
    "QUESTION_INDEX",
]
