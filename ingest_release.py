"""Ingest a US-style quarterly earnings release (Q2 layout: three-month,
six-month and trailing-twelve-month columns) into the same SQLite store.

Reads each statement's text by column position, labels every column with its
real period, and refuses to write anything if the statements don't foot.

Periods written (calendar-year filer, FY = calendar year):
  Q2FY2026 / Q2FY2025        three months ended Jun 30
  H1FY2026 / H1FY2025        six months ended Jun 30
  TTMQ2FY2026 / TTMQ2FY2025  trailing twelve months
  balance sheet: Jun 30 -> Q2FY2026, Dec 31 prior year -> FY2025
  (Jun 30 balance sheet items are also stored under TTMQ2FY2026 so the
   latest balance sheet pairs with TTM flows for valuation.)
"""
from __future__ import annotations
import argparse, re, sys
import pdfplumber
from core.schema import init_db
from core.db import add_document, add_line_item, LineItem, list_periods
from export.site_data import write_site_data
from valuation.comps import MarketData
from extraction.statement_discovery import discover_statement_pages
from run_pipeline import fetch_market_data, format_summary_table, email_summary
from ingest import normalize_metric

NUM = r"\(?-?[\d,]+(?:\.\d+)?\)?|—"
def _tok(s):
    if s == "—": return 0.0
    v = float(s.strip("()").replace(",", ""))
    return -v if s.startswith("(") else v

def parse_statement(pdf_path, page, ncols, start_re):
    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[page - 1].extract_text()
    rows, pending, started = {}, "", False
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line.replace("$", " ")).strip()
        if not started:
            started = bool(re.search(start_re, line)); continue
        m = re.search(rf"((?:\s(?:{NUM})){{{ncols}}})$", " " + line)
        if m:
            vals = [_tok(t) for t in re.findall(NUM, m.group(1))]
            label = (" " + line)[: m.start()].strip()
            if len(vals) == ncols and (label or pending):
                rows[(pending + " " + label).strip()] = vals; pending = ""; continue
        pending = "" if line.endswith(":") else (pending + " " + line).strip()
    return rows

# Labels the shared alias list (core/rules.yaml) doesn't resolve for US filers.
METRIC_OVERRIDES = {
    "Net cash provided by (used in) operating activities": "operating_cash_flow",
    "Total stockholders’ equity": "shareholders_equity",
    "Long-term debt": "total_debt",
    "Operating income": "ebit",
    "Total net sales": "revenue",
}

def clean(label, stmt):
    if stmt == "income_statement" and label in ("Basic", "Diluted"):
        return f"Weighted-average shares, {label.lower()}"
    if label.startswith("Preferred stock"): return "Preferred stock"
    if label.startswith("Common stock"): return "Common stock"
    return label

def verify(IS, CF, BS):
    z = lambda *c: [sum(x) for x in zip(*c)]
    ok = lambda a, b: all(abs(x - y) < 0.51 for x, y in zip(a, b))
    return {
     "IS: sales - opex = operating income": ok(z(IS["Total net sales"], [-x for x in IS["Total operating expenses"]]), IS["Operating income"]),
     "IS: pretax + tax + equity method = net income": ok(z(IS["Income before income taxes"], IS["Provision for income taxes"], IS["Equity-method investment activity, net of tax"]), IS["Net income"]),
     "CF: CFO + CFI + CFF + FX = net change": ok(z(CF["Net cash provided by (used in) operating activities"], CF["Net cash provided by (used in) investing activities"], CF["Net cash provided by (used in) financing activities"], CF["Foreign currency effect on cash, cash equivalents, and restricted cash"]), CF["Net increase (decrease) in cash, cash equivalents, and restricted cash"]),
     "CF: net income ties to income statement": ok(CF["Net income"][:4], IS["Net income"]),
     "BS: total assets = liabilities + equity": ok(BS["Total assets"], BS["Total liabilities and stockholders’ equity"]),
    }

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pdf_path"); p.add_argument("--entity", required=True)
    p.add_argument("--year", type=int, required=True, help="current year, e.g. 2026")
    p.add_argument("--ticker"); p.add_argument("--sector"); p.add_argument("--currency", default="USD")
    p.add_argument("--price", type=float); p.add_argument("--shares", type=float, help="millions")
    p.add_argument("--auto-market", action="store_true")
    p.add_argument("--db", default="data/financials.db"); p.add_argument("--out", default="site/data")
    p.add_argument("--email")
    a = p.parse_args()
    y, py = a.year, a.year - 1

    found = discover_statement_pages(a.pdf_path, top_k=1)
    pg = {k: v[0].page for k, v in found.items() if v}
    missing = {"balance_sheet", "income_statement", "cash_flow"} - set(pg)
    if missing: sys.exit(f"couldn't locate: {sorted(missing)}")
    print(f"pages: {pg}", file=sys.stderr)

    IS = {clean(k, "income_statement"): v for k, v in parse_statement(a.pdf_path, pg["income_statement"], 4, r"^\d{4} \d{4} \d{4} \d{4}").items()}
    CF = {clean(k, "cash_flow"): v for k, v in parse_statement(a.pdf_path, pg["cash_flow"], 6, r"^\d{4} \d{4} \d{4} \d{4}").items()}
    BS = {clean(k, "balance_sheet"): v for k, v in parse_statement(a.pdf_path, pg["balance_sheet"], 2, r"^ASSETS").items()}
    checks = verify(IS, CF, BS)
    for n, ok in checks.items(): print(f"  {'PASS' if ok else 'FAIL'}  {n}", file=sys.stderr)
    if not all(checks.values()):
        sys.exit("statements don't foot — refusing to write. Check the column layout matches a Q2 release (3M/6M/TTM).")

    cols = {"income_statement": [f"Q2FY{py}", f"Q2FY{y}", f"H1FY{py}", f"H1FY{y}"],
            "cash_flow": [f"Q2FY{py}", f"Q2FY{y}", f"H1FY{py}", f"H1FY{y}", f"TTMQ2FY{py}", f"TTMQ2FY{y}"],
            "balance_sheet": [f"FY{py}", f"Q2FY{y}"]}
    conn = init_db(a.db)
    doc = add_document(conn, a.entity, "earnings_release", f"FY{y}", a.pdf_path)
    n = 0
    def put(stmt, period, label, val, page, method="pdf_text_column_parse", conf=0.98, metric=None):
        nonlocal n
        m = metric or METRIC_OVERRIDES.get(label) or normalize_metric(label) or re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60]
        add_line_item(conn, doc, LineItem(entity=a.entity, period=period, statement=stmt, metric=m, metric_raw=label,
            value=val, unit="usd_mm", consolidated=True, source_page=page, source_table=None,
            extraction_method=method, extraction_confidence=conf, source_heading=None)); n += 1
    for stmt, rows in (("income_statement", IS), ("cash_flow", CF), ("balance_sheet", BS)):
        for label, vals in rows.items():
            for period, v in zip(cols[stmt], vals):
                metric = None
                if stmt == "cash_flow" and label == "Purchases of property and equipment":
                    metric = "purchases_of_property_and_equipment"
                put(stmt, period, label, v, pg[stmt], metric=metric)
    # capex net of proceeds = the company's own free-cash-flow definition
    for i, period in enumerate(cols["cash_flow"]):
        net = CF["Purchases of property and equipment"][i] + CF["Proceeds from property and equipment sales and incentives"][i]
        put("cash_flow", period, "Purchases of property and equipment, net of proceeds (derived)", net, pg["cash_flow"],
            "derived_from_two_reported_lines", 0.98, "capital_expenditure")
    # latest balance sheet paired with TTM flows for valuation
    ttm = f"TTMQ2FY{y}"
    for label in ("Cash and cash equivalents", "Long-term debt", "Total stockholders’ equity", "Total assets"):
        put("balance_sheet", ttm, label, BS[label][1], pg["balance_sheet"], "balance_sheet_at_quarter_end_paired_with_ttm", 0.95)
    # TTM revenue / operating income live in the supplemental table
    ttm_ebit = None
    with pdfplumber.open(a.pdf_path) as pdf:
        for pn in range(pg["balance_sheet"] + 1, len(pdf.pages) + 1):
            t = pdf.pages[pn - 1].extract_text() or ""
            got = {}
            for pref, metric in (("WW net sales", "revenue"), ("Operating income", "ebit")):
                for l in t.split("\n"):
                    if l.startswith(pref + " -- TTM ") and "Y/Y" not in l and "%" in l:
                        got[metric] = [float(x.replace(",", "")) for x in re.findall(r"[\d,]{3,}", l)]; break
            if len(got) == 2:
                ttm_ebit = got["ebit"]
                for metric, series in got.items():
                    for period, idx in ((f"TTMQ2FY{py}", 1), (ttm, 5)):
                        put("income_statement", period, f"{'WW net sales' if metric == 'revenue' else 'Operating income'} -- TTM", series[idx], pn, metric=metric)
                break

    # EBITDA is not reported by Amazon, so derive it: operating income + D&A.
    # Amazon's D&A line also includes amortization of capitalized content
    # costs and operating lease assets, so this is a broader EBITDA than
    # most analysts quote -- stored as derived, at lower confidence.
    da_label = "Depreciation and amortization of property and equipment and capitalized content costs, operating lease assets, and other"
    if da_label in CF and ttm_ebit:
        da = dict(zip(cols["cash_flow"], CF[da_label]))
        ebit_by_period = dict(zip(cols["income_statement"], IS["Operating income"]))
        ebit_by_period[f"TTMQ2FY{py}"] = ttm_ebit[1]
        ebit_by_period[ttm] = ttm_ebit[5]
        for period, e in ebit_by_period.items():
            if period in da:
                put("income_statement", period, "EBITDA (derived: operating income + depreciation and amortization)",
                    e + da[period], pg["cash_flow"], "derived_ebit_plus_da", 0.9, "ebitda")
    else:
        print("could not derive EBITDA (missing D&A line or TTM operating income)", file=sys.stderr)
    print(f"stored {n} line items in {a.db}", file=sys.stderr)

    price, shares = a.price, a.shares
    if (price is None or shares is None) and a.auto_market and a.ticker:
        ap, ash = fetch_market_data(a.ticker, a.currency)
        price, shares = price or ap, shares or ash
    market = MarketData(price=price, shares_outstanding=shares) if price and shares else MarketData()
    write_site_data(conn, a.out, [{"entity": a.entity, "ticker": a.ticker, "sector": a.sector,
                                    "currency": a.currency, "market": market if price and shares else None}])
    table = format_summary_table(conn, a.entity, ttm, f"TTMQ2FY{py}", market)
    print("\n" + table)
    if a.email: email_summary(a.email, f"Fulcrum: {a.entity} — TTM Q2 {y}", table)

if __name__ == "__main__":
    main()
