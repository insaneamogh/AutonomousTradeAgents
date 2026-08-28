"""Options trading Phase A (long calls/puts only — see docs/OPTIONS_PLAN.md
and CLAUDE.md's v1 scope table).

Public surface:
    evaluate_option(proposal, context, caps, *, specialists=()) -> RiskDecision
    to_risk_proposal(*, symbol, side, qty, ..., option) -> RiskProposal
    dte(expiry, now) -> int
    is_expiry_day(expiry, now) -> bool

Not built here — a separate, parallel track owns these:
    engine.options.selection   contract/strike/expiry selection algorithm
    engine.options.sizing      premium-at-risk sizing

Architecture rule (CLAUDE.md): agents propose, deterministic code
disposes. Nothing in this package is LLM-driven; every rule is a pure,
named, deterministic function, exactly like ``engine.risk.rules``.
"""

from __future__ import annotations

from engine.options.contracts import OccSymbol, contract_type_of, to_risk_proposal
from engine.options.expiry import dte, is_expiry_day
from engine.options.risk import evaluate_option

__all__ = [
    "OccSymbol",
    "contract_type_of",
    "dte",
    "evaluate_option",
    "is_expiry_day",
    "to_risk_proposal",
]
