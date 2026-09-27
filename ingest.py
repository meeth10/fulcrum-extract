"""Ingest one PDF into the structured financial store. Fully deterministic —
no LLM, no network call, no Ollama dependency.
"""

from __future__ import annotations

import argparse
import sys

from extraction.pdf_router import extract_page_tables, page_needs_ocr
from extraction.row_parser import parse_rows
from extraction.headings import page_heading_summary
from core.schema import init_db
from core.db import add_document, add_line_item, LineItem
from core.derivation import canonicalize_metric
from core.periods import canonicalize_period


def normalize_metric(raw_label: str) -> str | None:
    """Delegates to the SAME canonical dictionary the query-time lookups
    resolve against (core/rules.yaml canonical_terms, via
    derivation.canonicalize_metric) — one source of truth for the mapping."""
    cleaned = raw_label.strip()
    if not cleaned:
        return None
    return canonicalize_metric(cleaned)


def ingest(pdf_path: str, entity: str, doc_type: str, fiscal_year: str,
           period: str, statement: str, pages: list[int], db_path: str,
           consolidated_override: bool | None = None) -> None:
    conn = init_db(db_path)
    document_id = add_document(conn, entity, doc_type, fiscal_year, pdf_path)
    stored = 0
    skipped = 0
    requested_period = canonicalize_period(period)

    for page in pages:
        if page_needs_ocr(pdf_path, page):
            print(f"[page {page}] looks scanned — skipping, wire up OCR fallback first", file=sys.stderr)
            continue

        # Every consolidated/standalone financial statement is introduced by
        # a bold/large heading, not a data column — so it has to be read
        # from the page's formatting, not the table itself. --consolidated
        # overrides this if you already know and the page's own heading is
        # ambiguous or missing (e.g. a table that got split across pages).
        detected_heading, detected_consolidated = page_heading_summary(pdf_path, page)
        if consolidated_override is not None:
            if detected_consolidated is not None and detected_consolidated != consolidated_override:
                print(f"[page {page}] heading says {'consolidated' if detected_consolidated else 'standalone'} "
                      f"({detected_heading!r}) but --consolidated overrides to "
                      f"{'consolidated' if consolidated_override else 'standalone'}", file=sys.stderr)
            page_consolidated = consolidated_override
        else:
            page_consolidated = detected_consolidated
            if detected_heading:
                print(f"[page {page}] heading: {detected_heading!r} -> "
                      f"consolidated={page_consolidated}", file=sys.stderr)
            else:
                print(f"[page {page}] no bold/large heading detected — "
                      f"consolidated flag left unset (pass --consolidated to set it manually)", file=sys.stderr)

        tables = extract_page_tables(pdf_path, page)
        if not tables:
            print(f"[page {page}] no table found by any extractor", file=sys.stderr)
            continue

        for table in tables:
            parsed = parse_rows(table.rows)

            for row in parsed:
                if row.get("ambiguous_multi_period"):
                    print(f"[page {page}] skipping '{row.get('metric_raw')}': more than one "
                          f"numeric value and no year header to resolve them against", file=sys.stderr)
                    skipped += 1
                    continue
                metric = normalize_metric(row.get("metric_raw", ""))
                if metric is None or row.get("value") is None:
                    skipped += 1
                    continue

                # A detected year header gives a more precise, per-column
                # period than the single --period flag can — use it when
                # present, so one page with 2-3 statement years ingests all
                # of them correctly in one pass instead of requiring one
                # ingest() call per column.
                if row.get("period_raw"):
                    row_period = canonicalize_period(row["period_raw"])
                    if row_period != requested_period:
                        print(f"[page {page}] '{row.get('metric_raw')}': column period "
                              f"{row_period!r} (detected) differs from --period "
                              f"{requested_period!r} (requested) — storing under {row_period!r}",
                              file=sys.stderr)
                else:
                    row_period = requested_period

                add_line_item(conn, document_id, LineItem(
                    entity=entity, period=row_period, statement=statement,
                    metric=metric, metric_raw=row["metric_raw"], value=row["value"],
                    unit=row.get("unit") or "unspecified", consolidated=page_consolidated,
                    source_page=page, source_table=table.table_caption,
                    extraction_method=table.method,
                    extraction_confidence=table.confidence,
                    source_heading=detected_heading,
                ))
                stored += 1

    print(f"Ingested {pdf_path} for {entity} / {requested_period} into {db_path}")
    print(f"Stored line items: {stored}; skipped/unparsed: {skipped}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("pdf_path")
    p.add_argument("--entity", required=True)
    p.add_argument("--doc-type", required=True, choices=["10K", "annual_report", "sebi_quarterly", "sebi_annual", "investor_deck"])
    p.add_argument("--fiscal-year", required=True)
    p.add_argument("--period", required=True)
    p.add_argument("--statement", required=True, choices=["balance_sheet", "income_statement", "cash_flow"])
    p.add_argument("--pages", required=True)
    p.add_argument("--db", default="data/financials.db")
    p.add_argument("--consolidated", choices=["true", "false"], default=None,
                    help="Override the heading-detected consolidated/standalone flag. "
                         "Leave unset to trust the page's own bold heading.")
    args = p.parse_args()

    override = None if args.consolidated is None else (args.consolidated == "true")
    ingest(args.pdf_path, args.entity, args.doc_type, args.fiscal_year,
           args.period, args.statement, [int(x) for x in args.pages.split(",")],
           args.db, consolidated_override=override)
