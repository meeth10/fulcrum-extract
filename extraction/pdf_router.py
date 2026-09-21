"""Robust financial-table extraction.

Camelot is treated as a candidate generator, not an oracle. Financial PDFs
regularly produce a high parser score while still collapsing year columns,
merging numeric cells, or swallowing footnote text. We therefore run multiple
Camelot strategies, score the result on financial structure, and use
coordinate-aware pdfplumber reconstruction when the table geometry is weak.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional
import re

import pdfplumber


@dataclass
class ExtractedTable:
    page: int
    rows: list[list[str]]
    method: str
    confidence: float
    table_caption: Optional[str] = None
    quality_score: float = 0.0
    warnings: list[str] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


MIN_CHARS_FOR_TEXT_PAGE = 40
MIN_ACCEPTABLE_SCORE = 0.42
NUMERIC_RE = re.compile(r"^\(?[-+]?\s*\d[\d,]*(?:\.\d+)?%?\)?$")
YEAR_RE = re.compile(r"(?:19|20)\d{2}(?:[-/](?:19|20)?\d{2})?$")
FINANCIAL_TERMS = {
    "revenue", "sales", "income", "profit", "loss", "assets", "liabilities",
    "equity", "cash", "borrowings", "debt", "receivables", "payables",
    "inventory", "expenses", "tax", "ebit", "ebitda", "operating", "capital",
    "depreciation", "amortisation", "amortization", "dividend", "earnings",
}
TOTAL_TERMS = {"total", "subtotal", "net", "profit", "loss", "ebit", "ebitda"}


def _get_page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def _validate_page(pdf_path: str, page_number: int) -> None:
    count = _get_page_count(pdf_path)
    if page_number < 1 or page_number > count:
        raise ValueError(f"Page {page_number} doesn't exist — this PDF has {count} page(s).")


def _clean_rows(rows) -> list[list[str]]:
    cleaned: list[list[str]] = []
    for row in rows or []:
        r = ["" if v is None else re.sub(r"\s+", " ", str(v)).strip() for v in row]
        while r and not r[-1]:
            r.pop()
        if any(r):
            cleaned.append(r)
    return cleaned


def _looks_numeric(value: str) -> bool:
    s = value.strip().replace("−", "-")
    if s.lower() in {"n/a", "na"}:
        return False
    if s in {"", "-", "—", "–"}:
        return True
    return bool(NUMERIC_RE.match(s.replace(" ", "")))


EMBEDDED_NUMERIC_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\(?[-+]?\s*\d[\d,]*(?:\.\d+)?%?\)?"
    r"(?![A-Za-z0-9])"
)


def _embedded_numeric_count(value: str) -> int:
    """Count numeric values embedded inside otherwise textual cells."""
    return len(EMBEDDED_NUMERIC_RE.findall(value.replace("−", "-")))


def _looks_year(value: str) -> bool:
    s = value.strip().replace(" ", "")
    if YEAR_RE.match(s):
        return True
    # Common financial headers such as "FY25" or "FY 2025-26".
    return bool(re.match(r"^FY\s*(?:19|20)?\d{2}(?:[-/]\s*(?:19|20)?\d{2})?$", value.strip(), re.I))


def _table_stats(rows: list[list[str]]) -> dict[str, float | int]:
    rows = _clean_rows(rows)
    widths = [len(r) for r in rows if r]
    nonempty_widths = [sum(bool(x.strip()) for x in r) for r in rows]
    width_mode = max(set(nonempty_widths), key=nonempty_widths.count) if nonempty_widths else 0
    stable_rows = sum(w == width_mode for w in nonempty_widths) / max(len(nonempty_widths), 1)
    max_width = max(nonempty_widths, default=0)
    numeric_counts = [sum(_looks_numeric(c) for c in r) for r in rows]
    numeric_cells = sum(numeric_counts)
    numeric_rows = sum(n >= 1 for n in numeric_counts)
    multi_numeric_rows = sum(n >= 2 for n in numeric_counts)

    embedded_numeric_counts = [
        sum(_embedded_numeric_count(c) for c in r)
        for r in rows
    ]
    embedded_numeric_cells = sum(embedded_numeric_counts)
    embedded_numeric_rows = sum(n >= 1 for n in embedded_numeric_counts)
    multi_embedded_numeric_rows = sum(n >= 2 for n in embedded_numeric_counts)
    year_hits = sum(_looks_year(c) for r in rows[:4] for c in r)
    text = " ".join(c.lower() for r in rows for c in r)
    term_hits = sum(text.count(term) for term in FINANCIAL_TERMS)
    total_hits = sum(text.count(term) for term in TOTAL_TERMS)
    label_rows = sum(bool(r and any(ch.isalpha() for ch in r[0])) for r in rows)
    return {
        "row_count": len(rows),
        "max_width": max_width,
        "stable_rows": stable_rows,
        "numeric_cells": numeric_cells,
        "numeric_rows": numeric_rows,
        "multi_numeric_rows": multi_numeric_rows,
        "embedded_numeric_cells": embedded_numeric_cells,
        "embedded_numeric_rows": embedded_numeric_rows,
        "multi_embedded_numeric_rows": multi_embedded_numeric_rows,
        "year_hits": year_hits,
        "term_hits": term_hits,
        "total_hits": total_hits,
        "label_rows": label_rows,
        "width_mode": width_mode,
        "mean_width": sum(widths) / max(len(widths), 1),
    }


def _quality_score(rows: list[list[str]], method_score: float = 0.0) -> tuple[float, list[str]]:
    rows = _clean_rows(rows)
    warnings: list[str] = []
    if not rows:
        return 0.0, ["empty_table"]

    s = _table_stats(rows)
    row_count = int(s["row_count"])
    max_width = int(s["max_width"])
    stable_rows = float(s["stable_rows"])
    numeric_cells = int(s["numeric_cells"])
    numeric_rows = int(s["numeric_rows"])
    multi_numeric_rows = int(s["multi_numeric_rows"])
    embedded_numeric_cells = int(s["embedded_numeric_cells"])
    embedded_numeric_rows = int(s["embedded_numeric_rows"])
    multi_embedded_numeric_rows = int(s["multi_embedded_numeric_rows"])
    year_hits = int(s["year_hits"])
    term_hits = int(s["term_hits"])
    total_hits = int(s["total_hits"])
    label_rows = int(s["label_rows"])

    score = 0.0
    score += min(method_score / 100.0, 1.0) * 0.10
    score += min(max_width / 6.0, 1.0) * 0.14
    score += stable_rows * 0.14
    score += min(numeric_cells / max(row_count * 2, 1), 1.0) * 0.20
    score += min(numeric_rows / max(row_count, 1), 1.0) * 0.10
    score += min(multi_numeric_rows / max(row_count, 1), 1.0) * 0.12
    score += min(year_hits / 2.0, 1.0) * 0.08
    score += min(term_hits / 8.0, 1.0) * 0.09
    score += min(total_hits / 3.0, 1.0) * 0.03

    if max_width <= 1 and (
        numeric_cells > 0
        or embedded_numeric_cells > 0
        or embedded_numeric_rows >= 2
        or multi_embedded_numeric_rows > 0
    ):
        warnings.append("collapsed_columns")
        score -= 0.35
    if max_width >= 2 and multi_numeric_rows / max(row_count, 1) < 0.20:
        warnings.append("mostly_single_value_rows")
        score -= 0.12
    if stable_rows < 0.55:
        warnings.append("unstable_column_count")
        score -= 0.10
    if numeric_cells == 0:
        warnings.append("no_numeric_cells")
        score -= 0.25
    if numeric_rows < 2:
        warnings.append("too_few_numeric_rows")
        score -= 0.15
    if label_rows / max(row_count, 1) < 0.45:
        warnings.append("weak_row_labels")
        score -= 0.08
    if year_hits == 0:
        warnings.append("no_year_header_detected")

    return max(0.0, min(score, 1.0)), warnings


def _camelot_candidates(pdf_path: str, page_number: int) -> list[ExtractedTable]:
    import camelot

    candidates: list[ExtractedTable] = []
    strategies = [
        ("camelot_lattice", "lattice", {"line_scale": 40}),
        ("camelot_lattice_tight", "lattice", {"line_scale": 60, "process_background": True}),
        ("camelot_stream", "stream", {"row_tol": 8, "column_tol": 12, "split_text": True}),
        ("camelot_stream_tight", "stream", {"row_tol": 4, "column_tol": 8, "split_text": True}),
        ("camelot_stream_loose", "stream", {"row_tol": 10, "column_tol": 20, "split_text": True}),
    ]

    for label, flavor, kwargs in strategies:
        try:
            tables = camelot.read_pdf(pdf_path, pages=str(page_number), flavor=flavor, **kwargs)
        except Exception:
            continue
        for t in tables:
            raw_acc = float(t.parsing_report.get("accuracy", 0.0))
            rows = _clean_rows(t.df.values.tolist())
            q, warnings = _quality_score(rows, raw_acc)
            candidates.append(ExtractedTable(
                page=page_number,
                rows=rows,
                method=label,
                confidence=q,
                quality_score=q,
                warnings=warnings,
            ))
    return candidates


def _group_words_into_lines(words: list[dict], y_tol: float = 3.0) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        target = None
        for line in lines:
            avg_top = sum(w["top"] for w in line) / len(line)
            if abs(word["top"] - avg_top) <= y_tol:
                target = line
                break
        if target is None:
            lines.append([word])
        else:
            target.append(word)
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _coordinate_reconstruct(pdf_path: str, page_number: int) -> Optional[ExtractedTable]:
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number - 1]
        words = page.extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False)

    if not words:
        return None

    lines = _group_words_into_lines(words)
    numeric_x: list[float] = []
    for line in lines:
        for w in line:
            if _looks_numeric(w["text"]):
                numeric_x.append(float(w["x0"]))

    if not numeric_x:
        return None

    # Infer numeric column anchors from repeated x coordinates across rows.
    anchors: list[float] = []
    for x in sorted(numeric_x):
        if not anchors or abs(x - anchors[-1]) > 16:
            anchors.append(x)
        else:
            anchors[-1] = (anchors[-1] + x) / 2
    anchors = anchors[:8]

    rows: list[list[str]] = []
    for line in lines:
        numeric_words = [w for w in line if _looks_numeric(w["text"])]
        if not numeric_words:
            continue

        first_num = min(float(w["x0"]) for w in numeric_words)
        label_words = [w["text"] for w in line if float(w["x0"]) < first_num - 4]
        label = " ".join(label_words).strip()
        if not label:
            continue

        values = [""] * len(anchors)
        for w in numeric_words:
            idx = min(range(len(anchors)), key=lambda i: abs(float(w["x0"]) - anchors[i]))
            if not values[idx]:
                values[idx] = w["text"]
            else:
                values[idx] += " " + w["text"]

        # Remove empty trailing columns but retain interior gaps.
        while values and not values[-1]:
            values.pop()
        if values:
            rows.append([label, *values])

    if len(rows) < 2:
        return None

    q, warnings = _quality_score(rows, 55.0)
    warnings.append("coordinate_reconstruction")
    return ExtractedTable(
        page=page_number,
        rows=rows,
        method="pdfplumber_coordinates",
        confidence=q,
        quality_score=q,
        warnings=warnings,
    )


def extract_page_tables(pdf_path: str, page_number: int) -> list[ExtractedTable]:
    _validate_page(pdf_path, page_number)

    candidates = _camelot_candidates(pdf_path, page_number)
    coord = _coordinate_reconstruct(pdf_path, page_number)
    if coord:
        candidates.append(coord)

    if not candidates:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[page_number - 1]
            settings_list = [
                {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
                {"vertical_strategy": "text", "horizontal_strategy": "text", "text_x_tolerance": 2, "text_y_tolerance": 3},
            ]
            for settings in settings_list:
                try:
                    tables = page.extract_tables(table_settings=settings)
                except Exception:
                    continue
                for table in tables:
                    rows = _clean_rows(table)
                    q, warnings = _quality_score(rows, 40.0)
                    candidates.append(ExtractedTable(
                        page=page_number,
                        rows=rows,
                        method="pdfplumber",
                        confidence=q,
                        quality_score=q,
                        warnings=warnings,
                    ))

    if not candidates:
        return []

    # Discard structurally weak parses unless there is no alternative at all.
    candidates.sort(key=lambda t: (t.quality_score, t.confidence, len(t.rows)), reverse=True)
    viable = [c for c in candidates if c.quality_score >= MIN_ACCEPTABLE_SCORE]
    if viable:
        candidates = viable

    best = candidates[0]
    unique: list[ExtractedTable] = [best]
    for candidate in candidates[1:]:
        if candidate.quality_score < max(best.quality_score - 0.12, MIN_ACCEPTABLE_SCORE):
            continue
        shape_a = (len(best.rows), max((len(r) for r in best.rows), default=0))
        shape_b = (len(candidate.rows), max((len(r) for r in candidate.rows), default=0))
        if shape_a != shape_b or candidate.method != best.method:
            unique.append(candidate)
        if len(unique) >= 2:
            break
    return unique


def page_needs_ocr(pdf_path: str, page_number: int) -> bool:
    _validate_page(pdf_path, page_number)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number - 1]
        text = page.extract_text() or ""
        return len(text.strip()) < MIN_CHARS_FOR_TEXT_PAGE
