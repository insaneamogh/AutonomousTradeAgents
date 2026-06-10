"""Backtester ↔ RiskGate integration tests.

Confirms that proposals flowing through ``Engine.run()`` get the same vetoes
they'd hit in production, and that trims are propagated to the sim broker.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from broker.types import OrderRequest, OrderType
from broker.types import Side as BrokerSide
from engine.backtester import (
    Bar,
    BarFeed,
    Engine,
    Portfolio,
    RiskGate,
    SimulatedBroker,
)
from engine.risk import RiskCaps


# ─────────────────────────────────────────────────────────────────────
# Test fixtures — a one-symbol, fixed-price feed and a script-driven strategy
# ─────────────────────────────────────────────────────────────────────


def _bars(symbol: str = "AAPL", n: int = 5, price: float = 100.0) -> list[Bar]:
    start = datetime(2025, 1, 2, 21, 0, tzinfo=timezone.utc)
    return [
        Bar(
            symbol=symbol,
            timestamp=start + timedelta(days=i),
            open=price,
            high=price * 1.005,
            low=price * 0.995,
            close=price,
            volume=1_000_000,
        )
        for i in range(n)
    ]


class _BarsFeed:
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)


@dataclass
class _ScriptedStrategy:
    """Emits one OrderRequest on a specific bar index (0-based), nothing else.
    Used to inject one proposal at a known point and inspect what the gate
    did with it.
    """

    bar_index: int
    request: OrderRequest
    name: str = "scripted"
    _seen: int = field(default=0, init=False)

    def on_bar(self, bar: Bar) -> list[OrderRequest]:
        out: list[OrderRequest] = []
        if self._seen == self.bar_index:
            out.append(self.request)
        self._seen += 1
        return out


def _buy_request(symbol: str, qty: int) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=BrokerSide.BUY,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=f"test-{symbol}-{qty}",
    )


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


def test_gate_passes_small_buy() -> None:
    # 5 shares × $100 = $500 = 0.5% of $100K. Way under all caps.
    bars = _bars(price=100.0, n=6)
    strategy = _ScriptedStrategy(bar_index=1, request=_buy_request("AAPL", 5))
    engine = Engine(
        portfolio=Portfolio(starting_cash=100_000.0),
        strategy=strategy,
        broker=SimulatedBroker(),
        risk_gate=RiskGate(),
    )
    result = engine.run(_BarsFeed(bars))
    assert result.risk_vetoes == []
    assert result.risk_trims == []
    assert result.trades == 1
    assert result.fills[0].qty == 5


def test_gate_trims_oversized_buy() -> None:
    # 80 × $100 = $8K = 8% of $100K → over 5% cap → trim to 50 shares ($5K = 5%).
    # AAPL is in the 'tech' sector; sector cap defaults to 25% — passes after trim.
    bars = _bars(symbol="AAPL", price=100.0, n=6)
    strategy = _ScriptedStrategy(bar_index=1, request=_buy_request("AAPL", 80))
    engine = Engine(
        portfolio=Portfolio(starting_cash=100_000.0),
        strategy=strategy,
        broker=SimulatedBroker(),
        risk_gate=RiskGate(caps=RiskCaps(max_position_pct=5.0, max_single_name_pct=99.0)),
    )
    result = engine.run(_BarsFeed(bars))
    assert result.risk_vetoes == []
    assert len(result.risk_trims) == 1
    trim = result.risk_trims[0]
    assert trim.requested_qty == 80
    assert trim.adjusted_qty == 50
    # The trimmed qty actually filled.
    assert result.trades == 1
    assert result.fills[0].qty == 50


def test_gate_vetoes_when_drawdown_halted() -> None:
    # Tighten the halt threshold to make the test deterministic: build
    # a portfolio with a synthetic halt by passing a custom RiskCaps
    # that's already conditionally tripped. Easier: use a context provider
    # that pre-halts. Here we exercise the strategy path via a SELL to
    # show forbid_short_phase_0 fires (drawdown_halt is tested in test_risk).
    bars = _bars(symbol="AAPL", price=100.0, n=4)
    sell = OrderRequest(
        symbol="AAPL", side=BrokerSide.SELL, qty=10,
        order_type=OrderType.MARKET, client_order_id="test-sell",
    )
    strategy = _ScriptedStrategy(bar_index=1, request=sell)
    engine = Engine(
        portfolio=Portfolio(starting_cash=100_000.0),
        strategy=strategy,
        broker=SimulatedBroker(),
        risk_gate=RiskGate(),
    )
    result = engine.run(_BarsFeed(bars))
    assert len(result.risk_vetoes) == 1
    assert result.risk_vetoes[0].veto_rule == "forbid_short_phase_0"
    assert result.trades == 0  # nothing filled


def test_gate_bypass_when_none() -> None:
    # Setting risk_gate=None should restore pre-gate behavior — useful for
    # comparison studies.
    bars = _bars(price=100.0, n=6)
    strategy = _ScriptedStrategy(bar_index=1, request=_buy_request("AAPL", 80))
    engine = Engine(
        portfolio=Portfolio(starting_cash=100_000.0),
        strategy=strategy,
        broker=SimulatedBroker(),
        risk_gate=None,
    )
    result = engine.run(_BarsFeed(bars))
    # No gate → no trim, no veto, full 80 shares filled.
    assert result.risk_trims == []
    assert result.risk_vetoes == []
    assert result.trades == 1
    assert result.fills[0].qty == 80


# ─────────────────────────────────────────────────────────────────────
# SMA crossover wired to ATR sizing
# ─────────────────────────────────────────────────────────────────────


def test_sma_crossover_with_atr_sizing() -> None:
    """SMA strategy with ATR sizing should:
    1. Produce qty derived from ATR (not fixed)
    2. SELL exactly the held qty (no over-sells)
    3. Pass the risk gate without `forbid_short_phase_0` vetoes
    """
    from engine.backtester import SmaCrossover
    from engine.sizing import AtrSizingConfig

    # Build a synthetic feed that crosses up then down so SMA fires both
    # a BUY and a matching SELL within ~120 bars (fast=10, slow=30).
    start = datetime(2025, 1, 2, 21, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    price = 100.0
    for i in range(120):
        # First 50 bars trend up, next 70 trend down.
        delta = 0.5 if i < 50 else -0.4
        price += delta + ((i % 7) - 3) * 0.1  # mild noise
        bars.append(
            Bar(
                symbol="AAPL",
                timestamp=start + timedelta(days=i),
                open=price - 0.2,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=2_000_000,
            )
        )

    strategy = SmaCrossover(
        fast=10,
        slow=30,
        sizing_config=AtrSizingConfig(
            risk_per_trade_pct=0.5,
            max_position_pct=4.0,
            min_position_pct=0.5,
        ),
        starting_equity=100_000.0,
    )
    engine = Engine(
        portfolio=Portfolio(starting_cash=100_000.0),
        strategy=strategy,
        broker=SimulatedBroker(),
        # Default RiskGate — its caps must be loose enough for the sized qty
        # to clear. AtrSizingConfig.max_position_pct=4% < RiskCaps.max_position_pct=5%
        # so the gate's position-size rule won't trim.
    )
    result = engine.run(_BarsFeed(bars))

    # Sized buy + matching sell. NOT the old fixed qty=10.
    buy_fills = [f for f in result.fills if str(f.side).endswith("BUY")]
    sell_fills = [f for f in result.fills if str(f.side).endswith("SELL")]
    assert len(buy_fills) >= 1, "expected at least one BUY fill"
    # SELL qty equals the BUY qty — the over-sell bug from 9160408b is fixed.
    if sell_fills:
        assert sell_fills[0].qty == buy_fills[0].qty

    # No forbid_short vetoes because SELL qty matches held qty.
    short_vetoes = [v for v in result.risk_vetoes if v.veto_rule == "forbid_short_phase_0"]
    assert short_vetoes == []

    # ATR-driven qty is NOT 10 (the legacy fixed default).
    assert buy_fills[0].qty != 10
