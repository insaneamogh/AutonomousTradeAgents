"""``RiskCaps.aggressive_paper()`` + ``RISK_PROFILE`` dispatch.

docs/PLAN_AGGRESSIVE_PROFILE.md §3: the env var selects between two
REVIEWED, in-git, diffable profiles — it never supplies a widened number
directly. These tests pin that contract, plus the two numbers that must
never move regardless of profile (the drawdown halt and the equity
position cap) — see that plan's §2 "the one that must not move" and
§7's revert-check matrix.

Pure-logic — no DB, no LLM, no network.
"""

from __future__ import annotations

import pytest

from engine.risk import RiskCaps

# ─────────────────────────────────────────────────────────────────────
# The profile itself — diffable against the conservative default
# ─────────────────────────────────────────────────────────────────────


def test_aggressive_profile_widens_the_options_premium_caps() -> None:
    caps = RiskCaps.aggressive_paper()
    assert caps.options_max_premium_pct == pytest.approx(2.5)
    assert caps.options_max_total_premium_pct == pytest.approx(7.5)


def test_aggressive_profile_widens_the_confidence_floors() -> None:
    caps = RiskCaps.aggressive_paper()
    assert caps.min_council_confidence == pytest.approx(0.42)
    assert caps.min_specialist_avg_score == pytest.approx(40.0)


def test_aggressive_profile_tightens_the_options_stop_loss() -> None:
    """ "Cut losers early" — the stop tightens even though the caps widen."""
    caps = RiskCaps.aggressive_paper()
    assert caps.options_stop_loss_pct == pytest.approx(40.0)


def test_aggressive_profile_widens_the_correlation_cluster_cap() -> None:
    caps = RiskCaps.aggressive_paper()
    assert caps.max_correlation_cluster == 4


def test_aggressive_profile_leaves_the_drawdown_halt_alone() -> None:
    """The single most load-bearing invariant in the whole plan: widening
    the options premium cap and holding this halt fixed is ONE coupled
    decision. If this ever moves, the "12% book-to-zero is a multi-day
    worst case" argument breaks."""
    assert RiskCaps.aggressive_paper().daily_drawdown_halt_pct == pytest.approx(
        RiskCaps().daily_drawdown_halt_pct
    )
    assert RiskCaps.aggressive_paper().daily_drawdown_halt_pct == pytest.approx(-3.0)


def test_aggressive_profile_leaves_max_position_pct_alone() -> None:
    """The aggression is concentrated in the options caps, where loss is
    bounded by construction (the premium). The equity cap does not move."""
    assert RiskCaps.aggressive_paper().max_position_pct == pytest.approx(
        RiskCaps().max_position_pct
    )
    assert RiskCaps.aggressive_paper().max_position_pct == pytest.approx(5.0)


def test_aggressive_profile_still_honors_explicit_overrides() -> None:
    """``**overrides`` must still work on the new classmethod, same as the
    conservative constructor — callers (tests, a future profile variant)
    can still pin an exact value on top."""
    caps = RiskCaps.aggressive_paper(options_max_premium_pct=9.0)
    assert caps.options_max_premium_pct == pytest.approx(9.0)


# ─────────────────────────────────────────────────────────────────────
# RISK_PROFILE dispatch — a profile choice, never a number
# ─────────────────────────────────────────────────────────────────────


def test_risk_profile_env_selects_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PROFILE", "aggressive_paper")
    caps = RiskCaps.from_env()
    assert caps.options_max_premium_pct == pytest.approx(2.5)
    assert caps.options_max_total_premium_pct == pytest.approx(7.5)
    assert caps.min_council_confidence == pytest.approx(0.42)
    # The coupled invariant must hold via from_env() too, not just the
    # bare classmethod.
    assert caps.daily_drawdown_halt_pct == pytest.approx(-3.0)
    assert caps.max_position_pct == pytest.approx(5.0)


def test_risk_profile_unset_stays_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RISK_PROFILE", raising=False)
    caps = RiskCaps.from_env()
    assert caps.options_max_premium_pct == pytest.approx(1.0)
    assert caps.options_max_total_premium_pct == pytest.approx(5.0)


def test_unknown_risk_profile_falls_back_to_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd Railway variable must never silently select the wider
    profile — same fail-to-default contract ``_env_int``/``_env_float``
    already use for a malformed numeric override."""
    monkeypatch.setenv("RISK_PROFILE", "aggressive-paper")  # wrong separator
    caps = RiskCaps.from_env()
    assert caps.options_max_premium_pct == pytest.approx(1.0)
    assert caps.options_max_total_premium_pct == pytest.approx(5.0)
    assert caps.min_council_confidence == pytest.approx(0.50)


def test_risk_profile_value_is_trimmed_and_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RISK_PROFILE", "  Aggressive_Paper  ")
    caps = RiskCaps.from_env()
    assert caps.options_max_premium_pct == pytest.approx(2.5)


def test_aggressive_profile_env_data_quality_floors_still_apply_on_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The data-quality env floors (OI/volume/spread/exit thresholds) must
    keep applying ON TOP of whichever profile is selected — an explicit
    env var still wins over either profile's own default."""
    monkeypatch.setenv("RISK_PROFILE", "aggressive_paper")
    monkeypatch.setenv("OPTIONS_STOP_LOSS_PCT", "33")
    caps = RiskCaps.from_env()
    assert caps.options_stop_loss_pct == pytest.approx(33.0)
    # Untouched fields still come from the aggressive profile.
    assert caps.options_max_premium_pct == pytest.approx(2.5)


# ─────────────────────────────────────────────────────────────────────
# The halt coupling
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,caps",
    [("conservative", RiskCaps()), ("aggressive_paper", RiskCaps.aggressive_paper())],
)
def test_every_reviewed_profile_respects_the_halt_coupling(name: str, caps: RiskCaps) -> None:
    """A full options book falling to its stop must not be able to lose
    more in one session than the daily halt allows.

    This exists because the argument for widening the premium cap was made
    twice and was wrong both times: "the -3% halt bounds the single-day
    loss whatever the book's size". It does not. `drawdown_halt` blocks
    new ENTRIES and closes nothing — `position_manager` keeps closes legal
    under a halt precisely because the halt de-risks nothing.

    Live, 2026-09-01: six long calls at 11.45% of equity against a -40%
    stop. They gapped overnight, the breaker tripped 18 seconds after the
    bell at -3.56%, the account settled -3.67%, and NOT ONE stop fired —
    every position sat between -20% and -33%. 11.45% x 40% = -4.58% was
    always reachable before the first stop could trigger.

    An intraday stop cannot act on an overnight gap. The only thing that
    bounds a gap is how big the book was allowed to get.
    """
    assert caps.max_options_book_drawdown_pct <= abs(caps.daily_drawdown_halt_pct), (
        f"{name}: options book can lose "
        f"{caps.max_options_book_drawdown_pct:.2f}% of equity before any stop fires, "
        f"past a {caps.daily_drawdown_halt_pct:.2f}% halt. Lower "
        f"options_max_total_premium_pct or options_stop_loss_pct — never the halt."
    )
    assert caps.respects_halt_coupling


def test_the_widened_profile_is_sized_off_the_invariant_not_off_appetite() -> None:
    """7.5% is |halt| / stop, exactly — not a number someone liked."""
    caps = RiskCaps.aggressive_paper()
    implied = abs(caps.daily_drawdown_halt_pct) / (caps.options_stop_loss_pct / 100.0)
    assert caps.options_max_total_premium_pct == pytest.approx(implied)


def test_the_invariant_actually_rejects_the_configuration_that_failed() -> None:
    """The two values that shipped before this test existed. If the
    property cannot fail, it is not an invariant."""
    for bad_total in (12.0, 18.0):
        bad = RiskCaps.aggressive_paper(options_max_total_premium_pct=bad_total)
        assert not bad.respects_halt_coupling
