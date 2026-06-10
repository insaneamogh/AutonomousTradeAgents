"""ATR vol-targeted sizing tests.

The 7 cases from the AGENTV1 (k) playbook, plus a couple of edge-case
guards. Pure-logic — no DB, no LLM, runs in milliseconds.
"""

from __future__ import annotations

from engine.sizing import AtrSizingConfig, SizingInputs, atr_position_size


def test_atr_sizes_inversely_with_volatility() -> None:
    """High-ATR symbol → smaller qty than a low-ATR symbol at the same equity.
    Both use risk_per_trade_pct=0.5%, equity=$100K → $500 risk dollars.
    Low ATR  $1.00/share, stop = 2 × $1 = $2 → qty = 500/2 = 250
    High ATR $4.00/share, stop = 2 × $4 = $8 → qty = 500/8 = 62
    """
    low_vol = atr_position_size(
        SizingInputs(symbol="LOWVOL", last_price=50.0, atr_14=1.0,
                     account_equity=100_000.0, confidence=1.0),
        config=AtrSizingConfig(risk_per_trade_pct=0.5, stop_atr_mult=2.0,
                               max_position_pct=99.0, min_position_pct=0.0),
    )
    high_vol = atr_position_size(
        SizingInputs(symbol="HIGHVOL", last_price=50.0, atr_14=4.0,
                     account_equity=100_000.0, confidence=1.0),
        config=AtrSizingConfig(risk_per_trade_pct=0.5, stop_atr_mult=2.0,
                               max_position_pct=99.0, min_position_pct=0.0),
    )
    assert low_vol.qty > high_vol.qty
    assert low_vol.method == "atr"
    assert high_vol.method == "atr"


def test_atr_clamps_to_max_position_pct() -> None:
    """Tiny ATR would otherwise produce a massive notional — cap kicks in."""
    decision = atr_position_size(
        SizingInputs(symbol="AAPL", last_price=100.0, atr_14=0.10,
                     account_equity=100_000.0, confidence=1.0),
        config=AtrSizingConfig(risk_per_trade_pct=0.5, stop_atr_mult=2.0,
                               max_position_pct=5.0, min_position_pct=0.0),
    )
    # Max notional is 5% × $100K = $5,000 → max 50 shares at $100.
    assert decision.qty == 50
    assert decision.target_notional == 5000.0


def test_atr_clamps_to_min_position_pct() -> None:
    """Confidence × risk so tiny it would round to 0; min cap brings it up."""
    decision = atr_position_size(
        SizingInputs(symbol="AAPL", last_price=100.0, atr_14=5.0,
                     account_equity=100_000.0, confidence=0.05),
        config=AtrSizingConfig(risk_per_trade_pct=0.5, stop_atr_mult=2.0,
                               min_position_pct=2.0, max_position_pct=10.0),
    )
    # Risk dollars = 0.005 × 100000 × 0.05 = $25; stop dist=$10 → 2.5 shares.
    # But min_position_pct=2% × $100K = $2K → 20 shares.
    assert decision.qty == 20
    assert decision.method == "atr"


def test_confidence_zero_returns_zero_qty() -> None:
    decision = atr_position_size(
        SizingInputs(symbol="AAPL", last_price=100.0, atr_14=2.0,
                     account_equity=100_000.0, confidence=0.0),
    )
    assert decision.qty == 0
    assert "confidence=0" in decision.notes


def test_missing_atr_falls_back_to_pct() -> None:
    decision = atr_position_size(
        SizingInputs(symbol="AAPL", last_price=100.0, atr_14=None,
                     account_equity=100_000.0, confidence=1.0),
        config=AtrSizingConfig(fallback_position_pct=2.0, min_position_pct=0.0),
    )
    # 2% of $100K = $2000 → 20 shares at $100.
    assert decision.qty == 20
    assert decision.method == "fallback_pct"
    assert "fallback" in decision.notes.lower()


def test_zero_atr_falls_back() -> None:
    decision = atr_position_size(
        SizingInputs(symbol="AAPL", last_price=100.0, atr_14=0.0,
                     account_equity=100_000.0, confidence=1.0),
        config=AtrSizingConfig(fallback_position_pct=2.0, min_position_pct=0.0),
    )
    assert decision.method == "fallback_pct"
    assert decision.qty == 20


def test_stop_and_target_derived_from_atr() -> None:
    """Stop = entry − stop_atr_mult × ATR; target = entry + that × R-multiple."""
    decision = atr_position_size(
        SizingInputs(symbol="AAPL", last_price=200.0, atr_14=4.0,
                     account_equity=100_000.0, confidence=1.0),
        config=AtrSizingConfig(stop_atr_mult=2.0, target_r_multiple=2.5,
                               max_position_pct=99.0, min_position_pct=0.0),
    )
    # stop_distance = 2 × 4 = $8
    assert decision.stop_price == 192.0   # 200 − 8
    assert decision.target_price == 220.0  # 200 + 8 × 2.5


def test_negative_equity_or_price_returns_zero() -> None:
    """Defensive: caller passed bad inputs. Don't divide by zero."""
    bad_equity = atr_position_size(
        SizingInputs(symbol="X", last_price=100.0, atr_14=2.0,
                     account_equity=0.0, confidence=1.0)
    )
    assert bad_equity.qty == 0
    bad_price = atr_position_size(
        SizingInputs(symbol="X", last_price=0.0, atr_14=2.0,
                     account_equity=100_000.0, confidence=1.0)
    )
    assert bad_price.qty == 0
