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
import os
import smtplib
import sys
from email.mime.text import MIMEText

from extraction.statement_discovery import discover_statement_pages
from ingest import ingest
from export.site_data import write_site_data
from core.schema import init_db
from core.db import list_periods
from valuation.comps import MarketData, build_company_comp

STATEMENTS = ("balance_sheet", "income_statement", "cash_flow")


def fetch_market_data(ticker: str, currency: str) -> tuple[float | None, float | None]:
    """Look up live price + shares outstanding via yfinance.

    Indian tickers need an exchange suffix (.NS for NSE, .BO for BSE) that
    people don't usually type when they just say "COALINDIA" — try bare,
    then .NS, then .BO. Shares come back from Yahoo as a raw share count;
    the rest of this pipeline works in crore (1e7) for INR reporting to
    match how Indian filings are usually read, so convert accordingly.
    """
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance isn't installed — run: pip install yfinance", file=sys.stderr)
        return None, None

    candidates = [ticker] if "." in ticker else (
        [ticker, f"{ticker}.NS", f"{ticker}.BO"] if currency.upper() == "INR" else [ticker]
    )
    for candidate in candidates:
        try:
            info = yf.Ticker(candidate).fast_info
            price = info.get("last_price") or info.get("lastPrice")
            shares = info.get("shares") or info.get("shares_outstanding")
            if price and shares:
                shares_out = shares / 1e7 if currency.upper() == "INR" else shares / 1e6
                print(f"[market data] {candidate}: price={price:.2f}, shares={shares_out:.2f} "
                      f"({'cr' if currency.upper() == 'INR' else 'mm'})", file=sys.stderr)
                return float(price), shares_out
        except Exception as e:
            print(f"[market data] {candidate} failed: {e}", file=sys.stderr)
    print(f"[market data] couldn't resolve a live quote for '{ticker}' — pass --price/--shares manually", file=sys.stderr)
    return None, None


def format_summary_table(conn, entity: str, period: str, prior_period: str | None, market: MarketData) -> str:
    """Plain-text final table, built from the same build_company_comp()
    the site's export step uses — so the emailed numbers are exactly
    what ends up in site/data/*.json, not a second parallel calculation."""
    comp = build_company_comp(conn, entity, period, prior_period=prior_period, market=market)
    lines = [f"{entity} — {period}", "=" * 50, "Fundamentals:"]
    for k, v in comp.fundamentals.items():
        lines.append(f"  {k:<24} {v:>14,.1f}" if v is not None else f"  {k:<24} {'n/a':>14}")
    lines.append("\nMetrics:")
    for k, v in comp.metrics.items():
        lines.append(f"  {k:<24} {v:>14.3f}" if v is not None else f"  {k:<24} {'n/a':>14}")
    lines.append("\nValuation:")
    for k, v in comp.valuation.items():
        lines.append(f"  {k:<24} {v:>14,.2f}" if v is not None else f"  {k:<24} {'n/a':>14}")
    if comp.missing_inputs:
        lines.append(f"\nMissing inputs (not found in the filing): {', '.join(comp.missing_inputs)}")
    return "\n".join(lines)


def email_summary(to_addr: str, subject: str, body: str) -> None:
    """Sends over Gmail SMTP using an App Password, never your real Google
    password — generate one at myaccount.google.com/apppasswords (needs
    2-Step Verification on first) and set it as an env var, e.g.:
        export GMAIL_ADDRESS=you@gmail.com
        export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
    Nothing here reads or stores your password; it only exists in your
    own shell's environment for the duration of the run.
    """
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not app_password:
        print("Skipping email — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD to enable it "
              "(see the email_summary() docstring in run_pipeline.py).", file=sys.stderr)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, app_password)
        server.sendmail(sender, [to_addr], msg.as_string())
    print(f"Emailed summary to {to_addr}", file=sys.stderr)



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
    p.add_argument("--price", type=float, help="overrides --auto-market for price")
    p.add_argument("--shares", type=float, help="crore (INR) or millions (other currencies); overrides --auto-market for shares")
    p.add_argument("--auto-market", action="store_true", help="fetch live price/shares via yfinance using --ticker (skipped if --price/--shares given)")
    p.add_argument("--db", default="data/financials.db")
    p.add_argument("--out", default="site/data")
    p.add_argument("--pages-balance-sheet", help="comma-separated, overrides discovery")
    p.add_argument("--pages-income-statement", help="comma-separated, overrides discovery")
    p.add_argument("--pages-cash-flow", help="comma-separated, overrides discovery")
    p.add_argument("--email", help="email address to send the final summary table to (needs GMAIL_ADDRESS + GMAIL_APP_PASSWORD env vars)")
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

    price, shares = args.price, args.shares
    if (price is None or shares is None) and args.auto_market:
        if not args.ticker:
            print("--auto-market needs --ticker to look up a quote", file=sys.stderr)
        else:
            auto_price, auto_shares = fetch_market_data(args.ticker, args.currency)
            price = price if price is not None else auto_price
            shares = shares if shares is not None else auto_shares

    print(f"\n=== Exporting to {args.out} ===", file=sys.stderr)
    conn = init_db(args.db)
    market = MarketData(price=price, shares_outstanding=shares) if price and shares else None
    if bool(price) != bool(shares):
        print("Warning: only one of price/shares resolved — valuation multiples will be blank.", file=sys.stderr)
    write_site_data(conn, args.out, [{
        "entity": args.entity, "ticker": args.ticker, "sector": args.sector,
        "currency": args.currency, "market": market,
    }])
    print(f"\nDone. Commit {args.out}/*.json to the fulcrum repo's data/ folder and push (or use GitHub sync).", file=sys.stderr)

    periods = list_periods(conn, args.entity)
    prior_periods = [pd for pd in periods if pd < args.period]
    table = format_summary_table(conn, args.entity, args.period,
                                  prior_periods[-1] if prior_periods else None,
                                  market or MarketData())
    print(f"\n{table}")
    if args.email:
        email_summary(args.email, f"Fulcrum: {args.entity} — {args.period}", table)


if __name__ == "__main__":
    main()
