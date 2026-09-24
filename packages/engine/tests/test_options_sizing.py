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


# ── conviction-scaled budget ──────────────────────────────────────────
#
# Before this, the council's confidence decided WHETHER to trade and never
# HOW MUCH: a 0.42 idea and a 0.62 idea drew the identical premium budget,
# so the least-convinced trade the system would take was also a maximum-
# size one. Measured over 151 real option decisions the modal FILLED
# conviction was the floor itself.


def _conv(budget: float, ask: float, conviction: float | None,
          floor: float | None = 0.48) -> int:
    return options_position_size(
        OptionsSizingInputs(
            budget_usd=budget, ask=ask, multiplier=100,
            conviction=conviction, conviction_floor=floor,
        )
    ).qty


def test_conviction_scaling_is_inert_unless_both_inputs_are_given() -> None:
    """Opt-in by passing real data — the same contract ``open_interest``
    already has. Every pre-existing caller must be unaffected."""
    full = _conv(1_500.0, 1.00, conviction=None, floor=None)
    assert full == 15
    assert _conv(1_500.0, 1.00, conviction=0.48, floor=None) == full
    assert _conv(1_500.0, 1.00, conviction=None, floor=0.48) == full


def test_floor_conviction_gets_about_half_size() -> None:
    assert _conv(1_500.0, 1.00, conviction=0.48) == 7  # floor(1500*0.5/100)


def test_ceiling_conviction_gets_full_size() -> None:
    assert _conv(1_500.0, 1.00, conviction=0.62) == 15


def test_conviction_above_the_observed_ceiling_does_not_oversize() -> None:
    """0.62 is the highest the two-agent council has ever produced (it
    resolves on the MINIMUM of bull and bear). A hypothetical 0.9 must
    clamp to full budget, never exceed it."""
    assert _conv(1_500.0, 1.00, conviction=0.90) == _conv(1_500.0, 1.00, conviction=0.62)


def test_a_floor_at_or_above_the_ceiling_falls_back_to_full_budget() -> None:
    """Guard against a divide-by-~zero if the floor is ever configured at
    or above the observed ceiling: fall back to full budget and let the
    named risk rules do the refusing, rather than inventing a number."""
    assert _conv(1_500.0, 1.00, conviction=0.62, floor=0.62) == 15
    assert _conv(1_500.0, 1.00, conviction=0.62, floor=0.80) == 15
