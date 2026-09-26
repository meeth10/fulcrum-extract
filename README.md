# fulcrum-extract

The deterministic half of `financial-pdf-agent-v2`, repackaged with the
agent/MCP layer removed, plus one new file that was actually missing:
`export/site_data.py`, which turns the store into the JSON the Fulcrum
website reads.

## What got cut, and why it's safe to cut

Everything under `agent/` (the Ollama tool-calling loop: `run.py`,
`tools.py`, `runtime.py`, `system_prompt.py`, `context_agent.py`) and all
three of `mcp_servers/{financial,evidence,retrieval}` are gone. That layer
existed to let an LLM decide what to retrieve and reconcile conflicting
sources — useful for an open-ended "analyze any company" tool, not needed
for a fixed set of filings you're feeding in yourself.

`extraction/llm_cleanup.py` (the Ollama call for label cleanup) is also
gone, but check the code before assuming that's a loss: `cleanup_table()`
called `_deterministic_rows()` first, then tried an LLM label-polish pass
wrapped in `try/except` that silently fell back to the deterministic output
on *any* failure. The LLM was never load-bearing for correctness — it only
prettified label spelling, and `canonicalize_metric()`
(`core/derivation.py`, driven by `core/rules.yaml`'s alias lists) already
normalizes labels downstream. `extraction/row_parser.py` is that same
deterministic logic, lifted out on its own with zero LLM dependency.

`src/webapp.py` and `src/analyst_webapp.py` (the Flask UIs that served the
agent) are gone too — replaced by `export/site_data.py`, which just writes
static JSON files. No server, no per-request computation.

## What's unchanged, copied as-is

- `extraction/pdf_router.py` — Camelot (lazy-imported, only needed if you
  actually call the Camelot path) + pdfplumber table extraction, exactly as
  written and already smoke-tested in the original repo.
- `extraction/statement_discovery.py` — title-gated statement page finding.
- `core/schema.py`, `core/db.py` — the SQLite line-item store with full
  provenance (`source_page`, `source_table`, `extraction_confidence` on
  every row).
- `core/derivation.py`, `core/periods.py`, `core/units.py`,
  `core/validation.py`, `core/rules.yaml` — moved out of `agent/` into
  `core/` because they're rule-based normalization, not the agent loop
  (the module docstrings say as much — `derivation.py`: "deterministic...
  governed by rules.yaml").
- `valuation/dcf.py` — untouched. Was already a pure function with no LLM
  involvement.
- `export/excel_export.py` — untouched.

## What's new

- `extraction/row_parser.py` — `llm_cleanup.py` minus the LLM call (see
  above).
- `valuation/comps.py` — used to be `valuation/comps_adapter.py`, an HTTP
  client for a *second, separately-run* FastAPI service
  (`meeth10/Comp_analysis`). That service's own metric set (documented in
  the old `docs/COMPS_INTEGRATION.md`: EBITDA margin, ROE, ROIC, net
  debt/EBITDA, P/E, EV/EBITDA, EV/EBIT, EV/Sales, P/B, FCF yield) is
  reimplemented here directly against this store. One process instead of
  two services that both have to be running and kept in sync — this is the
  "make that into one" part.
- `export/site_data.py` + `build_site_data.py` (below) — reads the store,
  computes comps, writes `site/data/<company-id>.json` + `index.json`. This
  is the missing link between the extraction pipeline and the website; it
  didn't exist before.
- `ingest.py` — same CLI shape as the original, rewired onto
  `row_parser.parse_rows()` instead of `llm_cleanup.cleanup_table()`. Also
  fixes a real bug in the original: it had a `--skip-llm-cleanup` flag
  whose `if/else` branches both called the same LLM function — the flag did
  nothing. There's no LLM in this version, so nothing to skip.

## Usage

```bash
pip install pdfplumber camelot-py opencv-python-headless ghostscript openpyxl pyyaml
# Camelot's lattice mode also needs the system Ghostscript binary:
#   macOS: brew install ghostscript

# 1. Ingest each statement page of a filing (repeat per statement/period)
python ingest.py path/to/filing.pdf \
  --entity "Acme Ltd" --doc-type annual_report --fiscal-year FY2025 \
  --period FY2025 --statement balance_sheet --pages 42,43 \
  --db data/financials.db
# consolidated vs. standalone is auto-detected from the page's bold
# heading and logged to stderr — add --consolidated true|false only to
# override it (e.g. the heading landed on a different page than the table)

# 2. Export everything in the store to the JSON the website reads
python export/site_data.py --db data/financials.db --out site/data \
  --entity "Acme Ltd" --ticker ACME --sector Industrials \
  --price 150 --shares 1000000000
# (price/shares are market data, not in any filing — pass them yourself,
# or omit them and the site's valuation multiples for that company will
# just be absent, fundamentals still show)

# 3. Commit site/data/*.json to the Fulcrum repo, push, done — the site
#    fetches those files directly, no backend involved.
```

## Unit convention: keep shares_outstanding on the same scale as the money

`market_cap = price x shares_outstanding` only lands in the right scale if
`shares_outstanding` is expressed in the same units as your monetary
fundamentals. If your filings store revenue/EBITDA/etc. in Rs crore (the
usual convention for Indian filings, and what the sample data in the
website uses), pass `shares_outstanding` in **crore of shares**, not the
raw share count — otherwise EV/EBITDA and P/E come out off by
10,000,000x. Example: a company with ~42 crore shares at Rs 842 gets
`--shares 42`, not `--shares 420000000`.

## New: bold/large-heading detection (consolidated vs. standalone)

Every consolidated or standalone statement in a real filing is introduced
by a bold, larger-font heading — "CONSOLIDATED BALANCE SHEET",
"Statement of Profit and Loss (Standalone)" — not a data column, so it has
to be read from the page's formatting, not the table itself.
`extraction/headings.py` does this by reading pdfplumber's per-character
font metadata (size, fontname) directly — no vision model, no LLM, just
the font info the PDF already carries. It groups characters into lines,
flags lines that are meaningfully larger than the page's median body-text
size or in a bold-named font, and scans those specifically for
"Consolidated" / "Standalone" / "Separate".

This plugs into three places:

- **`ingest.py`** now detects each page's heading before extracting it,
  auto-populates the `consolidated` column (which existed in the schema
  before this but was always left `NULL`), and stores the heading text
  itself in a new `source_heading` column. `--consolidated {true,false}`
  overrides the detection if a page's heading is missing or ambiguous.
- **`statement_discovery.py`** uses the same detector to tell a page whose
  *actual bold heading* says "Balance Sheet" apart from a page that just
  mentions the phrase in an MD&A paragraph — a heading match adds +8 to
  the page's score. (Tested against a synthetic filing with a genuine
  decoy page — a Directors' Report that name-drops "consolidated" and
  "balance sheet" in plain body text — which correctly scored far lower
  and was correctly not flagged as heading-confirmed.)
- **`valuation/comps.py`** and **`export/site_data.py`** now treat
  consolidated/standalone as a categorical choice, not a quality signal.
  Previously, if both a consolidated and a standalone figure existed for
  the same metric/period, `_get_value()` picked whichever had the higher
  `extraction_confidence` — which could silently mix conventions for
  different metrics in the same comparison. It now prefers the
  consolidated figure by default (`prefer_consolidated=True`, overridable)
  and only falls back to standalone if consolidated wasn't extracted.
  `site_data.py`'s exported JSON keeps **both** variants per metric
  (`{"consolidated": {...}, "standalone": {...}, "unspecified": {...}}`)
  rather than collapsing to one, so the site can show either.

One real bug this surfaced and fixed along the way:
`statement_discovery.py`'s `_evaluate_page()` had `from
src.extraction.pdf_router import extract_page_tables` — a leftover from
the original repo's package layout. Since it's wrapped in a bare
`except Exception: pass`, this failed silently on every call, meaning
Gate 2/3 structural validation (the check that promotes a page from
`TITLE_ONLY` to `CONFIRMED`) never actually ran in this repackaged
version until now. Fixed to `from .pdf_router import extract_page_tables`.

## One thing worth checking against your actual filings

The synthetic smoke test I ran to validate this pipeline showed
`capital_expenditure` coming out negative when the filing prints it in
parentheses (a common cash-flow-statement convention: capex shown as a
cash *outflow*). `valuation/comps.py`'s free-cash-flow calc does
`operating_cash_flow - capital_expenditure`, which is correct if capex is
stored as a positive spend figure, but double-counts the sign if it's
already negative. Check one real filing's sign convention before trusting
the FCF/FCF-yield numbers — I didn't have a real filing to test this
against.
