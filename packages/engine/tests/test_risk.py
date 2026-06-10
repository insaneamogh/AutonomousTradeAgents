"""Risk-engine unit tests. Each test exercises one rule end-to-end through
``engine.risk.evaluate`` so the ordering / first-veto-wins logic stays
covered.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from engine.risk import (
    ClosedTrade,
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    RiskProposal,
    Side,
    SpecialistScore,
    evaluate,
)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _ctx(**overrides: object) -> RiskContext:
    base = dict(
        account_equity=100_000.0,
        cash=100_000.0,
        buying_power=200_000.0,
    )
    base.update(overrides)  # type: ignore[arg-type]
    return RiskContext(**base)  # type: ignore[arg-type]


def _buy(symbol: str = "AAPL", qty: int = 10, last_price: float = 150.0, **kw: object) -> RiskProposal:
    base = dict(
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        last_price=last_price,
        estimated_notional=qty * last_price,
        confidence=0.70,
        closes_intraday_position=False,
    )
    base.update(kw)  # type: ignore[arg-type]
    return RiskProposal(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


def test_happy_buy_passes() -> None:
    d = evaluate(_buy("AAPL", 10, 150.0), _ctx())
    assert d.approved
    assert d.veto_rule is None
    assert d.adjusted_qty is None


# ─────────────────────────────────────────────────────────────────────
# Drawdown circuit breaker
# ─────────────────────────────────────────────────────────────────────


def test_drawdown_already_halted_blocks_buy() -> None:
    ctx = _ctx(drawdown_halted=True, drawdown_halt_reason="Yesterday's loss")
    d = evaluate(_buy(), ctx)
    assert not d.approved
    assert d.veto_rule == "drawdown_halt_active"


def test_drawdown_just_tripped_blocks_and_explains() -> None:
    ctx = _ctx(daily_pnl_pct=-3.5)
    d = evaluate(_buy(), ctx)
    assert not d.approved
    assert d.veto_rule == "drawdown_halt_just_tripped"
    assert "-3.50" in d.reason or "-3.5" in d.reason


def test_drawdown_does_not_block_sells_so_user_can_flatten() -> None:
    # SELL on a held position is allowed even when halted.
    ctx = _ctx(
        drawdown_halted=True,
        open_positions=(PortfolioPosition("AAPL", 10, 150.0, 1500.0, "tech"),),
    )
    sell = _buy(symbol="AAPL", qty=10).__class__(  # build a SELL via dataclass replace
        symbol="AAPL",
        side=Side.SELL,
        qty=10,
        last_price=150.0,
        estimated_notional=1500.0,
        confidence=0.7,
        closes_intraday_position=False,
    )
    d = evaluate(sell, ctx)
    assert d.approved


# ─────────────────────────────────────────────────────────────────────
# PDT
# ─────────────────────────────────────────────────────────────────────


def test_pdt_blocks_4th_day_trade_under_25k() -> None:
    ctx = _ctx(account_equity=15_000.0, day_trades_last_5d=3)
    p = _buy(symbol="AAPL", qty=5, closes_intraday_position=True)
    # Day-trade close is conceptually a SELL on something opened today;
    # for the rule, side=SELL with closes_intraday_position=True.
    sell = p.__class__(
        symbol="AAPL", side=Side.SELL, qty=5, last_price=150.0,
        estimated_notional=750.0, confidence=0.7, closes_intraday_position=True,
    )
    ctx = RiskContext(
        account_equity=15_000.0,
        cash=15_000.0,
        buying_power=15_000.0,
        open_positions=(PortfolioPosition("AAPL", 5, 150.0, 750.0, "tech"),),
        day_trades_last_5d=3,
    )
    d = evaluate(sell, ctx)
    assert not d.approved
    assert d.veto_rule == "pdt_block"


def test_pdt_skipped_when_above_25k() -> None:
    ctx = RiskContext(
        account_equity=30_000.0,
        cash=30_000.0,
        buying_power=30_000.0,
        open_positions=(PortfolioPosition("AAPL", 5, 150.0, 750.0, "tech"),),
        day_trades_last_5d=5,  # well over the cap, but account is large enough
    )
    sell = RiskProposal(
        symbol="AAPL", side=Side.SELL, qty=5, last_price=150.0,
        estimated_notional=750.0, confidence=0.7, closes_intraday_position=True,
    )
    d = evaluate(sell, ctx)
    assert d.approved


# ─────────────────────────────────────────────────────────────────────
# Position-size cap (trim path)
# ─────────────────────────────────────────────────────────────────────


def test_position_size_trims_when_over_cap() -> None:
    # 50 × $200 = $10K = 10% of $100K equity. Cap position at 5% → trim to 25.
    # Loosen single-name + sector caps so they don't fire first.
    p = _buy(symbol="AAPL", qty=50, last_price=200.0)
    caps = RiskCaps(max_position_pct=5.0, max_single_name_pct=99.0, max_sector_pct=99.0)
    d = evaluate(p, _ctx(), caps=caps)
    assert d.approved
    assert d.adjusted_qty == 25
    assert any("trimmed" in f for f in d.informational_flags)


def test_position_size_blocks_when_trim_rounds_to_zero() -> None:
    # 1 share of a $200 stock against a $10 equity → trim rounds to 0 shares.
    # Use a non-classified symbol so sector rule no-ops; loosen single-name
    # so it doesn't catch the absurd ratio first.
    p = _buy(symbol="ZZZZ", qty=1, last_price=200.0)
    ctx = _ctx(account_equity=10.0, cash=10.0, buying_power=10.0)
    caps = RiskCaps(max_position_pct=5.0, min_qty=1, max_single_name_pct=9_999.0)
    d = evaluate(p, ctx, caps=caps)
    assert not d.approved
    assert d.veto_rule == "max_position_pct"


# ─────────────────────────────────────────────────────────────────────
# Sector concentration
# ─────────────────────────────────────────────────────────────────────


def test_sector_concentration_blocks_when_over_cap() -> None:
    # Three tech positions at 10% each = 30% → already over the 25% cap.
    held = tuple(
        PortfolioPosition(sym, 50, 200.0, 10_000.0, "tech")
        for sym in ("AAPL", "MSFT", "GOOGL")
    )
    ctx = _ctx(open_positions=held)
    p = _buy(symbol="NVDA", qty=10, last_price=200.0)  # +$2K tech
    d = evaluate(p, ctx, caps=RiskCaps(max_position_pct=10.0, max_sector_pct=25.0))
    assert not d.approved
    assert d.veto_rule == "sector_concentration"


def test_sector_passes_when_below_cap() -> None:
    held = (PortfolioPosition("AAPL", 50, 200.0, 10_000.0, "tech"),)
    ctx = _ctx(open_positions=held)
    p = _buy(symbol="JPM", qty=10, last_price=150.0)  # financials, not tech
    d = evaluate(p, ctx)
    assert d.approved


# ─────────────────────────────────────────────────────────────────────
# Correlation cap (cluster — tighter than sector)
# ─────────────────────────────────────────────────────────────────────


def test_correlation_cap_blocks_4th_megacap_tech() -> None:
    # AAPL, MSFT, GOOGL are all in the megacap_tech cluster.
    # Add a 4th (META, also megacap_tech) → block.
    held = (
        PortfolioPosition("AAPL", 10, 100.0, 1_000.0, "tech"),
        PortfolioPosition("MSFT", 10, 100.0, 1_000.0, "tech"),
        PortfolioPosition("GOOGL", 10, 100.0, 1_000.0, "tech"),
    )
    ctx = _ctx(open_positions=held)
    p = _buy(symbol="META", qty=5, last_price=100.0)  # also megacap_tech, $500
    d = evaluate(p, ctx, caps=RiskCaps(max_correlation_cluster=3))
    assert not d.approved
    assert d.veto_rule == "correlation_cap"
    assert "megacap_tech" in d.reason


def test_correlation_cap_allows_adding_to_existing_cluster_member() -> None:
    # Adding to a held member doesn't count as a new cluster name.
    held = (
        PortfolioPosition("AAPL", 10, 100.0, 1_000.0, "tech"),
        PortfolioPosition("MSFT", 10, 100.0, 1_000.0, "tech"),
        PortfolioPosition("GOOGL", 10, 100.0, 1_000.0, "tech"),
    )
    ctx = _ctx(open_positions=held)
    p = _buy(symbol="AAPL", qty=5, last_price=100.0)  # already held
    d = evaluate(p, ctx, caps=RiskCaps(max_correlation_cluster=3))
    assert d.approved
    assert d.veto_rule is None


def test_correlation_cap_skips_unclustered_symbols() -> None:
    # JPM is in the money_center_banks cluster, but the held names are all
    # in megacap_tech — JPM goes through.
    held = (
        PortfolioPosition("AAPL", 10, 100.0, 1_000.0, "tech"),
        PortfolioPosition("MSFT", 10, 100.0, 1_000.0, "tech"),
        PortfolioPosition("GOOGL", 10, 100.0, 1_000.0, "tech"),
    )
    ctx = _ctx(open_positions=held)
    p = _buy(symbol="JPM", qty=5, last_price=100.0)  # different cluster
    d = evaluate(p, ctx, caps=RiskCaps(max_correlation_cluster=3))
    assert d.approved


# ─────────────────────────────────────────────────────────────────────
# Specialist average score
# ─────────────────────────────────────────────────────────────────────


def test_specialist_avg_below_floor_blocks() -> None:
    d = evaluate(
        _buy(),
        _ctx(),
        specialists=(SpecialistScore("technical", 30.0, 0.4), SpecialistScore("fundamental", 35.0, 0.4)),
    )
    assert not d.approved
    assert d.veto_rule == "min_specialist_avg_score"


# ─────────────────────────────────────────────────────────────────────
# Wash-sale (INFORMATIONAL — never vetoes)
# ─────────────────────────────────────────────────────────────────────


def test_wash_sale_warns_when_recent_loss_on_same_symbol() -> None:
    recent = ClosedTrade(symbol="AAPL", closed_at=_today() - timedelta(days=10), realized_pnl=-150.0)
    ctx = _ctx(recent_losing_closes=(recent,))
    d = evaluate(_buy(symbol="AAPL"), ctx)
    assert d.approved                                         # informational ≠ veto
    assert "wash_sale_warning" in d.informational_flags
    # The rule's own reason gets dropped (final RiskDecision uses "All risk
    # checks passed"); we only verify the flag is propagated.


def test_wash_sale_silent_when_no_recent_close() -> None:
    d = evaluate(_buy(symbol="AAPL"), _ctx())
    assert d.approved
    assert "wash_sale_warning" not in d.informational_flags


def test_wash_sale_silent_when_close_older_than_lookback() -> None:
    # Default lookback is 30 days; 45 days old should not trigger.
    old = ClosedTrade(symbol="AAPL", closed_at=_today() - timedelta(days=45), realized_pnl=-200.0)
    ctx = _ctx(recent_losing_closes=(old,))
    d = evaluate(_buy(symbol="AAPL"), ctx)
    assert d.approved
    assert "wash_sale_warning" not in d.informational_flags


def test_wash_sale_silent_when_different_symbol() -> None:
    recent = ClosedTrade(symbol="MSFT", closed_at=_today(), realized_pnl=-50.0)
    ctx = _ctx(recent_losing_closes=(recent,))
    d = evaluate(_buy(symbol="AAPL"), ctx)  # buying a different ticker
    assert d.approved
    assert "wash_sale_warning" not in d.informational_flags


def test_wash_sale_silent_when_closed_at_profit() -> None:
    # Only LOSING closes count; a profitable close on the same name isn't
    # a wash-sale concern.
    winner = ClosedTrade(symbol="AAPL", closed_at=_today() - timedelta(days=5), realized_pnl=+250.0)
    ctx = _ctx(recent_losing_closes=(winner,))
    d = evaluate(_buy(symbol="AAPL"), ctx)
    assert d.approved
    assert "wash_sale_warning" not in d.informational_flags
