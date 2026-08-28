"""Premium-at-risk sizing tests — floor division, qty=0 HOLD, boundaries.

Pure-logic — no DB, no LLM, runs in milliseconds. Mirrors the style of
``packages/engine/tests/test_sizing.py`` (the equity ATR sizer's suite).
"""

from __future__ import annotations

from engine.options.sizing import OptionsSizingInputs, options_position_size


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
