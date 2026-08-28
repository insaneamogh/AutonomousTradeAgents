"""Contract-selection tests — synthetic ContractQuote fixtures, no network.

Covers each filter stage independently (contract type, DTE window boundary,
delta band by conviction, the liquidity floor's three sub-conditions,
missing-IV rejection), the tie-break, and the empty-funnel HOLD-with-named-
reason case. Pure-logic — no DB, no LLM, runs in milliseconds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from engine.options.selection import (
    ContractQuote,
    ContractSelectionInputs,
    select_contract,
)

_NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)


def _quote(
    *,
    occ_symbol: str = "AAPL261016C00250000",
    contract_type: Literal["call", "put"] = "call",
    strike: float = 250.0,
    dte: int = 30,
    bid: float | None = 3.10,
    ask: float | None = 3.30,
    open_interest: int | None = 500,
    volume: int | None = 200,
    delta: float | None = 0.50,
    implied_volatility: float | None = 0.28,
) -> ContractQuote:
    """A liquid, well-priced, mid-conviction-band 30-DTE call by default —
    every test overrides only the field(s) it's exercising."""
    return ContractQuote(
        occ_symbol=occ_symbol,
        contract_type=contract_type,
        strike=strike,
        expiry=(_NOW + timedelta(days=dte)).date(),
        bid=bid,
        ask=ask,
        open_interest=open_interest,
        volume=volume,
        delta=delta,
        implied_volatility=implied_volatility,
    )


def _inputs(
    candidates: tuple[ContractQuote, ...],
    *,
    direction: Literal["long", "short"] = "long",
    conviction: float = 0.8,
    days_to_earnings: int | None = None,
    realized_vol_pct: float | None = None,
) -> ContractSelectionInputs:
    # 0.8 is high-conviction (band [0.45, 0.65]), matching _quote()'s own
    # default delta=0.50 — tests exercising the conviction/delta-band
    # relationship itself pass their own explicit conviction below.
    return ContractSelectionInputs(
        underlying_symbol="AAPL",
        direction=direction,
        conviction=conviction,
        candidates=candidates,
        now=_NOW,
        days_to_earnings=days_to_earnings,
        realized_vol_pct=realized_vol_pct,
    )


# ─────────────────────────────────────────────────────────────────────
# Contract type (long -> call, short -> put)
# ─────────────────────────────────────────────────────────────────────


def test_long_thesis_selects_a_call_not_a_put() -> None:
    call = _quote(contract_type="call", occ_symbol="AAPL261016C00250000")
    put = _quote(contract_type="put", occ_symbol="AAPL261016P00250000")
    result = select_contract(_inputs((call, put), direction="long"))
    assert result.selected is not None
    assert result.selected.contract_type == "call"
    assert result.selected.occ_symbol == "AAPL261016C00250000"


def test_short_thesis_selects_a_put_not_a_call() -> None:
    call = _quote(contract_type="call", occ_symbol="AAPL261016C00250000")
    put = _quote(contract_type="put", occ_symbol="AAPL261016P00250000")
    result = select_contract(_inputs((call, put), direction="short"))
    assert result.selected is not None
    assert result.selected.contract_type == "put"
    assert result.selected.occ_symbol == "AAPL261016P00250000"


def test_no_matching_contract_type_is_a_named_hold() -> None:
    only_puts = (_quote(contract_type="put"),)
    result = select_contract(_inputs(only_puts, direction="long"))
    assert result.selected is None
    assert result.rejection_reason == "no_matching_contract_type"
    assert result.funnel_counts["total"] == 1
    assert result.funnel_counts["contract_type"] == 0


# ─────────────────────────────────────────────────────────────────────
# DTE window boundaries (21 <= dte <= 45)
# ─────────────────────────────────────────────────────────────────────


def test_dte_just_below_window_is_excluded() -> None:
    result = select_contract(_inputs((_quote(dte=20),)))
    assert result.selected is None
    assert result.rejection_reason == "no_expiry_in_window"


def test_dte_at_lower_boundary_is_included() -> None:
    result = select_contract(_inputs((_quote(dte=21),)))
    assert result.selected is not None


def test_dte_at_upper_boundary_is_included() -> None:
    result = select_contract(_inputs((_quote(dte=45),)))
    assert result.selected is not None


def test_dte_just_above_window_is_excluded() -> None:
    result = select_contract(_inputs((_quote(dte=46),)))
    assert result.selected is None
    assert result.rejection_reason == "no_expiry_in_window"


def test_dte_computed_fresh_not_trusted_precomputed() -> None:
    """A quote's ``expiry`` alone determines DTE — there is no separate
    stored dte field to trust or distrust; this pins that the boundary is
    evaluated against ``inputs.now``, not any value baked into the fixture."""
    later_now = _NOW + timedelta(days=10)
    inputs = ContractSelectionInputs(
        underlying_symbol="AAPL",
        direction="long",
        conviction=0.5,
        candidates=(_quote(dte=30),),  # 30 DTE from _NOW -> 20 DTE from later_now
        now=later_now,
    )
    result = select_contract(inputs)
    assert result.selected is None
    assert result.rejection_reason == "no_expiry_in_window"


# ─────────────────────────────────────────────────────────────────────
# Delta band by conviction
# ─────────────────────────────────────────────────────────────────────


def test_high_conviction_wants_closer_to_the_money_delta() -> None:
    result = select_contract(_inputs((_quote(delta=0.50),), conviction=0.9))
    assert result.selected is not None
    assert result.selected.strike == 250.0


def test_high_conviction_rejects_a_far_otm_delta() -> None:
    result = select_contract(_inputs((_quote(delta=0.30),), conviction=0.9))
    assert result.selected is None
    assert result.rejection_reason == "no_delta_in_band"


def test_low_conviction_wants_further_otm_delta() -> None:
    result = select_contract(_inputs((_quote(delta=0.30),), conviction=0.4))
    assert result.selected is not None


def test_low_conviction_rejects_a_too_close_to_the_money_delta() -> None:
    result = select_contract(_inputs((_quote(delta=0.60),), conviction=0.4))
    assert result.selected is None
    assert result.rejection_reason == "no_delta_in_band"


def test_conviction_threshold_boundary_is_high_band() -> None:
    """0.7 itself is high-conviction (>=), not low."""
    result = select_contract(_inputs((_quote(delta=0.50),), conviction=0.7))
    assert result.selected is not None


def test_missing_delta_fails_the_band_not_a_neutral_pass() -> None:
    result = select_contract(_inputs((_quote(delta=None),), conviction=0.5))
    assert result.selected is None
    assert result.rejection_reason == "no_delta_in_band"


# ─────────────────────────────────────────────────────────────────────
# Liquidity floor — three independent sub-conditions
# ─────────────────────────────────────────────────────────────────────


def test_liquidity_rejects_low_open_interest() -> None:
    result = select_contract(_inputs((_quote(open_interest=99),)))
    assert result.selected is None
    assert result.rejection_reason == "no_liquid_contract"


def test_liquidity_accepts_open_interest_at_the_floor() -> None:
    result = select_contract(_inputs((_quote(open_interest=100),)))
    assert result.selected is not None


def test_liquidity_rejects_low_volume() -> None:
    result = select_contract(_inputs((_quote(volume=9),)))
    assert result.selected is None
    assert result.rejection_reason == "no_liquid_contract"


def test_liquidity_accepts_volume_at_the_floor() -> None:
    result = select_contract(_inputs((_quote(volume=10),)))
    assert result.selected is not None


def test_liquidity_rejects_wide_relative_spread() -> None:
    # mid = 3.00, spread = 0.30 -> 10% > 8% cap
    result = select_contract(_inputs((_quote(bid=2.85, ask=3.15),)))
    assert result.selected is None
    assert result.rejection_reason == "no_liquid_contract"


def test_liquidity_accepts_tight_relative_spread() -> None:
    # mid = 3.00, spread = 0.20 -> 6.67% < 8% cap
    result = select_contract(_inputs((_quote(bid=2.90, ask=3.10),)))
    assert result.selected is not None


def test_liquidity_missing_bid_ask_skips_spread_check_only() -> None:
    """No bid/ask at all -> nothing to compute a spread from, so the
    spread arm is skipped; OI/volume still gate it on their own."""
    result = select_contract(_inputs((_quote(bid=None, ask=None),)))
    assert result.selected is not None

    result = select_contract(
        _inputs((_quote(bid=None, ask=None, open_interest=50),))
    )
    assert result.selected is None
    assert result.rejection_reason == "no_liquid_contract"


def test_liquidity_missing_open_interest_fails_the_floor() -> None:
    result = select_contract(_inputs((_quote(open_interest=None),)))
    assert result.selected is None
    assert result.rejection_reason == "no_liquid_contract"


def test_liquidity_missing_volume_fails_the_floor() -> None:
    result = select_contract(_inputs((_quote(volume=None),)))
    assert result.selected is None
    assert result.rejection_reason == "no_liquid_contract"


# ─────────────────────────────────────────────────────────────────────
# Missing IV — outright rejection, not a neutral pass-through
# ─────────────────────────────────────────────────────────────────────


def test_missing_iv_is_rejected_even_though_everything_else_passes() -> None:
    result = select_contract(_inputs((_quote(implied_volatility=None),)))
    assert result.selected is None
    assert result.rejection_reason == "no_iv"
    assert result.funnel_counts["liquidity"] == 1  # got all the way there
    assert result.funnel_counts["iv_present"] == 0


def test_present_iv_survives() -> None:
    result = select_contract(_inputs((_quote(implied_volatility=0.35),)))
    assert result.selected is not None
    assert result.selected.implied_volatility == 0.35


# ─────────────────────────────────────────────────────────────────────
# Tie-break: tightest relative spread, then highest OI
# ─────────────────────────────────────────────────────────────────────


def test_tie_break_prefers_tighter_spread() -> None:
    tight = _quote(occ_symbol="TIGHT", bid=2.95, ask=3.05)  # mid 3.00, 3.33%
    # A wide-but-still-under-cap spread so both candidates pass the
    # liquidity floor and the tie-break itself is what's being exercised.
    wide_ok = _quote(occ_symbol="WIDE_OK", bid=2.88, ask=3.12)  # mid 3.00, 8.0% exactly at cap
    result = select_contract(_inputs((tight, wide_ok)))
    assert result.selected is not None
    assert result.selected.occ_symbol == "TIGHT"


def test_tie_break_falls_back_to_highest_open_interest() -> None:
    a = _quote(occ_symbol="LOW_OI", bid=2.90, ask=3.10, open_interest=150)
    b = _quote(occ_symbol="HIGH_OI", bid=2.90, ask=3.10, open_interest=800)
    result = select_contract(_inputs((a, b)))
    assert result.selected is not None
    assert result.selected.occ_symbol == "HIGH_OI"


def test_tie_break_prefers_verified_spread_over_unknown_spread() -> None:
    known = _quote(occ_symbol="KNOWN_SPREAD", bid=2.90, ask=3.10, open_interest=100)
    unknown = _quote(occ_symbol="UNKNOWN_SPREAD", bid=None, ask=None, open_interest=100)
    result = select_contract(_inputs((known, unknown)))
    assert result.selected is not None
    assert result.selected.occ_symbol == "KNOWN_SPREAD"


# ─────────────────────────────────────────────────────────────────────
# Empty funnel — named HOLD reasons
# ─────────────────────────────────────────────────────────────────────


def test_no_candidates_at_all_is_a_named_hold() -> None:
    result = select_contract(_inputs(()))
    assert result.selected is None
    assert result.rejection_reason == "no_candidates"
    assert result.funnel_counts == {"total": 0}


def test_funnel_counts_reported_for_a_full_ladder() -> None:
    """One candidate survives every stage -> funnel counts step down to 1
    and stay there (nothing "revives" after being filtered). No
    realized_vol_pct given -> iv_realized_vol_band is a neutral pass,
    same count carried through."""
    result = select_contract(_inputs((_quote(),)))
    assert result.selected is not None
    assert result.funnel_counts == {
        "total": 1,
        "contract_type": 1,
        "dte_window": 1,
        "delta_band": 1,
        "liquidity": 1,
        "iv_present": 1,
        "iv_realized_vol_band": 1,
    }


# ─────────────────────────────────────────────────────────────────────
# iv_realized_vol_band — docs/OPTIONS_PLAN.md §2.2's IV-sanity criterion,
# the half `iv_present` alone doesn't cover.
# ─────────────────────────────────────────────────────────────────────


def test_iv_realized_vol_band_unit_consistency_check() -> None:
    """The landmine, written first: IV is a decimal fraction (0.25),
    realized_vol_pct is already in percent units (25.0) — a ratio of
    exactly 1.0 (fair-priced) must PASS. Getting the unit conversion
    backwards would make this fail and silently re-disable the stage."""
    quote = _quote(implied_volatility=0.25)
    result = select_contract(_inputs((quote,), realized_vol_pct=25.0))
    assert result.selected is not None
    assert result.funnel_counts["iv_realized_vol_band"] == 1


def test_iv_realized_vol_band_rejects_iv_too_rich_vs_realized() -> None:
    """IV at 4.5x realized vol (above the 3.0x ceiling) — buying rich IV
    into a quiet underlying, even on the right directional call."""
    quote = _quote(implied_volatility=0.90)
    result = select_contract(_inputs((quote,), realized_vol_pct=20.0))
    assert result.selected is None
    assert result.rejection_reason == "iv_outside_plausible_band"
    assert result.funnel_counts["iv_realized_vol_band"] == 0


def test_iv_realized_vol_band_rejects_iv_too_cheap_vs_realized() -> None:
    """IV at 0.1x realized vol (below the 0.3x floor)."""
    quote = _quote(implied_volatility=0.03)
    result = select_contract(_inputs((quote,), realized_vol_pct=30.0))
    assert result.selected is None
    assert result.rejection_reason == "iv_outside_plausible_band"


def test_iv_realized_vol_band_none_realized_vol_is_a_neutral_pass() -> None:
    """No realized-vol comparator available -> a fact about the analysis
    environment, not the contract; must not reject (unlike iv_present's
    own stricter handling of a genuinely missing IV)."""
    quote = _quote(implied_volatility=0.90)  # would fail the band if checked
    result = select_contract(_inputs((quote,), realized_vol_pct=None))
    assert result.selected is not None


def test_iv_realized_vol_band_zero_realized_vol_is_a_neutral_pass() -> None:
    """A degenerate realized_vol_pct (<=0) has nothing sane to compare
    against — must not reject on it."""
    quote = _quote(implied_volatility=0.90)
    result = select_contract(_inputs((quote,), realized_vol_pct=0.0))
    assert result.selected is not None


def test_rejection_reason_names_the_first_stage_that_emptied() -> None:
    """A candidate that fails contract_type must report THAT reason, not a
    downstream one it never even reached."""
    only_puts_missing_iv = _quote(contract_type="put", implied_volatility=None)
    result = select_contract(_inputs((only_puts_missing_iv,), direction="long"))
    assert result.selected is None
    assert result.rejection_reason == "no_matching_contract_type"
    assert result.funnel_counts["contract_type"] == 0
    # Everything downstream reads zero too, not "unreached"/absent.
    assert result.funnel_counts["iv_present"] == 0


# ─────────────────────────────────────────────────────────────────────
# days_to_earnings passthrough (not a filter input — see module docstring)
# ─────────────────────────────────────────────────────────────────────


def test_days_to_earnings_is_carried_into_the_selected_leg_unfiltered() -> None:
    result = select_contract(_inputs((_quote(),), days_to_earnings=1))
    assert result.selected is not None
    assert result.selected.days_to_earnings == 1


def test_days_to_earnings_none_does_not_reject() -> None:
    result = select_contract(_inputs((_quote(),), days_to_earnings=None))
    assert result.selected is not None
    assert result.selected.days_to_earnings is None
