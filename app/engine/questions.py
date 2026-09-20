"""Fixed question sequences.

Content basis: the danger-sign categories published in India's own community
health worker training material (MoHFW *Training Manual on Newborn and Child
Health Services for ASHA*, *Notes for ASHA Trainers*), IMNCI newborn danger
signs, and standard published lists of danger signs in pregnancy (severe
bleeding, fits/convulsions, high fever, severe difficulty breathing, severe
headache with blurred vision, reduced fetal movement, water leaking, baby not
feeding, lethargy, fast breathing/chest indrawing, temperature instability,
jaundice of palms/soles, cord infection).

Nothing here is novel clinical criteria. Questions are phrased for a
non-medical person under stress and are answerable with yes/no/don't-know.

Severity classes:
    RED    - a "yes" (or an unclear answer) means immediate emergency.
    AMBER  - a "yes" (or an unclear answer) means see a health worker soon.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    id: str            # stable key used in logs and the rules engine
    track: str         # "maternal" | "newborn"
    severity: str      # "red" | "amber"
    sign_key: str      # danger-sign key used in guidance mapping
    order: int         # position in the fixed sequence


MATERNAL_QUESTIONS: tuple[Question, ...] = (
    Question("m_bleeding",  "maternal", "red",   "bleeding",       1),
    Question("m_fits",      "maternal", "red",   "fits",           2),
    Question("m_breathing", "maternal", "red",   "breathing",      3),
    Question("m_fever",     "maternal", "red",   "fever",          4),
    Question("m_headache",  "maternal", "red",   "headache_vision", 5),
    Question("m_pain",      "maternal", "red",   "abdominal_pain", 6),
    Question("m_movement",  "maternal", "red",   "fetal_movement", 7),
    Question("m_leaking",   "maternal", "red",   "water_leaking",  8),
    Question("m_swelling",  "maternal", "amber", "swelling",       9),
    Question("m_vomiting",  "maternal", "amber", "vomiting",      10),
)

NEWBORN_QUESTIONS: tuple[Question, ...] = (
    Question("n_feeding",   "newborn", "red",   "not_feeding",    1),
    Question("n_fits",      "newborn", "red",   "fits",           2),
    Question("n_breathing", "newborn", "red",   "fast_breathing", 3),
    Question("n_temp_hot",  "newborn", "red",   "body_hot",       4),
    Question("n_temp_cold", "newborn", "red",   "body_cold",      5),
    Question("n_sleepy",    "newborn", "red",   "lethargy",       6),
    Question("n_jaundice",  "newborn", "red",   "jaundice",       7),
    Question("n_cord",      "newborn", "amber", "cord_infection", 8),
    Question("n_stools",    "newborn", "amber", "loose_stools",   9),
)

QUESTIONS_BY_TRACK: dict[str, tuple[Question, ...]] = {
    "maternal": MATERNAL_QUESTIONS,
    "newborn": NEWBORN_QUESTIONS,
}

QUESTION_INDEX: dict[str, Question] = {
    q.id: q for q in (*MATERNAL_QUESTIONS, *NEWBORN_QUESTIONS)
}


def questions_for(track: str) -> tuple[Question, ...]:
    try:
        return QUESTIONS_BY_TRACK[track]
    except KeyError:
        raise ValueError(f"unknown track: {track!r}") from None
