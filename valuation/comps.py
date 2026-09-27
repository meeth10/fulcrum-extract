"""Deterministic comparable-company analysis.

Replaces the old comps_adapter.py, which called a second, separately-run
FastAPI service (meeth10/Comp_analysis) over HTTP. That service's
computation logic — the metric set documented in docs/COMPS_INTEGRATION.md —
is reimplemented here directly against this repo's own store, so "comps" is
one codebase and one process, not two services that have to both be running
and kept in sync.

Fundamentals (revenue, EBITDA, net income, ...) come from the structured
store — the same extracted-from-filings data everything else here uses.
Market data (share price, shares outstanding) is NOT something a filing
reports for itself, so it's supplied separately as `market_data`, keyed by
entity.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from core import db


FUNDAMENTAL_METRICS = (
    "revenue", "ebitda", "ebit", "net_income", "shareholders_equity",
    "total_debt", "cash_and_equivalents", "operating_cash_flow", "capital_expenditure",
)


@dataclass
class MarketData:
    price: float | None = None
    shares_outstanding: float | None = None
    net_debt_override: float | None = None


@dataclass
class CompanyComp:
    entity: str
    period: str
    prior_period: str | None
    fundamentals: dict[str, float | None] = field(default_factory=dict)
    metrics: dict[str, float | None] = field(default_factory=dict)
    valuation: dict[str, float | None] = field(default_factory=dict)
    missing_inputs: list[str] = field(default_factory=list)


def _get_value(conn: sqlite3.Connection, entity: str, metric: str, period: str,
                prefer_consolidated: bool = True) -> float | None:
    rows = db.get_line_item(conn, entity, metric, period)
    if not rows:
        return None
    if len(rows) > 1:
        # Consolidated vs standalone is a categorical choice, not a quality
        # signal — sorting by confidence alone could silently mix the two
        # conventions for different metrics in the same comparison. Prefer
        # whichever side prefer_consolidated asks for (falling back to the
        # other side only if that one wasn't extracted for this metric),
        # and only break remaining ties by confidence.
        wanted = 1 if prefer_consolidated else 0
        matching = [r for r in rows if r["consolidated"] == wanted]
        candidates = matching or rows
        candidates = sorted(candidates, key=lambda r: (r["extraction_confidence"] or 0.0), reverse=True)
        return candidates[0]["value"]
    return rows[0]["value"]


def _fundamentals_for(conn: sqlite3.Connection, entity: str, period: str,
                       prefer_consolidated: bool = True) -> dict[str, float | None]:
    return {m: _get_value(conn, entity, m, period, prefer_consolidated) for m in FUNDAMENTAL_METRICS}


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def build_company_comp(conn: sqlite3.Connection, entity: str, period: str,
                        prior_period: str | None = None,
                        market: MarketData | None = None,
                        prefer_consolidated: bool = True) -> CompanyComp:
    market = market or MarketData()
    f = _fundamentals_for(conn, entity, period, prefer_consolidated)
    prior = _fundamentals_for(conn, entity, prior_period, prefer_consolidated) if prior_period else {}

    missing = [k for k, v in f.items() if v is None]

    net_debt = market.net_debt_override
    if net_debt is None and f["total_debt"] is not None and f["cash_and_equivalents"] is not None:
        net_debt = f["total_debt"] - f["cash_and_equivalents"]

    free_cash_flow = None
    if f["operating_cash_flow"] is not None and f["capital_expenditure"] is not None:
        # capital_expenditure's sign convention varies by filing: some print
        # it as a positive "spend" figure, others print it in parentheses
        # in the cash-flow statement (an outflow), which extraction then
        # stores as already-negative. FCF = OCF - capex only comes out
        # right in the first convention; in the second, subtracting an
        # already-negative number adds it back, silently inflating FCF.
        # Taking the magnitude makes the formula correct under either
        # convention, since capex is definitionally a cash outflow.
        free_cash_flow = f["operating_cash_flow"] - abs(f["capital_expenditure"])

    # Fundamental (non-valuation) comparables — don't need market data.
    metrics: dict[str, float | None] = {
        "ebitda_margin": _safe_div(f["ebitda"], f["revenue"]),
        "net_margin": _safe_div(f["net_income"], f["revenue"]),
        "roe": _safe_div(f["net_income"], f["shareholders_equity"]),
        "roic": _safe_div(
            f["ebit"],
            (f["total_debt"] + f["shareholders_equity"] - f["cash_and_equivalents"])
            if None not in (f["total_debt"], f["shareholders_equity"], f["cash_and_equivalents"]) else None,
        ),
        "free_cash_flow": free_cash_flow,
        "net_debt": net_debt,
        "net_debt_to_ebitda": _safe_div(net_debt, f["ebitda"]),
        "revenue_growth": _safe_div(
            (f["revenue"] - prior.get("revenue")) if prior.get("revenue") is not None and f["revenue"] is not None else None,
            prior.get("revenue"),
        ),
    }

    # Valuation multiples — need price/shares, so absent unless market data
    # was supplied.
    valuation: dict[str, float | None] = {}
    if market.price is not None and market.shares_outstanding is not None:
        market_cap = market.price * market.shares_outstanding
        enterprise_value = market_cap + (net_debt or 0.0) if net_debt is not None else None
        valuation.update({
            "market_cap": market_cap,
            "enterprise_value": enterprise_value,
            "pe": _safe_div(market_cap, f["net_income"]),
            "pb": _safe_div(market_cap, f["shareholders_equity"]),
            "ev_ebitda": _safe_div(enterprise_value, f["ebitda"]) if enterprise_value is not None else None,
            "ev_ebit": _safe_div(enterprise_value, f["ebit"]) if enterprise_value is not None else None,
            "ev_sales": _safe_div(enterprise_value, f["revenue"]) if enterprise_value is not None else None,
            "fcf_yield": _safe_div(free_cash_flow, market_cap),
        })

    return CompanyComp(
        entity=entity, period=period, prior_period=prior_period,
        fundamentals=f, metrics=metrics, valuation=valuation, missing_inputs=missing,
    )


def build_comparison(conn: sqlite3.Connection, entities: list[str], period: str,
                      prior_period: str | None = None,
                      market_data: dict[str, MarketData] | None = None,
                      prefer_consolidated: bool = True) -> dict[str, Any]:
    """Build a peer set. Mirrors the shape the old /comparisons/build HTTP
    endpoint returned, so downstream consumers (export/site_data.py, the
    Excel export) didn't need to change when the second service was folded
    in here."""
    market_data = market_data or {}
    companies = [
        build_company_comp(conn, entity, period, prior_period, market_data.get(entity), prefer_consolidated)
        for entity in entities
    ]
    return {
        "status": "DERIVED",
        "period": period,
        "companies": [
            {
                "entity": c.entity,
                "period": c.period,
                "metrics": c.metrics,
                "valuation": c.valuation,
                "missing_inputs": c.missing_inputs,
            }
            for c in companies
        ],
        "source": "local",  # was "Comp_analysis" (external service) before folding this in
    }
