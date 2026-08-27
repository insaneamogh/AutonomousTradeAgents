"""The activity feed used to call every non-approved decision "vetoed" —
including a strategy-fit HOLD that never reached the risk officer at
all, defaulting its side to "BUY" along the way. A HOLD read as an
unnamed BUY that got a risk rule fired against it, which never happened.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.council.postgres_store import _decision_to_activity


def _row(**overrides: object) -> SimpleNamespace:
    base = dict(
        id="dec-1",
        proposal=None,
        user_response=None,
        risk_approved=False,
        risk_veto_rule=None,
        selected_strategy=None,
        symbol="NVDA",
        user_responded_at=None,
        completed_at=None,
        triggered_at="2026-08-27T00:00:00Z",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_strategy_fit_hold_is_labeled_hold_not_vetoed() -> None:
    entry = _decision_to_activity(_row())
    assert entry.kind == "hold"
    assert entry.side is None
    assert "no strategy fit" in entry.headline


def test_drafter_hold_names_the_strategy_that_fit() -> None:
    entry = _decision_to_activity(_row(selected_strategy="momentum"))
    assert entry.kind == "hold"
    assert "momentum fit, drafter declined" in entry.headline


def test_a_real_risk_veto_is_still_labeled_vetoed() -> None:
    entry = _decision_to_activity(
        _row(
            proposal={"side": "BUY", "qty": 10},
            risk_veto_rule="pdt_block",
        )
    )
    assert entry.kind == "vetoed"
    assert entry.headline == "Vetoed — pdt_block."
    assert entry.side == "BUY"


def test_approved_proposal_is_unchanged() -> None:
    entry = _decision_to_activity(
        _row(proposal={"side": "BUY", "qty": 10}, user_response="approved")
    )
    assert entry.kind == "approved"
    assert entry.side == "BUY"
    assert entry.qty == 10
