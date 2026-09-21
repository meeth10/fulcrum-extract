"""Accounting validation & reconciliation checks — Rule Book §52.

These are cross-checks, not derivations: each one returns a
PASS / FAIL / INSUFFICIENT_DATA verdict, never a reportable metric.
That distinction matters — a check with a missing input is not the
same as a check that quietly wasn't run. Per Rule 2, the absence of a
check result must stay visible rather than being folded into silence.

Use: run_all(conn, entity, period) after ingesting a filing, before
trusting any derived ratio for that entity/period. A FAIL here is the
cheapest signal you'll get that a table extracted wrong.
"""

from __future__ import annotations

from typing import Any

from .derivation import _resolve  # noqa: F401 — intentional reuse of the same resolution path
from .units import to_absolute

DEFAULT_TOLERANCE = 0.01  # 1% of the largest operand's magnitude


def _get(conn, entity: str, metric: str, period: str, statement: str | None, consolidated: bool | None):
    return _resolve(conn, entity, metric, period, statement, consolidated, ())


def _abs_value(item: dict[str, Any]) -> float | None:
    if item.get("status") not in {"REPORTED", "DERIVED"}:
        return None
    value, _ = to_absolute(float(item["value"]), item.get("unit"))
    return value


def _check(name: str, terms: dict[str, float | None], expression_desc: str,
           tolerance: float = DEFAULT_TOLERANCE) -> dict[str, Any]:
    missing = [k for k, v in terms.items() if v is None]
    if missing:
        return {"check": name, "result": "INSUFFICIENT_DATA", "missing": missing,
                "expression": expression_desc}
    total = sum(terms.values())
    scale = max(abs(v) for v in terms.values()) or 1.0
    passed = abs(total) <= tolerance * scale
    return {"check": name, "result": "PASS" if passed else "FAIL",
            "residual": round(total, 4), "tolerance": tolerance,
            "expression": expression_desc, "inputs": terms}


def balance_sheet_check(conn, entity, period, statement="balance_sheet", consolidated=None):
    a = _abs_value(_get(conn, entity, "total_assets", period, statement, consolidated))
    l = _abs_value(_get(conn, entity, "total_liabilities", period, statement, consolidated))
    e = _abs_value(_get(conn, entity, "shareholders_equity", period, statement, consolidated))
    return _check(
        "balance_sheet",
        {"total_assets": a,
         "-total_liabilities": None if l is None else -l,
         "-shareholders_equity": None if e is None else -e},
        "total_assets - total_liabilities - shareholders_equity ≈ 0",
        tolerance=0.005,
    )


def debt_composition_check(conn, entity, period, statement="balance_sheet", consolidated=None):
    total = _abs_value(_get(conn, entity, "total_debt", period, statement, consolidated))
    short = _abs_value(_get(conn, entity, "short_term_debt", period, statement, consolidated))
    long_ = _abs_value(_get(conn, entity, "long_term_debt", period, statement, consolidated))
    return _check(
        "debt_composition",
        {"total_debt": total,
         "-short_term_debt": None if short is None else -short,
         "-long_term_debt": None if long_ is None else -long_},
        "total_debt - short_term_debt - long_term_debt ≈ 0",
    )


def gross_profit_check(conn, entity, period, statement="income_statement", consolidated=None):
    gp = _abs_value(_get(conn, entity, "gross_profit", period, statement, consolidated))
    rev = _abs_value(_get(conn, entity, "revenue", period, statement, consolidated))
    cor = _abs_value(_get(conn, entity, "cost_of_revenue", period, statement, consolidated))
    return _check(
        "gross_profit",
        {"gross_profit": gp, "-revenue": None if rev is None else -rev, "cost_of_revenue": cor},
        "gross_profit - revenue + cost_of_revenue ≈ 0",
    )


def ebitda_check(conn, entity, period, statement="income_statement", consolidated=None):
    ebitda = _abs_value(_get(conn, entity, "ebitda", period, statement, consolidated))
    ebit = _abs_value(_get(conn, entity, "ebit", period, statement, consolidated))
    dep = _abs_value(_get(conn, entity, "depreciation", period, statement, consolidated))
    amort = _abs_value(_get(conn, entity, "amortisation", period, statement, consolidated))
    return _check(
        "ebitda",
        {"ebitda": ebitda,
         "-ebit": None if ebit is None else -ebit,
         "-depreciation": None if dep is None else -dep,
         "-amortisation": None if amort is None else -amort},
        "ebitda - ebit - depreciation - amortisation ≈ 0",
    )


def net_income_bridge_check(conn, entity, period, statement="income_statement", consolidated=None):
    """Rule 52's own text flags the nuance: a real residual here can
    legitimately be exceptional_items rather than an extraction error.
    This check surfaces the residual — it does not attempt to explain
    it, since guessing which cause applies would itself be an inference
    Rule 2 forbids."""
    pat = _abs_value(_get(conn, entity, "net_income", period, statement, consolidated))
    pbt = _abs_value(_get(conn, entity, "pbt", period, statement, consolidated))
    tax = _abs_value(_get(conn, entity, "tax_expense", period, statement, consolidated))
    return _check(
        "net_income_bridge",
        {"net_income": pat, "-pbt": None if pbt is None else -pbt, "tax_expense": tax},
        "net_income - pbt + tax_expense ≈ 0 (before exceptional items — see docstring)",
    )


def cash_flow_composition_check(conn, entity, period, statement="cash_flow", consolidated=None):
    cfo = _abs_value(_get(conn, entity, "operating_cash_flow", period, statement, consolidated))
    cfi = _abs_value(_get(conn, entity, "investing_cash_flow", period, statement, consolidated))
    cff = _abs_value(_get(conn, entity, "financing_cash_flow", period, statement, consolidated))
    net_change = _abs_value(_get(conn, entity, "net_change_in_cash", period, statement, consolidated))
    return _check(
        "cash_flow_composition",
        {"operating_cash_flow": cfo, "investing_cash_flow": cfi, "financing_cash_flow": cff,
         "-net_change_in_cash": None if net_change is None else -net_change},
        "cfo + cfi + cff - net_change_in_cash ≈ 0 (excludes fx effect)",
    )


def opening_closing_cash_check(conn, entity, period, statement="cash_flow", consolidated=None):
    opening = _abs_value(_get(conn, entity, "opening_cash", period, statement, consolidated))
    change = _abs_value(_get(conn, entity, "net_change_in_cash", period, statement, consolidated))
    closing = _abs_value(_get(conn, entity, "closing_cash", period, statement, consolidated))
    return _check(
        "opening_closing_cash",
        {"opening_cash": opening, "net_change_in_cash": change,
         "-closing_cash": None if closing is None else -closing},
        "opening_cash + net_change_in_cash - closing_cash ≈ 0",
    )


ALL_CHECKS = (
    balance_sheet_check,
    debt_composition_check,
    gross_profit_check,
    ebitda_check,
    net_income_bridge_check,
    cash_flow_composition_check,
    opening_closing_cash_check,
)


def run_all(conn, entity: str, period: str, consolidated: bool | None = None) -> list[dict[str, Any]]:
    """Run every Rule Book §52 check available for one entity/period.
    Checks whose inputs weren't ingested return INSUFFICIENT_DATA
    rather than being silently omitted from the list."""
    return [fn(conn, entity, period, consolidated=consolidated) for fn in ALL_CHECKS]
