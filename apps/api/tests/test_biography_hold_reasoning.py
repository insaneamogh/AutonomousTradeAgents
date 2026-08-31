"""Three genuinely different HOLDs used to render as one indistinguishable
"Council proposed HOLD X" with an empty detail — a strategy-fit HOLD (the
council never ran), a Drafter-verdict HOLD (it ran and said no), and a
parse-failure HOLD, are now told apart.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.council.biography_service import (
    _proposal_estimated_notional,
    _proposed_or_held_summary,
)


def _row(**overrides: object) -> SimpleNamespace:
    base = dict(
        symbol="NVDA",
        final_action="HOLD",
        selected_strategy=None,
        selector_rationale="",
        reasoning=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_no_strategy_fit_hold_says_no_setup_matched() -> None:
    title, detail = _proposed_or_held_summary(_row(), proposal={}, side=None)
    assert "no strategy fit" in title
    assert "no registered strategy" in detail.lower()


def test_no_strategy_fit_hold_prefers_the_selector_rationale_when_present() -> None:
    row = _row(selector_rationale="watchlist scan found nothing tradable today")
    _title, detail = _proposed_or_held_summary(row, proposal={}, side=None)
    assert detail == "watchlist scan found nothing tradable today"


def test_drafter_verdict_hold_surfaces_its_own_rationale() -> None:
    row = _row(
        selected_strategy="momentum",
        reasoning={"drafter_rationale": "Conviction too thin to size despite a real setup."},
    )
    title, detail = _proposed_or_held_summary(row, proposal={}, side=None)
    assert "momentum fit, but the drafter said no" in title
    assert "too thin to size" in detail


def test_strategy_fit_with_no_drafter_rationale_reads_as_a_parse_failure() -> None:
    row = _row(selected_strategy="sma_crossover", reasoning={})
    title, _detail = _proposed_or_held_summary(row, proposal={}, side=None)
    assert "failed to parse" in title


def test_a_real_proposal_is_unchanged() -> None:
    row = _row(final_action="BUY")
    proposal = {"side": "BUY", "rationale": "Confirmed uptrend, strong relative strength."}
    title, detail = _proposed_or_held_summary(row, proposal, side="BUY")
    assert title == "Council proposed BUY NVDA"
    assert detail == "Confirmed uptrend, strong relative strength."


def test_a_stale_raw_state_envelope_is_treated_as_no_proposal() -> None:
    """Historical rows written before the postgres.py fix can still have
    the raw_state ENVELOPE in ``proposal`` — a non-empty dict with no
    ``side`` key. Must read as a HOLD, not silently render an empty
    "Council proposed HOLD X"."""
    row = _row(selector_rationale="no registered strategy cleared the floor")
    envelope = {"regime": "choppy", "proposal": None, "analyst_subset": [], "degraded_nodes": []}
    title, detail = _proposed_or_held_summary(row, envelope, side=None)
    assert "no strategy fit" in title
    assert detail == "no registered strategy cleared the floor"


def test_estimated_notional_reads_the_camel_case_key() -> None:
    """The normal case, post the ``927dc415`` write-side fix: a fresh
    proposal (approved or vetoed) is always the camelCase DTO shape."""
    assert _proposal_estimated_notional({"estimatedNotional": 4922.08}) == 4922.08


def test_estimated_notional_falls_back_to_the_snake_case_key() -> None:
    """The 6 rows vetoed before the ``927dc415`` fix landed still hold the
    Drafter's raw snake_case dict (``estimated_notional``), not the
    camelCase DTO shape (``estimatedNotional``) — this must not regress to
    showing a blank notional on their biography timeline forever."""
    assert _proposal_estimated_notional({"estimated_notional": 4922.08}) == 4922.08


def test_estimated_notional_prefers_camel_case_when_both_present() -> None:
    assert _proposal_estimated_notional({"estimatedNotional": 1.0, "estimated_notional": 2.0}) == 1.0


def test_estimated_notional_is_none_when_neither_key_present() -> None:
    assert _proposal_estimated_notional({}) is None
