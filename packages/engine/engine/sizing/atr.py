"""ATR vol-targeted position sizing.

PLAN.md §6.3: "Never percent-of-account fixed. Volatility-targeted by
default (ATR-based or realized vol)." This is the ATR-based path.

Math:

    raw_risk_dollars   = (risk_per_trade_pct / 100) * equity * confidence
    stop_distance      = stop_atr_mult * atr_14
    qty_unclamped      = raw_risk_dollars / stop_distance
    qty_pre_floor      = clamp(qty_unclamped × last_price → [min, max] of equity)
                         / last_price
    qty                = floor(qty_pre_floor)

Stop + target prices:

    stop_price   = last_price - stop_atr_mult * atr_14            (for BUY)
    target_price = last_price + stop_atr_mult * atr_14 * R       (for BUY)

A 4%-ATR name gets a smaller qty than a 1.5%-ATR name for the same dollar
risk. That's the whole point.

Fallback: when ``atr_14`` is None / 0 / negative, we use
``fallback_position_pct`` of equity. Method returns "fallback_pct" so
callers can surface it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engine.sizing.types import SizingDecision, SizingInputs


@dataclass(frozen=True)
class AtrSizingConfig:
    risk_per_trade_pct: float = 0.5
    """Risk this % of equity per trade (the 1R). Industry-typical 0.25–1%."""
    stop_atr_mult: float = 2.0
    """Initial stop sits this many ATRs below entry."""
    target_r_multiple: float = 2.5
    """Take-profit = entry + (stop_atr_mult × atr × target_r_multiple)."""
    min_position_pct: float = 0.5
    """Floor on position size. Set 0 to allow "skip" outcomes."""
    max_position_pct: float = 5.0
    """Ceiling — mirrors RiskCaps.max_position_pct so the risk-gate trim
    rarely needs to fire on a sized proposal."""
    fallback_position_pct: float = 2.0
    """Used when ATR isn't available. Plain old % of equity."""
    min_qty: int = 1


def atr_position_size(
    inputs: SizingInputs,
    config: AtrSizingConfig | None = None,
) -> SizingDecision:
    """Vol-targeted sizing. Returns a SizingDecision with the qty + stops."""
    config = config or AtrSizingConfig()

    confidence = max(0.0, min(1.0, inputs.confidence))
    if confidence == 0.0:
        return SizingDecision(
            qty=0,
            target_notional=0.0,
            stop_price=inputs.last_price,
            target_price=inputs.last_price,
            method="atr" if (inputs.atr_14 and inputs.atr_14 > 0) else "fallback_pct",
            notes="confidence=0 → no trade",
        )

    if inputs.last_price <= 0 or inputs.account_equity <= 0:
        return SizingDecision(
            qty=0,
            target_notional=0.0,
            stop_price=inputs.last_price,
            target_price=inputs.last_price,
            method="atr",
            notes="non-positive price or equity → no trade",
        )

    atr = inputs.atr_14 if (inputs.atr_14 and inputs.atr_14 > 0) else None
    if atr is None:
        return _fallback_pct(inputs, config, confidence)

    return _vol_targeted(inputs, config, confidence, atr)


def _vol_targeted(
    inputs: SizingInputs,
    config: AtrSizingConfig,
    confidence: float,
    atr: float,
) -> SizingDecision:
    risk_dollars = (config.risk_per_trade_pct / 100.0) * inputs.account_equity * confidence
    stop_distance = config.stop_atr_mult * atr
    qty_unclamped = risk_dollars / stop_distance

    # Convert qty → notional → clamp by % of equity → back to qty.
    notional = qty_unclamped * inputs.last_price
    min_notional = (config.min_position_pct / 100.0) * inputs.account_equity
    max_notional = (config.max_position_pct / 100.0) * inputs.account_equity
    notional_clamped = max(min_notional, min(max_notional, notional))

    qty = math.floor(notional_clamped / inputs.last_price)
    if qty < config.min_qty:
        return SizingDecision(
            qty=0,
            target_notional=0.0,
            stop_price=inputs.last_price - stop_distance,
            target_price=inputs.last_price + stop_distance * config.target_r_multiple,
            method="atr",
            notes=f"qty rounded to 0 (notional ${notional_clamped:.2f}, price ${inputs.last_price:.2f})",
        )

    target_notional = qty * inputs.last_price
    stop_price = round(inputs.last_price - stop_distance, 4)
    target_price = round(
        inputs.last_price + stop_distance * config.target_r_multiple, 4
    )
    pct_of_equity = (target_notional / inputs.account_equity) * 100.0

    return SizingDecision(
        qty=qty,
        target_notional=round(target_notional, 2),
        stop_price=stop_price,
        target_price=target_price,
        method="atr",
        notes=(
            f"ATR-sized: risk_dollars=${risk_dollars:.2f} stop=${stop_distance:.2f}/share "
            f"qty={qty} notional=${target_notional:.2f} ({pct_of_equity:.2f}% of equity)"
        ),
    )


def _fallback_pct(
    inputs: SizingInputs,
    config: AtrSizingConfig,
    confidence: float,
) -> SizingDecision:
    """ATR missing → plain % of equity, scaled by confidence."""
    notional_target = (
        (config.fallback_position_pct / 100.0) * inputs.account_equity * confidence
    )
    min_notional = (config.min_position_pct / 100.0) * inputs.account_equity
    max_notional = (config.max_position_pct / 100.0) * inputs.account_equity
    notional_clamped = max(min_notional, min(max_notional, notional_target))

    qty = math.floor(notional_clamped / inputs.last_price)
    if qty < config.min_qty:
        return SizingDecision(
            qty=0,
            target_notional=0.0,
            stop_price=inputs.last_price,
            target_price=inputs.last_price,
            method="fallback_pct",
            notes=f"qty rounded to 0 in fallback path (notional ${notional_clamped:.2f})",
        )

    target_notional = qty * inputs.last_price
    # Without ATR we have no principled stop — use a flat 4% as a placeholder.
    flat_stop_pct = 4.0
    stop_distance = inputs.last_price * (flat_stop_pct / 100.0)
    stop_price = round(inputs.last_price - stop_distance, 4)
    target_price = round(
        inputs.last_price + stop_distance * config.target_r_multiple, 4
    )

    return SizingDecision(
        qty=qty,
        target_notional=round(target_notional, 2),
        stop_price=stop_price,
        target_price=target_price,
        method="fallback_pct",
        notes=f"No ATR — fallback to {config.fallback_position_pct}% of equity",
    )
