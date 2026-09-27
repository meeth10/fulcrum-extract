"""Detect bold/large-font heading text on a PDF page, using pdfplumber's
per-character font metadata (size, fontname) — not a vision model, not an
LLM, just reading the font info the PDF already carries.

Two things this is for:
  1. Telling "Consolidated" and "Standalone" statements apart. Indian
     (and most) annual reports print this distinction as a bold heading
     directly above each statement, not as a data column — so it has to
     be read from formatting, not from the table itself.
  2. Giving statement_discovery.py a stronger signal: a page whose BOLD
     heading says "Balance Sheet" is far more likely to actually be the
     balance sheet than a page that merely mentions the phrase in a
     footnote or MD&A paragraph (see the false-positive test case below).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

import pdfplumber

_BOLD_HINTS = ("bold", "black", "heavy", "-b", ",b", "semibold")
_CONSOLIDATED_RE = re.compile(r"\bconsolidated\b", re.IGNORECASE)
_STANDALONE_RE = re.compile(r"\b(standalone|separate)\b", re.IGNORECASE)


@dataclass
class HeadingLine:
    text: str
    size: float
    is_bold: bool
    top: float  # distance from top of page, for ordering


def _is_bold_font(fontname: str) -> bool:
    lower = fontname.lower()
    return any(hint in lower for hint in _BOLD_HINTS)


def _group_chars_into_lines(chars: list[dict], y_tolerance: float = 3.0) -> list[list[dict]]:
    """Group characters into visual lines by vertical position — pdfplumber
    gives characters individually, not pre-grouped into text lines."""
    if not chars:
        return []
    ordered = sorted(chars, key=lambda c: (round(c["top"] / y_tolerance), c["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = [ordered[0]]
    current_top = ordered[0]["top"]
    for ch in ordered[1:]:
        if abs(ch["top"] - current_top) <= y_tolerance:
            current.append(ch)
        else:
            lines.append(current)
            current = [ch]
            current_top = ch["top"]
    lines.append(current)
    return lines


def page_headings(pdf_path: str, page_number: int, size_ratio: float = 1.15,
                   max_heading_words: int = 14) -> list[HeadingLine]:
    """Return candidate heading lines on a page: text that is either
    meaningfully larger than the page's median body-text size, or in a
    bold-named font — and short enough to plausibly be a heading rather
    than a wrapped paragraph. Ordered top-to-bottom.
    """
    with pdfplumber.open(pdf_path) as pdf:
        if page_number - 1 >= len(pdf.pages) or page_number < 1:
            return []
        page = pdf.pages[page_number - 1]
        chars = [c for c in page.chars if c.get("text", "") != ""]
        if not chars:
            return []

        sizes = [c["size"] for c in chars]
        body_size = statistics.median(sizes)

        lines = _group_chars_into_lines(chars)
        headings: list[HeadingLine] = []
        for line_chars in lines:
            text = "".join(c["text"] for c in sorted(line_chars, key=lambda c: c["x0"])).strip()
            if not text or len(text.split()) > max_heading_words:
                continue
            line_size = statistics.median(c["size"] for c in line_chars)
            line_bold = sum(1 for c in line_chars if _is_bold_font(c["fontname"])) >= len(line_chars) * 0.6
            is_large = line_size >= body_size * size_ratio
            if line_bold or is_large:
                headings.append(HeadingLine(
                    text=text, size=line_size, is_bold=line_bold,
                    top=min(c["top"] for c in line_chars),
                ))
        headings.sort(key=lambda h: h.top)
        return headings


def detect_consolidation(headings: list[HeadingLine]) -> bool | None:
    """Scan heading candidates (top-to-bottom, so the first match wins if
    a page somehow has both words in separate bold lines) for
    "Consolidated" vs "Standalone"/"Separate". Returns None — not a
    guess — when neither appears, or both appear in the SAME line
    (genuinely ambiguous, e.g. a page that says "Consolidated and
    Standalone financial statements" as a section divider)."""
    for h in headings:
        has_c = bool(_CONSOLIDATED_RE.search(h.text))
        has_s = bool(_STANDALONE_RE.search(h.text))
        if has_c and not has_s:
            return True
        if has_s and not has_c:
            return False
        # both or neither on this line — keep scanning subsequent lines
    return None


def page_heading_summary(pdf_path: str, page_number: int) -> tuple[str | None, bool | None]:
    """Convenience wrapper: (top heading text or None, consolidated flag or None)."""
    headings = page_headings(pdf_path, page_number)
    top_text = headings[0].text if headings else None
    return top_text, detect_consolidation(headings)


def _normalise_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def heading_matches_title(headings: list[HeadingLine], title_pattern: re.Pattern) -> bool:
    """True if a statement-title pattern (as statement_discovery.py already
    matches against page text) is found inside one of this page's BOLD/LARGE
    heading lines specifically — not just anywhere in the page's body text.
    This is what lets scoring tell a real statement page apart from a page
    that merely name-drops "balance sheet" in a narrative paragraph.
    title_pattern is expected to match against normalised (lowercased,
    whitespace-collapsed) text, matching statement_discovery.py's convention."""
    return any(title_pattern.search(_normalise_for_match(h.text)) for h in headings)
