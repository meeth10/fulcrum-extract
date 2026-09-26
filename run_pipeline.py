"""End-to-end convenience wrapper: PDF in, site/data/*.json out.

ingest.py takes --pages per statement because it's a low-level, precise
tool — you tell it exactly what to read. That's the right primitive, but
it means finding page numbers by hand before you can ingest anything.
statement_discovery.py already knows how to find those pages (title-gated
scoring + the same bold-heading detector ingest.py uses for
consolidated/standalone) but nothing wired it into a single command. This
does that: discover -> ingest each statement at its best-scoring page ->
export. Falls back to manual --pages-balance-sheet / --pages-income-statement
/ --pages-cash-flow overrides for anything discovery gets wrong.

Usage:
    python run_pipeline.py filing.pdf --entity "Acme Ltd" \\
        --doc-type annual_report --fiscal-year FY2025 --period FY2025 \\
        --ticker ACME --sector Industrials --price 150 --shares 42 \\
        --db data/financials.db --out site/data
"""
from __future__ import annotations

import argparse
import sys

from extraction.statement_discovery import discover_statement_pages
from ingest import ingest
from export.site_data import write_site_data
from core.schema import init_db
from valuation.comps import MarketData

STATEMENTS = ("balance_sheet", "income_statement", "cash_flow")


def discover_or_override(pdf_path: str, overrides: dict[str, list[int] | None]) -> dict[str, list[int]]:
    discovered = discover_statement_pages(pdf_path, top_k=3)
    chosen: dict[str, list[int]] = {}
    for statement in STATEMENTS:
        if overrides.get(statement):
            chosen[statement] = overrides[statement]
            print(f"[{statement}] using manually specified page(s): {chosen[statement]}", file=sys.stderr)
            continue
        candidates = discovered[statement]
        if not candidates:
            print(f"[{statement}] no candidate page found — pass --pages-{statement.replace('_', '-')} to set it manually", file=sys.stderr)
            chosen[statement] = []
            continue
        top = candidates[0]
        print(f"[{statement}] page {top.page} (status={top.status}, score={top.score:.1f}"
              f"{', heading confirmed: ' + repr(top.detected_heading) if top.heading_confirmed else ''})", file=sys.stderr)
        if top.status != "CONFIRMED":
            print(f"[{statement}]   ^ not CONFIRMED by the structural gates — worth checking this page by eye", file=sys.stderr)
        chosen[statement] = [top.page]
    return chosen


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("pdf_path")
    p.add_argument("--entity", required=True)
    p.add_argument("--doc-type", required=True, choices=["10K", "annual_report", "sebi_quarterly", "sebi_annual", "investor_deck"])
    p.add_argument("--fiscal-year", required=True)
    p.add_argument("--period", required=True)
    p.add_argument("--ticker")
    p.add_argument("--sector")
    p.add_argument("--currency", default="INR")
    p.add_argument("--price", type=float)
    p.add_argument("--shares", type=float, help="crore of shares if fundamentals are in Rs crore — see fulcrum-extract README")
    p.add_argument("--db", default="data/financials.db")
    p.add_argument("--out", default="site/data")
    p.add_argument("--pages-balance-sheet", help="comma-separated, overrides discovery")
    p.add_argument("--pages-income-statement", help="comma-separated, overrides discovery")
    p.add_argument("--pages-cash-flow", help="comma-separated, overrides discovery")
    args = p.parse_args()

    overrides = {
        "balance_sheet": [int(x) for x in args.pages_balance_sheet.split(",")] if args.pages_balance_sheet else None,
        "income_statement": [int(x) for x in args.pages_income_statement.split(",")] if args.pages_income_statement else None,
        "cash_flow": [int(x) for x in args.pages_cash_flow.split(",")] if args.pages_cash_flow else None,
    }

    print(f"=== Discovering statement pages in {args.pdf_path} ===", file=sys.stderr)
    pages = discover_or_override(args.pdf_path, overrides)

    print(f"\n=== Ingesting into {args.db} ===", file=sys.stderr)
    for statement in STATEMENTS:
        if not pages[statement]:
            continue
        ingest(args.pdf_path, args.entity, args.doc_type, args.fiscal_year,
               args.period, statement, pages[statement], args.db)

    print(f"\n=== Exporting to {args.out} ===", file=sys.stderr)
    conn = init_db(args.db)
    market = MarketData(price=args.price, shares_outstanding=args.shares) if args.price and args.shares else None
    if args.price and not args.shares or args.shares and not args.price:
        print("Warning: pass both --price and --shares, or neither — got only one, so valuation multiples will be blank.", file=sys.stderr)
    write_site_data(conn, args.out, [{
        "entity": args.entity, "ticker": args.ticker, "sector": args.sector,
        "currency": args.currency, "market": market,
    }])
    print(f"\nDone. Commit {args.out}/*.json to the fulcrum repo's data/ folder and push (or use GitHub sync).", file=sys.stderr)


if __name__ == "__main__":
    main()
