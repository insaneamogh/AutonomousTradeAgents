"""Premium-at-risk sizing tests — floor division, qty=0 HOLD, boundaries.

Pure-logic — no DB, no LLM, runs in milliseconds. Mirrors the style of
``packages/engine/tests/test_sizing.py`` (the equity ATR sizer's suite).
"""

from __future__ import annotations

from engine.options.sizing import OptionsSizingInputs, options_position_size
from engine.risk import RiskCaps


def test_floor_division_basic_case() -> None:
    """The exact example from the module docstring: $500 budget, $3.20 ask,
    x100 multiplier -> $320/contract -> 1 contract, $180 left unused."""
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=500.0, ask=3.20, multiplier=100)
    )
    assert decision.qty == 1
    assert "1 contract" in decision.notes
    assert "$320.00" in decision.notes
    assert "$500.00" in decision.notes


def test_floor_division_multiple_contracts() -> None:
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=1000.0, ask=2.00, multiplier=100)
    )
    # cost/contract = $200 -> floor(1000/200) = 5
    assert decision.qty == 5


def test_never_rounds_up() -> None:
    """$639.99 at $3.20 x100 ($320/contract) -> 1.999...  contracts, must
    floor to 1, never round to 2."""
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=639.99, ask=3.20, multiplier=100)
    )
    assert decision.qty == 1


def test_boundary_exact_multiple_of_contract_cost() -> None:
    """$640.00 / $320 = exactly 2.0 -> qty=2, no floor-rounding ambiguity."""
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=640.0, ask=3.20, multiplier=100)
    )
    assert decision.qty == 2


def test_qty_zero_when_budget_cannot_afford_one_contract() -> None:
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=100.0, ask=5.00, multiplier=100)
    )
    assert decision.qty == 0
    assert "exceeds" in decision.notes


def test_qty_zero_boundary_just_under_one_contract() -> None:
    """$319.99 budget can't quite afford a $320 contract."""
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=319.99, ask=3.20, multiplier=100)
    )
    assert decision.qty == 0


def test_qty_one_at_exact_contract_cost() -> None:
    """$320.00 budget exactly affords a $320 contract — the boundary goes
    the caller's way, not against it."""
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=320.0, ask=3.20, multiplier=100)
    )
    assert decision.qty == 1


def test_non_positive_ask_returns_zero() -> None:
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=500.0, ask=0.0, multiplier=100)
    )
    assert decision.qty == 0
    assert "non-positive" in decision.notes

    decision_neg = options_position_size(
        OptionsSizingInputs(budget_usd=500.0, ask=-1.0, multiplier=100)
    )
    assert decision_neg.qty == 0


def test_non_positive_budget_returns_zero() -> None:
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=0.0, ask=3.20, multiplier=100)
    )
    assert decision.qty == 0
    assert "non-positive" in decision.notes


def test_non_positive_multiplier_returns_zero() -> None:
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=500.0, ask=3.20, multiplier=0)
    )
    assert decision.qty == 0
    assert "multiplier" in decision.notes


def test_qty_is_never_negative() -> None:
    for bad in (
        OptionsSizingInputs(budget_usd=-100.0, ask=3.20, multiplier=100),
        OptionsSizingInputs(budget_usd=500.0, ask=-3.20, multiplier=100),
        OptionsSizingInputs(budget_usd=500.0, ask=3.20, multiplier=-100),
    ):
        assert options_position_size(bad).qty >= 0


# ─────────────────────────────────────────────────────────────────────
# The real reason options_max_premium_pct moved 1.0 -> 2.5
# (docs/PLAN_AGGRESSIVE_PROFILE.md §1) — not risk appetite, a sizing-floor
# bug. budget_usd here is computed exactly the way the real caller does
# (``trading_agents.nodes.drafter``: ``equity * caps.options_max_premium_pct
# / 100.0``), so this is a revert-checkable regression test, not a
# hand-picked number.
# ─────────────────────────────────────────────────────────────────────


def test_the_old_one_percent_cap_floored_a_twelve_dollar_contract_to_zero() -> None:
    """Documents the bug the aggressive profile happens to fix: at
    $100k equity and the CONSERVATIVE 1% cap, a $12.00 ask (x100 = $1,200)
    exceeds the $1,000 budget and floors to 0 contracts — a silent HOLD
    that never even reached the Refusal Ledger, because the sizer emits a
    HOLD via ``.notes``, not a veto."""
    equity = 100_000.0
    caps = RiskCaps()  # conservative default: options_max_premium_pct = 1.0
    budget_usd = equity * caps.options_max_premium_pct / 100.0
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=budget_usd, ask=12.0, multiplier=100)
    )
    assert decision.qty == 0


def test_a_twelve_dollar_contract_sizes_to_at_least_one() -> None:
    """The fix: under ``RiskCaps.aggressive_paper()`` (2.5%), the same
    $12.00 contract sizes to qty >= 1 instead of HOLDing. Revert
    ``options_max_premium_pct`` to 1.0 in ``aggressive_paper()`` to see
    this fail — it is the test that documents the real reason for the
    change, not just "the number is bigger now"."""
    equity = 100_000.0
    caps = RiskCaps.aggressive_paper()
    budget_usd = equity * caps.options_max_premium_pct / 100.0
    decision = options_position_size(
        OptionsSizingInputs(budget_usd=budget_usd, ask=12.0, multiplier=100)
    )
    assert decision.qty >= 1


# ── liquidity trim (the CME sizing hole) ─────────────────────────────


def test_open_interest_trims_a_position_the_budget_alone_would_oversize() -> None:
    """CME261016P00270000, reconstructed: ask $4.60, open interest 167,
    $2,300 of premium budget available. Budget alone sizes 5 contracts —
    which is what actually happened, and the position then gapped 26
    points between prints. At 1% of open interest it sizes 1."""
    decision = options_position_size(
        OptionsSizingInputs(
            budget_usd=2300.0,
            ask=4.60,
            multiplier=100,
            open_interest=167,
            max_pct_of_open_interest=1.0,
        )
    )
    assert decision.qty == 1
    assert "liquidity cap" in decision.notes
    assert "167" in decision.notes


def test_the_trim_does_not_bind_on_a_genuinely_liquid_contract() -> None:
    """SPY-shaped: 2,841 open interest allows 28 lots, far above what the
    dollar budget affords. The premium budget must stay the operative
    constraint on liquid names — this cap exists to shrink doubtful
    positions, not to shrink every position."""
    decision = options_position_size(
        OptionsSizingInputs(
            budget_usd=2300.0,
            ask=4.60,
            multiplier=100,
            open_interest=2841,
            max_pct_of_open_interest=1.0,
        )
    )
    assert decision.qty == 5
    assert "liquidity cap" not in decision.notes


def test_the_trim_never_rounds_a_viable_trade_to_zero() -> None:
    """Sizing TRIMS; it does not veto. A contract too thin to hold one lot
    is refused upstream by options_min_open_interest and the chain-depth
    gate, which is where a refusal belongs and where it gets a named
    reason in the ledger."""
    decision = options_position_size(
        OptionsSizingInputs(
            budget_usd=2300.0,
            ask=4.60,
            multiplier=100,
            open_interest=10,  # 1% of 10 == 0.1, floors to 0
            max_pct_of_open_interest=1.0,
        )
    )
    assert decision.qty == 1


def test_omitting_open_interest_leaves_sizing_exactly_as_it_was() -> None:
    """The trim is opt-in by passing real data. Every pre-existing caller
    and fixture that does not pass open interest must size identically to
    before, or this change silently alters unrelated paths."""
    without = options_position_size(
        OptionsSizingInputs(budget_usd=2300.0, ask=4.60, multiplier=100)
    )
    assert without.qty == 5
    assert "liquidity cap" not in without.notes

    disabled = options_position_size(
        OptionsSizingInputs(
            budget_usd=2300.0,
            ask=4.60,
            multiplier=100,
            open_interest=167,
            max_pct_of_open_interest=0.0,  # 0 turns the side off
        )
    )
    assert disabled.qty == 5
