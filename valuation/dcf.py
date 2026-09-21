"""Deterministic discounted-cash-flow valuation.

The caller supplies normalized financial history and explicit assumptions. No
LLM arithmetic belongs here. The engine returns a fully auditable valuation
bridge and a WACC x terminal-growth sensitivity matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence


class DCFValidationError(ValueError):
    """Raised when DCF inputs are incomplete or economically invalid."""


def _finite(name: str, value: float) -> float:
    value = float(value)
    if not isfinite(value):
        raise DCFValidationError(f"{name} must be finite")
    return value


def _pct(name: str, value: float) -> float:
    value = _finite(name, value)
    if value <= -1:
        raise DCFValidationError(f"{name} must be greater than -100%")
    return value


@dataclass(frozen=True)
class DCFInputs:
    """All inputs required by the deterministic DCF engine.

    `revenue`, `ebit_margin`, `tax_rate`, `da`, `capex`, and `delta_nwc`
    represent the forecast years. Monetary values must use one consistent
    currency/unit scale throughout the model.
    """

    revenue: Sequence[float]
    ebit_margin: Sequence[float]
    tax_rate: Sequence[float]
    da: Sequence[float]
    capex: Sequence[float]
    delta_nwc: Sequence[float]
    shares_outstanding: float
    cash: float
    debt: float
    wacc: float | None = None
    terminal_growth: float = 0.03
    exit_multiple: float | None = None
    risk_free_rate: float | None = None
    beta: float | None = None
    equity_risk_premium: float | None = None
    pre_tax_cost_of_debt: float | None = None
    effective_tax_rate: float | None = None
    debt_weight: float | None = None


@dataclass(frozen=True)
class DCFResult:
    enterprise_value: float
    equity_value: float
    value_per_share: float
    wacc: float
    terminal_value: float
    terminal_method: str
    projected_fcff: tuple[float, ...]
    discount_factors: tuple[float, ...]
    pv_of_fcff: tuple[float, ...]
    sensitivity: dict[str, dict[str, float]]


def _resolve_wacc(inputs: DCFInputs) -> float:
    if inputs.wacc is not None:
        wacc = _finite("wacc", inputs.wacc)
    else:
        fields = (
            inputs.risk_free_rate,
            inputs.beta,
            inputs.equity_risk_premium,
            inputs.pre_tax_cost_of_debt,
            inputs.debt_weight,
        )
        if any(x is None for x in fields):
            raise DCFValidationError("Provide wacc or all CAPM/capital-structure inputs")
        rf = _finite("risk_free_rate", inputs.risk_free_rate)  # type: ignore[arg-type]
        beta = _finite("beta", inputs.beta)  # type: ignore[arg-type]
        erp = _finite("equity_risk_premium", inputs.equity_risk_premium)  # type: ignore[arg-type]
        kd = _finite("pre_tax_cost_of_debt", inputs.pre_tax_cost_of_debt)  # type: ignore[arg-type]
        dw = _finite("debt_weight", inputs.debt_weight)  # type: ignore[arg-type]
        if not 0 <= dw <= 1:
            raise DCFValidationError("debt_weight must be between 0 and 1")
        ew = 1 - dw
        tax = inputs.effective_tax_rate if inputs.effective_tax_rate is not None else 0.0
        tax = _finite("effective_tax_rate", tax)
        if not 0 <= tax < 1:
            raise DCFValidationError("effective_tax_rate must be in [0, 1)")
        ke = rf + beta * erp
        wacc = ew * ke + dw * kd * (1 - tax)
    if wacc <= 0:
        raise DCFValidationError("wacc must be positive")
    if wacc <= inputs.terminal_growth and inputs.exit_multiple is None:
        raise DCFValidationError("wacc must exceed terminal growth for a perpetuity DCF")
    return wacc


def _validate_vectors(inputs: DCFInputs) -> int:
    n = len(inputs.revenue)
    vectors = {
        "revenue": inputs.revenue,
        "ebit_margin": inputs.ebit_margin,
        "tax_rate": inputs.tax_rate,
        "da": inputs.da,
        "capex": inputs.capex,
        "delta_nwc": inputs.delta_nwc,
    }
    if n == 0:
        raise DCFValidationError("At least one forecast year is required")
    for name, values in vectors.items():
        if len(values) != n:
            raise DCFValidationError(f"{name} must have {n} forecast values")
        for value in values:
            _finite(name, value)
    if inputs.shares_outstanding <= 0:
        raise DCFValidationError("shares_outstanding must be positive")
    _finite("cash", inputs.cash)
    _finite("debt", inputs.debt)
    return n


def _fcff(values: DCFInputs) -> tuple[float, ...]:
    return tuple(
        rev * margin * (1 - tax) + da - capex - d_nwc
        for rev, margin, tax, da, capex, d_nwc in zip(
            values.revenue, values.ebit_margin, values.tax_rate, values.da, values.capex, values.delta_nwc
        )
    )


def _terminal_value(last_fcff: float, wacc: float, g: float, exit_multiple: float | None) -> tuple[float, str]:
    if exit_multiple is not None:
        if exit_multiple <= 0:
            raise DCFValidationError("exit_multiple must be positive")
        return last_fcff * exit_multiple, "EXIT_MULTIPLE"
    return last_fcff * (1 + g) / (wacc - g), "PERPETUITY_GROWTH"


def _present_value(fcffs: Sequence[float], terminal_value: float, wacc: float) -> tuple[float, tuple[float, ...], tuple[float, ...]]:
    dfs = tuple(1 / ((1 + wacc) ** year) for year in range(1, len(fcffs) + 1))
    pv_fcff = tuple(cf * df for cf, df in zip(fcffs, dfs))
    pv_terminal = terminal_value * dfs[-1]
    return sum(pv_fcff) + pv_terminal, dfs, pv_fcff


def _sensitivity(fcffs: Sequence[float], shares: float, cash: float, debt: float,
                wacc: float, terminal_growth: float, wacc_steps: Sequence[float],
                growth_steps: Sequence[float]) -> dict[str, dict[str, float]]:
    grid: dict[str, dict[str, float]] = {}
    for w in wacc_steps:
        row: dict[str, float] = {}
        for g in growth_steps:
            if w <= g:
                row[f"{g:.4f}"] = float("nan")
                continue
            tv = fcffs[-1] * (1 + g) / (w - g)
            ev, _, _ = _present_value(fcffs, tv, w)
            row[f"{g:.4f}"] = (ev + cash - debt) / shares
        grid[f"{w:.4f}"] = row
    return grid


def run_dcf(inputs: DCFInputs, *, wacc_range: Sequence[float] | None = None,
            terminal_growth_range: Sequence[float] | None = None) -> DCFResult:
    """Run the deterministic DCF and return valuation + sensitivity."""
    n = _validate_vectors(inputs)
    wacc = _resolve_wacc(inputs)
    g = _pct("terminal_growth", inputs.terminal_growth)
    if inputs.exit_multiple is None and g >= wacc:
        raise DCFValidationError("terminal_growth must be below wacc")

    fcffs = _fcff(inputs)
    tv, method = _terminal_value(fcffs[-1], wacc, g, inputs.exit_multiple)
    ev, dfs, pv_fcff = _present_value(fcffs, tv, wacc)
    equity = ev + inputs.cash - inputs.debt
    per_share = equity / inputs.shares_outstanding

    wacc_steps = tuple(wacc_range) if wacc_range is not None else tuple(wacc + delta for delta in (-0.02, -0.01, 0.0, 0.01, 0.02))
    growth_steps = tuple(terminal_growth_range) if terminal_growth_range is not None else tuple(g + delta for delta in (-0.01, -0.005, 0.0, 0.005, 0.01))

    # Sensitivity is intentionally based on the perpetuity method because the
    # WACC x terminal-growth grid is undefined under an exit-multiple terminal.
    sensitivity = _sensitivity(
        fcffs, inputs.shares_outstanding, inputs.cash, inputs.debt,
        wacc, g, wacc_steps, growth_steps,
    )

    return DCFResult(
        enterprise_value=ev,
        equity_value=equity,
        value_per_share=per_share,
        wacc=wacc,
        terminal_value=tv,
        terminal_method=method,
        projected_fcff=tuple(fcffs),
        discount_factors=tuple(dfs),
        pv_of_fcff=tuple(pv_fcff),
        sensitivity=sensitivity,
    )
