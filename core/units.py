"""Unit and scale normalization — Rule Book §6 (normalize units before
calculations) and §55 (currency / unit rules).

Filing values arrive in crore, lakh, million, thousand, or absolute
terms, in whatever currency the filing uses. `derivation.py` should
never have to reason about scale itself — this module owns that.

Design constraint: when every input to a formula already shares the
identical unit string (the common case — one statement, one filing,
one presentation), `derivation.py` skips this module entirely and
uses raw values, so a derived figure stays in the filing's own scale
instead of being silently blown out to an absolute rupee count. This
module is only invoked when units genuinely differ, which is exactly
the case Rule 6 exists for.
"""

from __future__ import annotations

import re

# How many absolute (scale=1) units one token represents.
_SCALE: dict[str, float] = {
    "thousand": 1_000, "thousands": 1_000, "k": 1_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "million": 1_000_000, "millions": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000,
    "billion": 1_000_000_000, "billions": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "unit": 1, "units": 1, "absolute": 1,
}

_CURRENCY_ALIASES: dict[str, str] = {
    "inr": "INR", "rs": "INR", "rs.": "INR", "₹": "INR", "rupee": "INR", "rupees": "INR",
    "usd": "USD", "$": "USD", "dollar": "USD", "dollars": "USD",
    "eur": "EUR", "€": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "£": "GBP", "pound": "GBP", "pounds": "GBP",
}

_NON_MONETARY = {"%", "percent", "x", "days", "", "unspecified"}


def parse_unit(unit_str: str | None) -> tuple[str | None, float]:
    """Parse a free-text unit string like 'INR crore' or '₹ in Lakhs'
    into (currency, scale_multiplier).

    An empty, missing, or unrecognized string returns (None, 1.0) —
    per Rule 56, this is "unspecified", not a claim that the value is
    already absolute. Callers that need to know the difference should
    check the currency, not assume a bare 1.0 scale means anything.
    """
    if not unit_str or not unit_str.strip():
        return None, 1.0
    cleaned = unit_str.strip().lower()
    if cleaned == "unspecified":
        return None, 1.0

    currency = None
    for token, canon in _CURRENCY_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", cleaned):
            currency = canon
            break

    scale = 1.0
    for token in sorted(_SCALE, key=len, reverse=True):
        if re.search(rf"\b{re.escape(token)}\b", cleaned):
            scale = _SCALE[token]
            break

    return currency, scale


def to_absolute(value: float, unit_str: str | None) -> tuple[float, str | None]:
    """Convert a value to absolute (scale=1) terms. Returns (value, currency)."""
    currency, scale = parse_unit(unit_str)
    return value * scale, currency


def is_monetary(unit_str: str | None) -> bool:
    """False for ratio/percentage/day-count units that were never a
    currency amount to begin with — these must never go through scale
    conversion (there is nothing to convert)."""
    return str(unit_str or "").strip().lower() not in _NON_MONETARY


def compatible(unit_a: str | None, unit_b: str | None) -> bool:
    """Two units may be combined arithmetically only if their
    currencies agree, or at least one side is unspecified. Different
    known currencies must never be silently combined — Rule 5's
    never-mix-scope spirit applied to currency instead of entity."""
    currency_a, _ = parse_unit(unit_a)
    currency_b, _ = parse_unit(unit_b)
    if currency_a and currency_b and currency_a != currency_b:
        return False
    return True
