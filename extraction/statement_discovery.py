"""High-recall deterministic discovery with diagnostic validation gates."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import re
from typing import Iterable

import pdfplumber

from .pdf_router import _looks_numeric
from .headings import page_headings, detect_consolidation, heading_matches_title


@dataclass(frozen=True)
class StatementCandidate:
    statement: str
    page: int
    score: float
    matched_terms: tuple[str, ...]
    text_preview: str
    status: str = "TITLE_ONLY"
    title_match: str | None = None
    gate2_label_hits: int = 0
    gate2_structured_rows: int = 0
    gate3_numeric_columns: int = 0
    gate3_garbage_ratio: float = 1.0
    main_cluster: bool = True
    outside_cluster_duplicate: bool = False
    review_flag: str | None = None
    detected_heading: str | None = None
    heading_confirmed: bool = False
    consolidated: bool | None = None


STATEMENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "balance_sheet": (
        r"balance sheet",
        r"statement of financial position",
        r"consolidated balance sheets?",
        r"consolidated statements? of financial position",
        r"assets\s+and\s+liabilities",
    ),
    "income_statement": (
        r"income statement",
        r"statement of (?:profit and loss|operations)",
        r"profit and loss account",
        r"consolidated statements? of (?:operations|income|profit and loss)",
        r"profit\s*&\s*loss",
        r"statement of comprehensive income",
    ),
    "cash_flow": (
        r"cash flow statement",
        r"statement of cash flows?",
        r"consolidated statements? of cash flows?",
        r"cash flows? from operating activities",
        r"cash flows? from investing activities",
        r"cash flows? from financing activities",
    ),
}

SUPPORT_TERMS: dict[str, tuple[str, ...]] = {
    "balance_sheet": (
        "total assets", "total liabilities", "shareholders' equity",
        "shareholders’ equity", "current assets", "current liabilities",
        "accounts receivable", "accounts payable",
    ),
    "income_statement": (
        "revenue", "net sales", "operating income", "gross profit",
        "profit before tax", "net income", "profit after tax", "ebitda",
    ),
    "cash_flow": (
        "operating activities", "investing activities", "financing activities",
        "net cash", "cash and cash equivalents", "capital expenditures",
    ),
}

TITLE_LISTS = {
    "balance_sheet": (
        "CONSOLIDATED BALANCE SHEETS", "CONSOLIDATED BALANCE SHEET", "BALANCE SHEET",
        "STATEMENT OF FINANCIAL POSITION", "CONSOLIDATED STATEMENTS OF FINANCIAL POSITION",
        "STANDALONE BALANCE SHEET",
    ),
    "income_statement": (
        "CONSOLIDATED STATEMENT OF PROFIT AND LOSS", "STATEMENT OF PROFIT AND LOSS",
        "CONSOLIDATED INCOME STATEMENT", "INCOME STATEMENT", "STATEMENT OF OPERATIONS",
        "CONSOLIDATED STATEMENTS OF OPERATIONS", "CONSOLIDATED STATEMENTS OF INCOME",
    ),
    "cash_flow": (
        "CONSOLIDATED STATEMENT OF CASH FLOWS", "STATEMENT OF CASH FLOWS",
        "CASH FLOW STATEMENT", "CONSOLIDATED CASH FLOW STATEMENT",
    ),
}

STATEMENT_LABELS: dict[str, tuple[str, ...]] = {
    "balance_sheet": (
        "cash and cash equivalents", "total assets", "total liabilities", "total equity",
        "shareholders' equity", "current assets", "current liabilities", "accounts receivable",
        "accounts payable", "borrowings", "debt", "inventory",
    ),
    "income_statement": (
        "revenue", "net sales", "gross profit", "operating income", "operating profit",
        "profit before tax", "profit after tax", "net income", "profit for the year", "ebitda",
        "finance cost", "income tax",
    ),
    "cash_flow": (
        "net cash from operating activities", "cash generated from operations",
        "operating activities", "investing activities", "financing activities", "net cash",
        "capital expenditures", "purchase of property, plant and equipment", "depreciation",
        "cash and cash equivalents",
    ),
}

STATEMENT_ORDER = ("balance_sheet", "income_statement", "cash_flow")
CLUSTER_RADIUS = 5
STATEMENT_LOCALITY_RADIUS = 2


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _title_pattern(title: str) -> re.Pattern[str]:
    parts = [re.escape(x) for x in _normalise(title).split(" ")]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def _match_title(text: str, statement: str) -> str | None:
    for title in TITLE_LISTS[statement]:
        if _title_pattern(title).search(text):
            return title
    for pattern in STATEMENT_PATTERNS[statement]:
        if re.search(pattern, _normalise(text), flags=re.IGNORECASE):
            return pattern
    return None


def _score_page(text: str, statement: str) -> tuple[float, tuple[str, ...]]:
    normalised = _normalise(text)
    matched: list[str] = []
    score = 0.0
    for pattern in STATEMENT_PATTERNS[statement]:
        if re.search(pattern, normalised, flags=re.IGNORECASE):
            matched.append(pattern)
            score += 10.0
    for term in SUPPORT_TERMS[statement]:
        if term in normalised:
            matched.append(term)
            score += 1.5
    number_hits = len(re.findall(r"(?:\(?\s*[-$€£₹]?\s*[\d,]+(?:\.\d+)?\s*\)?)", text))
    score += min(number_hits, 20) * 0.15
    if len(normalised) >= 600:
        score += 2.0
    elif len(normalised) >= 250:
        score += 1.0
    return score, tuple(matched)


def _gate2(rows: list[list[str]], statement: str) -> tuple[int, int]:
    labels = {_normalise(x) for x in STATEMENT_LABELS[statement]}
    label_hits = 0
    structured_rows = 0
    for row in rows:
        if not row:
            continue
        cells = [str(x or "").strip() for x in row]
        if not cells or not any(ch.isalpha() for ch in cells[0]):
            continue
        if sum(_looks_numeric(c) for c in cells[1:]):
            structured_rows += 1
            label = _normalise(cells[0])
            if any(term in label or label in term for term in labels):
                label_hits += 1
    return label_hits, structured_rows


def _gate3(rows: list[list[str]]) -> tuple[int, float]:
    structured = []
    for row in rows:
        if not row or not any(ch.isalpha() for ch in str(row[0] or "")):
            continue
        trailing = [str(x or "").strip() for x in row[1:]]
        if trailing:
            structured.append(trailing)
    if not structured:
        return 0, 1.0
    max_cols = max(len(r) for r in structured)
    numeric_by_col = [0] * max_cols
    garbage = 0
    nonempty = 0
    for row in structured:
        for i, cell in enumerate(row):
            if not cell:
                continue
            nonempty += 1
            if _looks_numeric(cell):
                numeric_by_col[i] += 1
            else:
                garbage += 1
    min_rows = max(2, int(len(structured) * 0.5 + 0.999))
    numeric_columns = sum(n >= min_rows for n in numeric_by_col)
    return numeric_columns, garbage / max(nonempty, 1)


def _title_regex_for_match(title: str, statement: str) -> re.Pattern[str]:
    """The same pattern _match_title used to find this title in the page's
    full text, reusable against a single heading line's normalised text."""
    if title in TITLE_LISTS[statement]:
        return _title_pattern(title)
    return re.compile(title, re.IGNORECASE)


def _evaluate_page(pdf_path: str, page_number: int, statement: str, text: str) -> StatementCandidate | None:
    title = _match_title(text, statement)
    if not title:
        return None
    score, matched = _score_page(text, statement)
    labels = structured_rows = numeric_columns = 0
    garbage_ratio = 1.0
    try:
        from .pdf_router import extract_page_tables
        tables = extract_page_tables(pdf_path, page_number)
        if tables:
            table = max(tables, key=lambda t: (t.quality_score, t.confidence))
            labels, structured_rows = _gate2(table.rows, statement)
            numeric_columns, garbage_ratio = _gate3(table.rows)
    except Exception:
        pass

    # Bold/large-heading check: a page whose actual formatted heading says
    # "Balance Sheet" is much stronger evidence than a page that merely
    # mentions the phrase in a footnote or MD&A paragraph. This also
    # recovers which of Consolidated/Standalone the page is, straight from
    # the heading's own wording rather than a separate CLI flag.
    detected_heading = None
    heading_confirmed = False
    consolidated = None
    try:
        heading_lines = page_headings(pdf_path, page_number)
        detected_heading = heading_lines[0].text if heading_lines else None
        heading_confirmed = heading_matches_title(heading_lines, _title_regex_for_match(title, statement))
        consolidated = detect_consolidation(heading_lines)
    except Exception:
        pass
    if heading_confirmed:
        score += 8.0

    gate2_pass = labels >= 3 and structured_rows >= 3
    gate3_pass = numeric_columns >= 2 and garbage_ratio <= 0.25
    status = "CONFIRMED" if gate2_pass and gate3_pass else "TITLE_ONLY"
    return StatementCandidate(
        statement=statement, page=page_number, score=round(score, 2), matched_terms=matched,
        text_preview=" ".join(text.split())[:300], status=status, title_match=title,
        gate2_label_hits=labels, gate2_structured_rows=structured_rows,
        gate3_numeric_columns=numeric_columns, gate3_garbage_ratio=round(garbage_ratio, 3),
        detected_heading=detected_heading, heading_confirmed=heading_confirmed,
        consolidated=consolidated,
    )


def _title_candidates(page_text: list[str], statement: str, min_score: float) -> list[tuple[int, float, tuple[str, ...], str]]:
    """Return title-gated candidates before expensive table extraction."""
    rows: list[tuple[int, float, tuple[str, ...], str]] = []
    for page_number, text in enumerate(page_text, start=1):
        if not text.strip():
            continue
        title = _match_title(text, statement)
        if not title:
            continue
        score, matched = _score_page(text, statement)
        if score >= min_score:
            rows.append((page_number, score, matched, title))
    return rows


def _local_pages(anchor_pages: Iterable[int], page_count: int, radius: int = STATEMENT_LOCALITY_RADIUS) -> set[int]:
    pages: set[int] = set()
    for anchor in anchor_pages:
        start = max(1, anchor - radius)
        end = min(page_count, anchor + radius)
        pages.update(range(start, end + 1))
    return pages


def _rank_key(candidate: StatementCandidate, local_pages: set[int]) -> tuple[float, int, float]:
    locality_bonus = 6.0 if candidate.page in local_pages else 0.0
    return (candidate.score + locality_bonus, -candidate.page, candidate.score)


def discover_statement_pages(pdf_path: str, *, top_k: int = 3,
                             min_score: float = 10.0,
                             cluster_radius: int = CLUSTER_RADIUS) -> dict[str, list[StatementCandidate]]:
    """Title-gate discovery, then use a ±2 page locality window for the other statements.

    The locality window is a ranking preference, not a hard exclusion. If a
    statement is not found near another statement, the full title-gated scan is
    used as a high-recall fallback.
    """
    candidates = {statement: [] for statement in STATEMENT_ORDER}
    with pdfplumber.open(pdf_path) as pdf:
        page_text = [(page.extract_text() or "") for page in pdf.pages]
    page_count = len(page_text)

    title_hits = {
        statement: _title_candidates(page_text, statement, min_score)
        for statement in STATEMENT_ORDER
    }
    anchor_pages: list[int] = []
    for statement in STATEMENT_ORDER:
        if title_hits[statement]:
            anchor_pages.append(title_hits[statement][0][0])
    local_pages = _local_pages(anchor_pages, page_count, STATEMENT_LOCALITY_RADIUS)

    for statement in STATEMENT_ORDER:
        ordered = sorted(
            title_hits[statement],
            key=lambda row: (0 if row[0] in local_pages else 1, -row[1], row[0]),
        )
        evaluated: list[StatementCandidate] = []
        for page_number, _score, _matched, _title in ordered:
            candidate = _evaluate_page(pdf_path, page_number, statement, page_text[page_number - 1])
            if candidate:
                evaluated.append(candidate)
        evaluated.sort(key=lambda candidate: _rank_key(candidate, local_pages), reverse=True)
        candidates[statement] = evaluated[:top_k]
    return candidates


def discover_statement_statuses(pdf_path: str) -> dict[str, list[dict]]:
    with pdfplumber.open(pdf_path) as pdf:
        page_text = [page.extract_text() or "" for page in pdf.pages]
    out: dict[str, list[dict]] = {s: [] for s in STATEMENT_ORDER}
    for statement in STATEMENT_ORDER:
        for page_number, text in enumerate(page_text, start=1):
            candidate = _evaluate_page(pdf_path, page_number, statement, text) if text.strip() else None
            out[statement].append(asdict(candidate) if candidate else {"page": page_number, "status": "NOT_A_STATEMENT_PAGE"})
    return out


def discover_pages(pdf_path: str, *, top_k: int = 3) -> dict[str, list[int]]:
    discovered = discover_statement_pages(pdf_path, top_k=top_k)
    return {statement: [candidate.page for candidate in rows] for statement, rows in discovered.items()}


def candidates_as_dict(candidates: dict[str, Iterable[StatementCandidate]]) -> dict[str, list[dict]]:
    return {key: [asdict(item) for item in value] for key, value in candidates.items()}
