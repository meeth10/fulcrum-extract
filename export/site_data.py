"""Build the static JSON files the Fulcrum website reads.

This replaces src/webapp.py and src/analyst_webapp.py (the Flask UIs that
served the agent). There is no server here: this script reads the local
SQLite store and writes plain JSON files into a folder you commit to the
Fulcrum repo and push to GitHub Pages. The site fetches those files
directly — no backend, no agent, no per-request computation.

Usage:
    python build_site_data.py --db data/financials.db --out site/data \
        --entity "Acme Ltd" --ticker ACME --sector Industrials \
        --price 150 --shares 1000000000
    (repeat --entity ... for each company, or pass --all to export every
    entity already in the store with no market data attached)
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    # Run directly (`python export/site_data.py ...`) rather than as a
    # module — put the repo root on sys.path so `core`/`valuation` resolve
    # regardless of the caller's working directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db
from core.derivation import canonicalize_metric
from valuation.comps import FUNDAMENTAL_METRICS, MarketData, build_company_comp

STATEMENTS = ("balance_sheet", "income_statement", "cash_flow")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "company"


def list_entities(conn: sqlite3.Connection) -> list[str]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT DISTINCT entity FROM line_items ORDER BY entity").fetchall()
    return [r["entity"] for r in rows]


def _statement_block(conn: sqlite3.Connection, entity: str, statement: str, period: str) -> dict[str, Any]:
    """One entry per metric, holding up to three variants — 'consolidated',
    'standalone', and 'unspecified' (heading wasn't detected/overridden) —
    rather than one cell per metric. Filings routinely carry BOTH a
    consolidated and a standalone version of the same line item under
    their own bold headings, and collapsing them to a single cell would
    silently pick whichever row SQLite happened to return last."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT li.*, d.doc_type, d.fiscal_year, d.filepath
           FROM line_items li JOIN documents d ON d.id = li.document_id
           WHERE li.entity = ? AND li.statement = ? AND li.period = ?
           ORDER BY li.id""",
        (entity, statement, period),
    ).fetchall()
    block: dict[str, Any] = {}
    for row in rows:
        metric = canonicalize_metric(row["metric"])
        cell = {
            "value": row["value"],
            "unit": row["unit"],
            "metric_raw": row["metric_raw"],
            "source_page": row["source_page"],
            "source_table": row["source_table"],
            "source_heading": row["source_heading"],
            "extraction_method": row["extraction_method"],
            "extraction_confidence": row["extraction_confidence"],
            "doc_type": row["doc_type"],
            "fiscal_year": row["fiscal_year"],
        }
        basis = "consolidated" if row["consolidated"] == 1 else ("standalone" if row["consolidated"] == 0 else "unspecified")
        slot = block.setdefault(metric, {"consolidated": None, "standalone": None, "unspecified": None})
        existing = slot[basis]
        if existing is None or (cell["extraction_confidence"] or 0.0) > (existing["extraction_confidence"] or 0.0):
            slot[basis] = cell
    return block


def build_company_json(conn: sqlite3.Connection, entity: str, *,
                        ticker: str | None = None, sector: str | None = None,
                        currency: str = "INR",
                        market: MarketData | None = None,
                        prefer_consolidated: bool = True) -> dict[str, Any]:
    periods = db.list_periods(conn, entity)

    statements: dict[str, dict[str, Any]] = {s: {} for s in STATEMENTS}
    for statement in STATEMENTS:
        for period in periods:
            block = _statement_block(conn, entity, statement, period)
            if block:
                statements[statement][period] = block

    fundamentals: dict[str, dict[str, float | None]] = {}
    comps_metrics: dict[str, dict[str, Any]] = {}
    for i, period in enumerate(periods):
        prior = periods[i - 1] if i > 0 else None
        comp = build_company_comp(conn, entity, period, prior_period=prior, market=market,
                                   prefer_consolidated=prefer_consolidated)
        fundamentals[period] = comp.fundamentals
        comps_metrics[period] = {"metrics": comp.metrics, "valuation": comp.valuation,
                                  "missing_inputs": comp.missing_inputs}

    return {
        "id": _slugify(entity),
        "name": entity,
        "ticker": ticker,
        "sector": sector,
        "currency": currency,
        "market": {
            "price": market.price if market else None,
            "shares_outstanding": market.shares_outstanding if market else None,
            "net_debt_override": market.net_debt_override if market else None,
        },
        "periods": periods,
        "statements": statements,
        "fundamentals": fundamentals,
        "comps": comps_metrics,
    }


def write_site_data(conn: sqlite3.Connection, out_dir: str, companies: list[dict[str, Any]]) -> None:
    """`companies` is a list of dicts: {entity, ticker?, sector?, currency?, market?}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for spec in companies:
        market = spec.get("market")
        payload = build_company_json(
            conn, spec["entity"],
            ticker=spec.get("ticker"), sector=spec.get("sector"),
            currency=spec.get("currency", "INR"), market=market,
        )
        (out / f"{payload['id']}.json").write_text(json.dumps(payload, indent=2, default=str))
        index.append({"id": payload["id"], "name": payload["name"], "ticker": payload["ticker"],
                       "sector": payload["sector"], "periods": payload["periods"]})
    (out / "index.json").write_text(json.dumps(index, indent=2))
    print(f"Wrote {len(companies)} company file(s) + index.json to {out}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/financials.db")
    p.add_argument("--out", default="site/data")
    p.add_argument("--entity", action="append", default=[], help="repeat for each company")
    p.add_argument("--ticker", action="append", default=[])
    p.add_argument("--sector", action="append", default=[])
    p.add_argument("--price", action="append", default=[])
    p.add_argument("--shares", action="append", default=[])
    p.add_argument("--all", action="store_true", help="export every entity already in the store, no market data")
    args = p.parse_args()

    conn = db.init_db(args.db) if hasattr(db, "init_db") else None
    from core.schema import init_db
    conn = init_db(args.db)

    if args.all:
        entities = [{"entity": e} for e in list_entities(conn)]
    else:
        entities = []
        for i, entity in enumerate(args.entity):
            market = None
            if i < len(args.price) and i < len(args.shares):
                market = MarketData(price=float(args.price[i]), shares_outstanding=float(args.shares[i]))
            entities.append({
                "entity": entity,
                "ticker": args.ticker[i] if i < len(args.ticker) else None,
                "sector": args.sector[i] if i < len(args.sector) else None,
                "market": market,
            })

    write_site_data(conn, args.out, entities)
