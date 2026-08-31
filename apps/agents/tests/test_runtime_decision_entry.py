"""IMPL_REFUSAL_LEDGER.md §0 — the write-side fix.

`runtime._to_decision_entry` persists `raw_state["proposal"]`, which
`PostgresDecisionLog.record()` falls back to whenever `proposal_dto` is
None (i.e. every VETOED-but-drafted decision, since `run_council` only
builds `proposal_dto` when risk approved). Before this fix that fallback
was the Drafter's raw snake_case dict — `estimated_notional`, not
`estimatedNotional` — which is exactly why `ghost_service.build_veto_
ledger`'s `proposal.get("estimatedNotional")` summed to $0 against live
vetoed rows (verified against the real DB: 6/6 `single_name_concentration`
vetoes had `estimated_notional` set and `estimatedNotional` entirely
absent).
"""

from __future__ import annotations

from trading_agents.runtime import _to_decision_entry


def _drafted_proposal(**over: object) -> dict:
    base: dict = {
        "side": "BUY",
        "qty": 16,
        "direction": "long",
        "order_type": "MARKET",
        "estimated_notional": 4922.08,
        "rationale": "thesis",
        "bull_case": "bull",
        "bear_case": "bear",
        "risk_level": 3,
        "conviction_level": 4,
    }
    base.update(over)
    return base


def _final_state(*, risk_approved: bool, proposal: dict) -> dict:
    return {
        "symbol": "NVDA",
        "triggered_at": None,
        "regime": "neutral",
        "analyst_subset": [],
        "degraded_nodes": [],
        "selected_strategy": "momentum",
        "selector_confidence": 0.6,
        "selector_rationale": "",
        "final_action": "VETOED" if not risk_approved else "BUY",
        "risk_approved": risk_approved,
        "risk_veto_rule": None if risk_approved else "single_name_concentration",
        "risk_reason": "" if risk_approved else "single_name_concentration exceeded",
        "technical": None,
        "fundamental": None,
        "macro": None,
        "proposal": proposal,
        "context": {"asset": {"shortable": True, "easy_to_borrow": True}},
        "token_usage": None,
        "bull_case": None,
        "bear_case": None,
    }


def test_vetoed_row_persists_camelcase_notional_not_snake_case() -> None:
    """The exact bug: a vetoed proposal's `raw_state["proposal"]` must be
    the camelCase shape so `ghost_eval`/the veto ledger can read
    `estimatedNotional`. Break this by reverting to `final.get("proposal")`
    untouched (the Drafter's raw snake_case dict)."""
    final = _final_state(risk_approved=False, proposal=_drafted_proposal())

    entry = _to_decision_entry("NVDA", "short", "user-1", final, None, "run-1")

    persisted = entry.raw_state["proposal"]
    assert persisted is not None
    assert persisted.get("estimatedNotional") == 4922.08
    assert persisted.get("side") == "BUY"
    assert persisted.get("qty") == 16


def test_vetoed_row_does_not_become_actionable() -> None:
    """The camelCase normalization must be persistence-only: `proposal_dto`
    (which gates the return value's actionable `"proposal"` key, the
    approval card, and the push notification) must stay None for a veto —
    the risk engine's refusal must never reach the user as something to
    approve."""
    final = _final_state(risk_approved=False, proposal=_drafted_proposal())

    entry = _to_decision_entry("NVDA", "short", "user-1", final, None, "run-1")

    assert entry.proposal_dto is None


def test_approved_row_is_unaffected() -> None:
    """An approved row's `raw_state["proposal"]` must still be exactly the
    (already camelCase) `proposal_dto` it was before this fix — this fix
    only changes the vetoed branch."""
    proposal_dto = {
        "id": "agent-abc123",
        "symbol": "NVDA",
        "side": "BUY",
        "qty": 16,
        "estimatedNotional": 4922.08,
    }
    final = _final_state(risk_approved=True, proposal=_drafted_proposal())

    entry = _to_decision_entry("NVDA", "short", "user-1", final, proposal_dto, "run-1")

    assert entry.raw_state["proposal"] is proposal_dto
    assert entry.proposal_dto is proposal_dto


def test_genuine_hold_persists_no_proposal() -> None:
    """Nothing drafted at all -> `raw_state["proposal"]` must stay None,
    not become `{}` or a synthesized DTO."""
    final = _final_state(risk_approved=False, proposal={})
    final["final_action"] = "HOLD"
    final["risk_veto_rule"] = None

    entry = _to_decision_entry("NVDA", "short", "user-1", final, None, "run-1")

    assert entry.raw_state["proposal"] is None
    assert entry.proposal_dto is None
