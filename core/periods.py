"""Period canonicalization — Rule Book §54.

Filings phrase the same fiscal year a dozen different ways: "FY2025",
"FY 2024-25", "Year ended March 31, 2025", a bare "2025" as a table
column header, "31-Mar-25", "H1 FY25". Two rows that are actually the
same period must resolve to the identical `period` string, or
retrieval silently misses (a row stored as "FY2025" never matches a
lookup for "Year ended March 31, 2025") — this is what makes the
multi-year-column fix in extraction/llm_cleanup.py actually usable
end to end, and it's why derivation.py and tools.py both canonicalize
through this module before touching the store.

Assumption this module bakes in, stated rather than hidden: a bare
year or a date is read as an *Indian* fiscal year (ending March 31),
labelled by its ending calendar year — e.g. "2025" or "31 March 2025"
-> FY2025. That matches every filing example in this project (SEBI,
IRDAI, Indian annual reports). It will mislabel a calendar-year filer.
If this pipeline ever ingests a non-Indian filing, this is the first
place to revisit.
"""

from __future__ import annotations

import re

_LTM_TTM_RE = re.compile(r"^(LTM|TTM)$", re.I)
_QUARTER_RE = re.compile(r"^(Q[1-4])\s*[- ]?\s*(?:FY\s*)?(\d{2,4})?$", re.I)
_HALF_RE = re.compile(r"^(H[12])\s*[- ]?\s*(?:FY\s*)?(\d{2,4})?$", re.I)
_YEAR_ENDED_RE = re.compile(r"(?:year|period|months)\s+ended\b.*?(\d{4})", re.I)
_FY_RE = re.compile(r"^FY\s*[- ]?\s*(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?$", re.I)
_YEAR_RANGE_RE = re.compile(r"^(\d{4})\s*[-/]\s*(\d{2,4})$")
_BARE_YEAR_RE = re.compile(r"^(\d{4})$")
_MONTH_NAMES = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_MONTH_DATE_RE = re.compile(rf"\b({_MONTH_NAMES})[a-z]*[.,/ -]+'?(\d{{2,4}})\b", re.I)
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./](\d{2,4})\b")


def _full_year(token: str) -> int:
    n = int(token)
    if n < 100:
        return 2000 + n if n < 50 else 1900 + n
    return n


def canonicalize_period(text: str | None) -> str:
    """Map filing period phrasing to canonical period metadata.

    Falls back to a cleaned, uppercased copy of the input when nothing
    recognizable is found, rather than fabricating a year — an
    unparsed period string is still a *consistent* key across calls,
    just one that won't reconcile with other phrasings of the same
    period (Rule 2: no silent inference beats a wrong guess)."""
    if not text or not text.strip():
        return ""
    raw = re.sub(r"\s+", " ", text.strip())

    m = _LTM_TTM_RE.fullmatch(raw)
    if m:
        return m.group(1).upper()

    m = _QUARTER_RE.fullmatch(raw)
    if m:
        year = f"FY{_full_year(m.group(2))}" if m.group(2) else ""
        return f"{m.group(1).upper()}{year}"

    m = _HALF_RE.fullmatch(raw)
    if m:
        year = f"FY{_full_year(m.group(2))}" if m.group(2) else ""
        return f"{m.group(1).upper()}{year}"

    m = _YEAR_ENDED_RE.search(raw)
    if m:
        return f"FY{_full_year(m.group(1))}"

    m = _FY_RE.fullmatch(raw)
    if m:
        end_token = m.group(2) or m.group(1)
        return f"FY{_full_year(end_token)}"

    m = _YEAR_RANGE_RE.fullmatch(raw)
    if m:
        return f"FY{_full_year(m.group(2))}"

    m = _BARE_YEAR_RE.fullmatch(raw)
    if m:
        return f"FY{m.group(1)}"

    m = _MONTH_DATE_RE.search(raw)
    if m:
        return f"FY{_full_year(m.group(2))}"

    m = _NUMERIC_DATE_RE.search(raw)
    if m:
        return f"FY{_full_year(m.group(1))}"

    return raw.upper()
