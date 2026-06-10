"""India-market risk rules + US-rule market gating.

Covers: symbol market detection, F&O lot-size veto/pass/unverified-flag,
derivative notional cap, MIS square-off window, and that PDT/wash-sale
self-gate to US symbols only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine.risk import RiskCaps, RiskContext, RiskProposal, Side, evaluate
from engine.risk.markets import (
    exchange_of,
    is_derivative,
    market_of,
    tradingsymbol_of,
)
from engine.risk.rules import (
    derivative_notional_cap,
    lot_size_block,
    mis_square_off_block,
    pdt_block,
    wash_sale,
)
from engine.risk.types import ClosedTrade


def _ctx(**overrides: object) -> RiskContext:
    defaults: dict[str, object] = dict(
        account_equity=1_000_000.0,
        cash=1_000_000.0,
        buying_power=1_000_000.0,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)  # type: ignore[arg-type]


def _proposal(symbol: str, qty: int, last_price: float, **overrides: object) -> RiskProposal:
    defaults: dict[str, object] = dict(
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        estimated_notional=qty * last_price,
        last_price=last_price,
        confidence=0.9,
    )
    defaults.update(overrides)
    return RiskProposal(**defaults)  # type: ignore[arg-type]


# ── Market detection ─────────────────────────────────────────────────


def test_market_detection() -> None:
    assert market_of("AAPL") == "US"
    assert market_of("NSE:RELIANCE") == "IN"
    assert market_of("BSE:SENSEX") == "IN"
    assert market_of("NFO:NIFTY24DECFUT") == "IN"
    assert market_of("nse:infy") == "IN"  # case-insensitive prefix
    assert exchange_of("AAPL") is None
    assert exchange_of("NFO:X") == "NFO"
    assert tradingsymbol_of("NFO:NIFTY24DECFUT") == "NIFTY24DECFUT"
    assert tradingsymbol_of("AAPL") == "AAPL"


def test_derivative_detection() -> None:
    assert is_derivative("NFO:NIFTY24DECFUT")
    assert is_derivative("MCX:CRUDEOIL24JUNFUT")
    assert not is_derivative("NSE:RELIANCE")
    assert not is_derivative("AAPL")


# ── Lot size ─────────────────────────────────────────────────────────


def test_lot_size_vetoes_off_lot_qty() -> None:
    d = lot_size_block(_proposal("NFO:NIFTY24DECFUT", 80, 24_000.0), _ctx(), RiskCaps())
    assert d is not None and not d.approved
    assert d.veto_rule == "lot_size_block"
    assert "75" in d.reason


def test_lot_size_passes_whole_lots() -> None:
    assert lot_size_block(_proposal("NFO:NIFTY24DECFUT", 75, 24_000.0), _ctx(), RiskCaps()) is None
    assert lot_size_block(_proposal("NFO:NIFTY24DECFUT", 150, 24_000.0), _ctx(), RiskCaps()) is None


def test_lot_size_longest_prefix_wins() -> None:
    # BANKNIFTY (35) must match before NIFTY (75).
    d = lot_size_block(_proposal("NFO:BANKNIFTY24DECFUT", 35, 51_000.0), _ctx(), RiskCaps())
    assert d is None
    d = lot_size_block(_proposal("NFO:BANKNIFTY24DECFUT", 75, 51_000.0), _ctx(), RiskCaps())
    assert d is not None and not d.approved


def test_lot_size_unknown_underlying_flags_not_vetoes() -> None:
    d = lot_size_block(_proposal("NFO:RELIANCE24DECFUT", 250, 2_900.0), _ctx(), RiskCaps())
    assert d is not None and d.approved
    assert any(f.startswith("lot_size_unverified:") for f in d.informational_flags)


def test_lot_size_ignores_equity_and_us() -> None:
    assert lot_size_block(_proposal("NSE:RELIANCE", 7, 2_900.0), _ctx(), RiskCaps()) is None
    assert lot_size_block(_proposal("AAPL", 7, 200.0), _ctx(), RiskCaps()) is None


# ── Derivative notional cap ──────────────────────────────────────────


def test_derivative_notional_cap_vetoes_oversize() -> None:
    # 75 * 24,000 = 1.8M notional vs 20% of 1M equity = 200K cap.
    d = derivative_notional_cap(
        _proposal("NFO:NIFTY24DECFUT", 75, 24_000.0), _ctx(), RiskCaps()
    )
    assert d is not None and not d.approved
    assert d.veto_rule == "derivative_notional_cap"


def test_derivative_notional_cap_passes_within_cap() -> None:
    # 75 * 240 = 18K vs 200K cap — an options premium-sized order.
    d = derivative_notional_cap(
        _proposal("NFO:NIFTY2461924000CE", 75, 240.0), _ctx(), RiskCaps()
    )
    assert d is None


def test_derivative_notional_cap_ignores_equity_symbols() -> None:
    d = derivative_notional_cap(
        _proposal("NSE:RELIANCE", 1_000, 2_900.0), _ctx(), RiskCaps()
    )
    assert d is None


# ── MIS square-off window ────────────────────────────────────────────


def _utc_for_ist(hour: int, minute: int) -> datetime:
    # IST = UTC+5:30 → 15:00 IST == 09:30 UTC.
    return datetime(2026, 6, 10, hour, minute, tzinfo=timezone.utc)


def test_mis_blocked_after_cutoff() -> None:
    ctx = _ctx(now_utc=_utc_for_ist(9, 45))  # 15:15 IST
    d = mis_square_off_block(
        _proposal("NSE:RELIANCE", 10, 2_900.0, is_intraday=True), ctx, RiskCaps()
    )
    assert d is not None and not d.approved
    assert d.veto_rule == "mis_square_off_block"


def test_mis_allowed_before_cutoff() -> None:
    ctx = _ctx(now_utc=_utc_for_ist(5, 0))  # 10:30 IST
    d = mis_square_off_block(
        _proposal("NSE:RELIANCE", 10, 2_900.0, is_intraday=True), ctx, RiskCaps()
    )
    assert d is None


def test_mis_rule_ignores_delivery_and_us() -> None:
    ctx = _ctx(now_utc=_utc_for_ist(9, 45))
    assert mis_square_off_block(
        _proposal("NSE:RELIANCE", 10, 2_900.0, is_intraday=False), ctx, RiskCaps()
    ) is None
    assert mis_square_off_block(
        _proposal("AAPL", 10, 200.0, is_intraday=True), ctx, RiskCaps()
    ) is None


# ── US rules gate on market ──────────────────────────────────────────


def test_pdt_does_not_fire_for_india() -> None:
    ctx = _ctx(account_equity=10_000.0, day_trades_last_5d=3)
    proposal = _proposal(
        "NSE:RELIANCE", 10, 2_900.0, closes_intraday_position=True
    )
    assert pdt_block(proposal, ctx, RiskCaps()) is None


def test_pdt_still_fires_for_us() -> None:
    ctx = _ctx(account_equity=10_000.0, day_trades_last_5d=3)
    proposal = _proposal("AAPL", 10, 200.0, closes_intraday_position=True)
    d = pdt_block(proposal, ctx, RiskCaps())
    assert d is not None and not d.approved and d.veto_rule == "pdt_block"


def test_wash_sale_does_not_flag_india() -> None:
    ctx = _ctx(
        recent_losing_closes=(
            ClosedTrade(
                symbol="NSE:RELIANCE",
                closed_at=datetime.now(timezone.utc).date(),
                realized_pnl=-500.0,
            ),
        )
    )
    assert wash_sale(_proposal("NSE:RELIANCE", 10, 2_900.0), ctx, RiskCaps()) is None


# ── Through the full engine ──────────────────────────────────────────


def test_evaluate_vetoes_off_lot_india_derivative() -> None:
    d = evaluate(_proposal("NFO:NIFTY24DECFUT", 80, 240.0), _ctx())
    assert not d.approved and d.veto_rule == "lot_size_block"


def test_evaluate_approves_clean_india_option_order() -> None:
    d = evaluate(
        _proposal("NFO:NIFTY2461924000CE", 75, 240.0),
        _ctx(now_utc=_utc_for_ist(5, 0)),
    )
    assert d.approved, d.reason
