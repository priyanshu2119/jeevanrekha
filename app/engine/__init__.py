"""Deterministic triage engine package.

The engine is pure Python data + pure functions. No I/O, no database, no
network, no randomness, no language models. Every decision is reproducible
from the logged answers alone.
"""
from . import i18n, questions, rules

__all__ = ["i18n", "questions", "rules"]
