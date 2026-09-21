"""Deterministic table-row parsing: raw Camelot/pdfplumber rows -> label +
value + period entries.

This is the real logic that used to live in extraction/llm_cleanup.py. That
file called an LLM (Ollama) to cosmetically polish row labels, but the call
was wrapped in try/except and fell back to this exact deterministic output
on any failure — so the LLM step was never load-bearing for correctness.
canonicalize_metric() (core/derivation.py, driven by core/rules.yaml's
canonical_terms aliases) already does the real label normalization
downstream. Dropping the LLM step removes a network dependency (Ollama)
from the ingest path without changing what gets stored.

Financial numbers are immutable: this module never invents, calculates, or
adjusts a value. It only classifies which numeric token in a row is which
period's figure.
"""

from __future__ import annotations

import re
from typing import Any

from .pdf_router import _looks_year  # same year/FY pattern the quality scorer already applies

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z])(?:\$|€|£|₹)?\s*"
    r"(?:\(\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*\)"
    r"|\(\s*\d+(?:\.\d+)?\s*\)"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?)"
)

_HEADER_LABEL_WORDS = {"particulars", "description", "details", "particulars (rs. in lakhs)", ""}


def _clean_cell(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell or "")).strip()


def _parse_number(token: str) -> float | int | None:
    token = token.strip().replace("$", "").replace("€", "").replace("£", "").replace("₹", "")
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("() ").replace(",", "")
    if not token:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    if negative:
        value = -value
    return int(value) if value.is_integer() else value


def _last_number(text: str) -> float | int | None:
    matches = list(_NUMBER_RE.finditer(text))
    if not matches:
        return None
    return _parse_number(matches[-1].group(0))


def _all_numbers(text: str) -> list[float | int]:
    return [n for n in (_parse_number(m.group(0)) for m in _NUMBER_RE.finditer(text)) if n is not None]


def _strip_numeric_tail(text: str) -> str:
    text = _clean_cell(text)
    matches = list(_NUMBER_RE.finditer(text))
    if not matches:
        return text
    label = text[:matches[-1].start()].strip(" $€£₹\t")
    return label.rstrip(" .:,-")


def _detect_year_header(rows: list[list[str]], scan_rows: int = 4) -> tuple[int, dict[int, str]] | None:
    """Look for a header row (e.g. "Particulars | 2025 | 2024") among the
    first few rows. Returns (header_row_index, {column_index: raw_period}),
    or None.

    Bar: 2+ year-like columns, relaxed to 1+ when the row's own label cell
    is a generic non-metric header word ("Particulars", blank) — a real
    line item is essentially never literally "Particulars", so a single
    year next to one of those words is still solid evidence, not a guess.
    """
    best: tuple[int, dict[int, str]] | None = None
    for row_index, row in enumerate(rows[:scan_rows]):
        cells = [_clean_cell(c) for c in (row or [])]
        if len(cells) < 2:
            continue
        candidate: dict[int, str] = {}
        for col_index, cell in enumerate(cells[1:], start=1):
            if cell and _looks_year(cell):
                candidate[col_index] = cell
        min_hits = 1 if cells[0].strip().lower() in _HEADER_LABEL_WORDS else 2
        if len(candidate) < min_hits:
            continue
        if best is None or len(candidate) > len(best[1]):
            best = (row_index, candidate)
    return best


def parse_rows(rows: list[list[str]]) -> list[dict]:
    """Parse a raw table's rows into label + value entries.

    Never mix periods, never silently infer: a multi-year statement puts
    more than one numeric token in a row (current year, prior year,
    sometimes restated), and picking "the last one" with nothing to check
    it against is a guess dressed up as parsing.

    Preferred path: a year/FY header row is detected. Every data row then
    yields ONE entry per populated column, each carrying that column's own
    `period_raw` — this is what lets a 2-3 year statement (the normal case
    for an annual report) get ingested correctly in one pass.

    Fallback: no confident header row. Take the row's last numeric token as
    the value, and flag the row `ambiguous_multi_period` when more than one
    candidate value was present, so callers can skip rather than guess.
    """
    header = _detect_year_header(rows)
    parsed: list[dict] = []

    if header:
        header_row_index, columns = header
        for row_index, row in enumerate(rows):
            if row_index == header_row_index:
                continue
            cells = [_clean_cell(c) for c in (row or [])]
            if not cells:
                continue
            label = _strip_numeric_tail(cells[0])
            if not label:
                continue
            for col_index, period_raw in columns.items():
                if col_index >= len(cells):
                    continue
                value = _last_number(cells[col_index])
                if value is None:
                    continue
                parsed.append({
                    "row_id": row_index, "metric_raw": label, "value": value,
                    "period_raw": period_raw, "ambiguous_multi_period": False, "all_values": None,
                })
        if parsed:
            return parsed
        # Header row detected but nothing usable aligned under it (e.g. every
        # data row was a single merged cell) — fall through to the legacy path.

    for row_index, row in enumerate(rows):
        cells = [_clean_cell(c) for c in (row or [])]
        if not cells:
            continue

        label = _strip_numeric_tail(cells[0])
        if not label:
            continue

        numbers_in_label_cell = _all_numbers(cells[0])
        other_cell_numbers = [n for cell in cells[1:] for n in _all_numbers(cell)]
        all_numbers = numbers_in_label_cell + other_cell_numbers

        value = all_numbers[-1] if all_numbers else None
        parsed.append({
            "row_id": row_index,
            "metric_raw": label,
            "value": value,
            "period_raw": None,
            "ambiguous_multi_period": len(all_numbers) > 1,
            "all_values": all_numbers if len(all_numbers) > 1 else None,
        })
    return parsed
