"""Deterministic financial derivation engine governed by rules.yaml.

The LLM may choose what to ask for, but this module owns arithmetic,
input requirements, status, and confidence propagation.
"""

from __future__ import annotations

from pathlib import Path
import math
import re
from typing import Any

import yaml

from . import db
from .units import to_absolute, compatible as units_compatible, parse_unit
from .periods import canonicalize_period

RULES_PATH = Path(__file__).with_name("rules.yaml")


def load_rules() -> dict[str, Any]:
    with RULES_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_RULES = load_rules()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def canonicalize_metric(metric: str) -> str:
    cleaned = _norm(metric)
    if cleaned in _RULES.get("formulas", {}):
        return cleaned
    for canonical, aliases in _RULES.get("canonical_terms", {}).items():
        if cleaned == canonical or cleaned in {_norm(a) for a in aliases}:
            return canonical
    return metric.strip().lower().replace(" ", "_")


def _direct_lookup(conn, entity: str, metric: str, period: str, statement: str | None,
                   consolidated: bool | None) -> dict[str, Any]:
    canonical = canonicalize_metric(metric)
    canonical_period = canonicalize_period(period)
    rows = db.get_line_item(conn, entity, canonical, canonical_period, statement, consolidated)
    if not rows:
        return {"status": "UNAVAILABLE", "metric": canonical, "period": canonical_period,
                "reason": "required input not found"}
    if len(rows) > 1:
        return {"status": "CONFLICTED", "metric": canonical, "period": canonical_period,
                "reason": "multiple matching line items", "candidates": [dict(r) for r in rows]}
    row = dict(rows[0])
    confidence = row.get("extraction_confidence") or 0.0
    level = "HIGH" if confidence >= 0.9 else "MEDIUM" if confidence >= 0.6 else "LOW"
    return {
        "status": "REPORTED", "metric": canonical, "value": row["value"],
        "unit": row["unit"], "period": row["period"], "statement": row["statement"],
        "consolidated": row["consolidated"], "source_page": row["source_page"],
        "source_table": row["source_table"], "source_type": row.get("source_type", "MANUAL_UPLOAD"),
        "confidence": level, "extraction_confidence": confidence,
    }


def is_monetary_unit(unit_str: str | None) -> bool:
    return str(unit_str or "").strip().lower() not in {"%", "percent", "x", "days", "", "unspecified"}


def _currencies_compatible(inputs: list[dict[str, Any]]) -> bool:
    """Checks that no two monetary inputs disagree on *currency* — must
    never be silently combined, regardless of what their scales are."""
    money_units = [x.get("unit") for x in inputs
                   if x.get("status") in {"REPORTED", "DERIVED"} and is_monetary_unit(x.get("unit"))]
    for i in range(len(money_units)):
        for j in range(i + 1, len(money_units)):
            if not units_compatible(money_units[i], money_units[j]):
                return False
    return True


def _gather_values(formula_spec: dict[str, Any], inputs: list[dict[str, Any]]
                    ) -> tuple[dict[str, float] | None, str | None, dict[str, Any] | None]:
    """Turn resolved input items into a {metric: value} dict ready for
    _calc_expression, honoring Rule 6.

    Fast path: every input already shares the identical unit string (the
    common case — one statement, one filing). Use raw values as-is, so a
    derived money figure stays in the filing's own scale instead of being
    blown out to an absolute count nobody asked for.

    Slow path: units genuinely differ. Convert every monetary input to
    absolute terms before combining — this is what Rule 6 actually asks
    for — after first confirming no two inputs disagree on *currency*
    (that must never be silently combined). Returns
    (values, result_unit_mode, error) where result_unit_mode is
    "same" | "absolute", and error is a ready-to-return UNAVAILABLE
    reason dict if currencies conflict.
    """
    raw_units = [str(x.get("unit") or "").strip().lower() for x in inputs
                 if x.get("status") in {"REPORTED", "DERIVED"}]
    non_empty = [u for u in raw_units if u and u != "unspecified"]
    all_same_unit = len(set(non_empty)) <= 1

    if all_same_unit:
        values = {input_metric: float(item["value"])
                  for input_metric, item in zip(formula_spec["inputs"], inputs)}
        return values, "same", None

    if not _currencies_compatible(inputs):
        return None, None, {"reason": "inputs use incompatible currencies"}

    values: dict[str, float] = {}
    for input_metric, item in zip(formula_spec["inputs"], inputs):
        if is_monetary_unit(item.get("unit")):
            values[input_metric], _ = to_absolute(float(item["value"]), item.get("unit"))
        else:
            values[input_metric] = float(item["value"])
    return values, "absolute", None


def _calc_expression(name: str, values: dict[str, float]) -> float:
    if name == "gross_profit":
        return values["revenue"] - values["cost_of_revenue"]
    if name == "ebitda":
        return values["ebit"] + values["depreciation"] + values["amortisation"]
    if name == "net_debt":
        return values["total_debt"] - values["cash_and_equivalents"]
    if name == "net_cash":
        return values["cash_and_equivalents"] - values["total_debt"]
    if name == "working_capital":
        return values["current_assets"] - values["current_liabilities"]
    if name == "free_cash_flow":
        return values["operating_cash_flow"] - values["capital_expenditure"]
    if name == "ebitda_margin":
        return values["ebitda"] / values["revenue"] * 100
    if name == "ebit_margin":
        return values["ebit"] / values["revenue"] * 100
    if name == "gross_margin":
        return values["gross_profit"] / values["revenue"] * 100
    if name == "net_margin":
        return values["net_income"] / values["revenue"] * 100
    if name == "current_ratio":
        return values["current_assets"] / values["current_liabilities"]
    if name == "quick_ratio":
        return (values["cash_and_equivalents"] + values["short_term_investments"] + values["accounts_receivable"]) / values["current_liabilities"]
    if name == "asset_turnover":
        return values["revenue"] / values["total_assets"]
    if name == "receivable_days":
        return values["accounts_receivable"] / values["revenue"] * 365
    if name == "inventory_days":
        return values["inventory"] / values["cost_of_revenue"] * 365
    if name == "payable_days":
        return values["accounts_payable"] / values["cost_of_revenue"] * 365
    if name == "cash_conversion_cycle":
        return values["receivable_days"] + values["inventory_days"] - values["payable_days"]
    if name == "debt_to_equity":
        return values["total_debt"] / values["shareholders_equity"]
    if name == "debt_to_ebitda":
        return values["total_debt"] / values["ebitda"]
    if name == "net_debt_to_ebitda":
        return values["net_debt"] / values["ebitda"]
    if name == "interest_coverage":
        return values["ebit"] / values["finance_cost"]
    if name == "fcf_margin":
        return values["free_cash_flow"] / values["revenue"] * 100
    if name == "cfo_to_ebitda":
        return values["operating_cash_flow"] / values["ebitda"] * 100
    if name == "cfo_to_pat":
        return values["operating_cash_flow"] / values["net_income"] * 100
    raise ValueError(f"Formula not implemented: {name}")


def _confidence_level(inputs: list[dict[str, Any]]) -> str:
    levels = [x.get("confidence") for x in inputs]
    if "LOW" in levels:
        return "LOW"
    if "MEDIUM" in levels:
        return "MEDIUM"
    return "HIGH"


def _result_unit(formula_spec: dict[str, Any], inputs: list[dict[str, Any]], unit_mode: str | None) -> str | None:
    if formula_spec.get("unit"):
        return formula_spec["unit"]
    if unit_mode == "absolute":
        currencies = {parse_unit(x.get("unit"))[0] for x in inputs if x.get("status") in {"REPORTED", "DERIVED"}}
        currencies.discard(None)
        currency = next(iter(currencies), None)
        return f"{currency} (absolute)" if currency else "absolute (currency unspecified)"
    return inputs[0].get("unit") if inputs else None


def _resolve(conn, entity: str, metric: str, period: str, statement: str | None,
             consolidated: bool | None, stack: tuple[str, ...]) -> dict[str, Any]:
    requested = canonicalize_metric(metric)
    period = canonicalize_period(period)
    if requested in stack:
        return {"status": "CONFLICTED", "metric": requested, "period": period,
                "reason": "cyclic rule dependency"}

    direct = _direct_lookup(conn, entity, requested, period, statement, consolidated)
    if direct.get("status") == "REPORTED":
        return direct
    if requested not in _RULES.get("formulas", {}):
        return direct

    formula_spec = _RULES["formulas"][requested]
    inputs: list[dict[str, Any]] = []
    for input_metric in formula_spec["inputs"]:
        item = _resolve(conn, entity, input_metric, period, statement, consolidated, stack + (requested,))
        inputs.append(item)
        if item.get("status") not in {"REPORTED", "DERIVED"}:
            return {
                "status": item.get("status", "UNAVAILABLE"),
                "metric": requested, "period": period,
                "reason": f"missing or ambiguous input: {input_metric}",
                "formula": formula_spec["expression"], "inputs": inputs,
            }

    values, unit_mode, error = _gather_values(formula_spec, inputs)
    if error is not None:
        return {"status": "UNAVAILABLE", "metric": requested, "period": period,
                "reason": error["reason"], "formula": formula_spec["expression"], "inputs": inputs}

    if requested in {"debt_to_ebitda", "net_debt_to_ebitda"} and values.get("ebitda", 1) <= 0:
        return {"status": "UNAVAILABLE", "metric": requested, "period": period,
                "reason": "EBITDA is zero or negative; leverage multiple is not meaningful",
                "formula": formula_spec["expression"], "inputs": inputs}

    if requested == "net_cash" and values.get("total_debt", 0) > values.get("cash_and_equivalents", 0):
        return {"status": "UNAVAILABLE", "metric": requested, "period": period,
                "reason": "debt exceeds cash; report net_debt instead of net_cash",
                "formula": formula_spec["expression"], "inputs": inputs}

    for denom in ("revenue", "current_liabilities", "shareholders_equity", "finance_cost",
                  "ebitda", "net_income", "total_assets", "cost_of_revenue"):
        if denom in values and denom in formula_spec["expression"] and values[denom] == 0:
            return {"status": "UNAVAILABLE", "metric": requested, "period": period,
                    "reason": f"denominator {denom} is zero", "formula": formula_spec["expression"],
                    "inputs": inputs}

    try:
        result = _calc_expression(requested, values)
    except (ZeroDivisionError, ValueError):
        return {"status": "UNAVAILABLE", "metric": requested, "period": period,
                "reason": "calculation could not be completed", "formula": formula_spec["expression"],
                "inputs": inputs}

    if not math.isfinite(result):
        return {"status": "UNAVAILABLE", "metric": requested, "period": period,
                "reason": "non-finite calculation result", "inputs": inputs}

    source_pages = {str(x.get("metric")): x.get("source_page") for x in inputs}
    return {
        "status": "DERIVED", "metric": requested, "value": round(result, 6),
        "unit": _result_unit(formula_spec, inputs, unit_mode),
        "period": period, "statement": statement, "consolidated": consolidated,
        "confidence": _confidence_level(inputs), "formula": formula_spec["expression"],
        "inputs": inputs, "source_pages": source_pages,
    }


def calculate_metric(conn, entity: str, metric: str, period: str,
                     statement: str | None = None,
                     consolidated: bool | None = None) -> dict[str, Any]:
    return _resolve(conn, entity, metric, period, statement, consolidated, ())


def calculate_growth(conn, entity: str, metric: str, current_period: str,
                     prior_period: str, statement: str | None = None,
                     consolidated: bool | None = None) -> dict[str, Any]:
    base = canonicalize_metric(metric)
    current_period = canonicalize_period(current_period)
    prior_period = canonicalize_period(prior_period)
    mapping = {"revenue": "revenue_growth", "ebitda": "ebitda_growth",
               "net_income": "pat_growth", "ebit": "ebit_growth"}
    growth_metric = mapping.get(base)
    if growth_metric is None:
        return {"status": "UNAVAILABLE", "metric": base,
                "reason": "growth rule not defined for this metric"}

    current = _resolve(conn, entity, base, current_period, statement, consolidated, ())
    prior = _resolve(conn, entity, base, prior_period, statement, consolidated, ())
    inputs = [current, prior]
    if any(x.get("status") not in {"REPORTED", "DERIVED"} for x in inputs):
        return {"status": "UNAVAILABLE", "metric": growth_metric,
                "reason": "current or prior value unavailable", "inputs": inputs}
    if not units_compatible(current.get("unit"), prior.get("unit")):
        return {"status": "UNAVAILABLE", "metric": growth_metric,
                "reason": "current and prior values use incompatible currencies", "inputs": inputs}

    current_value, _ = to_absolute(float(current["value"]), current.get("unit"))
    prior_value, _ = to_absolute(float(prior["value"]), prior.get("unit"))
    if prior_value == 0 or prior_value < 0:
        return {"status": "UNAVAILABLE", "metric": growth_metric,
                "reason": "prior-period denominator is zero or negative", "inputs": inputs}
    result = (current_value / prior_value - 1) * 100
    return {
        "status": "DERIVED", "metric": growth_metric, "value": round(result, 6),
        "unit": "%", "period": current_period,
        "formula": f"({current_period} / {prior_period} - 1) * 100",
        "inputs": inputs, "confidence": _confidence_level(inputs),
        "source_pages": {current_period: current.get("source_page"), prior_period: prior.get("source_page")},
    }


_RETURN_RATIO_BASE = {"roa": "total_assets", "roe": "shareholders_equity"}


def calculate_return_ratio(conn, entity: str, ratio: str, period: str,
                           prior_period: str | None = None,
                           statement: str | None = None,
                           consolidated: bool | None = None) -> dict[str, Any]:
    """ROA / ROE — Rule 41 & 42: prefer the average of opening and closing
    balance-sheet base, and fall back to closing-only ONLY when the prior
    period isn't available, explicitly marked PROXY rather than DERIVED
    so the confidence hierarchy in Rule 59 stays honest.
    """
    ratio = ratio.strip().lower()
    period = canonicalize_period(period)
    prior_period = canonicalize_period(prior_period) if prior_period else None
    base_metric = _RETURN_RATIO_BASE.get(ratio)
    if base_metric is None:
        return {"status": "UNAVAILABLE", "metric": ratio, "reason": "ratio not defined (expected roa or roe)"}

    net_income = _resolve(conn, entity, "net_income", period, statement, consolidated, ())
    closing = _resolve(conn, entity, base_metric, period, statement, consolidated, ())
    inputs = [net_income, closing]
    if any(x.get("status") not in {"REPORTED", "DERIVED"} for x in inputs):
        return {"status": "UNAVAILABLE", "metric": ratio,
                "reason": f"missing net_income or {base_metric}", "inputs": inputs}
    if not units_compatible(net_income.get("unit"), closing.get("unit")):
        return {"status": "UNAVAILABLE", "metric": ratio,
                "reason": "net_income and balance-sheet base use incompatible currencies", "inputs": inputs}

    net_income_abs, _ = to_absolute(float(net_income["value"]), net_income.get("unit"))
    closing_abs, _ = to_absolute(float(closing["value"]), closing.get("unit"))

    if prior_period:
        opening = _resolve(conn, entity, base_metric, prior_period, statement, consolidated, ())
        if opening.get("status") in {"REPORTED", "DERIVED"} and units_compatible(opening.get("unit"), closing.get("unit")):
            opening_abs, _ = to_absolute(float(opening["value"]), opening.get("unit"))
            average = (opening_abs + closing_abs) / 2
            if average != 0:
                return {
                    "status": "DERIVED", "metric": ratio, "value": round(net_income_abs / average * 100, 6),
                    "unit": "%", "period": period,
                    "formula": f"net_income / average({base_metric}) * 100",
                    "inputs": inputs + [opening], "confidence": _confidence_level(inputs + [opening]),
                }

    if closing_abs == 0:
        return {"status": "UNAVAILABLE", "metric": ratio, "reason": f"closing {base_metric} is zero", "inputs": inputs}
    return {
        "status": "PROXY", "metric": ratio, "value": round(net_income_abs / closing_abs * 100, 6),
        "unit": "%", "period": period,
        "formula": f"net_income / closing_{base_metric} * 100",
        "reason": "opening-period balance unavailable; used closing balance only, per Rule 41",
        "inputs": inputs, "confidence": "LOW",
    }


def calculate_cagr(conn, entity: str, metric: str, start_period: str, end_period: str,
                   n_years: float, statement: str | None = None,
                   consolidated: bool | None = None) -> dict[str, Any]:
    """Rule 49 — never applied to a negative or zero starting value."""
    base = canonicalize_metric(metric)
    start_period = canonicalize_period(start_period)
    end_period = canonicalize_period(end_period)
    start = _resolve(conn, entity, base, start_period, statement, consolidated, ())
    end = _resolve(conn, entity, base, end_period, statement, consolidated, ())
    inputs = [start, end]
    if any(x.get("status") not in {"REPORTED", "DERIVED"} for x in inputs):
        return {"status": "UNAVAILABLE", "metric": f"{base}_cagr",
                "reason": "start or end value unavailable", "inputs": inputs}
    if not units_compatible(start.get("unit"), end.get("unit")):
        return {"status": "UNAVAILABLE", "metric": f"{base}_cagr",
                "reason": "start and end values use incompatible currencies", "inputs": inputs}

    start_abs, _ = to_absolute(float(start["value"]), start.get("unit"))
    end_abs, _ = to_absolute(float(end["value"]), end.get("unit"))
    if start_abs <= 0:
        return {"status": "UNAVAILABLE", "metric": f"{base}_cagr",
                "reason": "CAGR is not meaningful from a zero or negative starting value", "inputs": inputs}
    if n_years <= 0:
        return {"status": "UNAVAILABLE", "metric": f"{base}_cagr", "reason": "n_years must be positive"}

    result = (end_abs / start_abs) ** (1 / n_years) - 1
    return {
        "status": "DERIVED", "metric": f"{base}_cagr", "value": round(result * 100, 6),
        "unit": "%", "period": f"{start_period}->{end_period}",
        "formula": f"(({end_period} / {start_period}) ^ (1 / {n_years}) - 1) * 100",
        "inputs": inputs, "confidence": _confidence_level(inputs),
    }
