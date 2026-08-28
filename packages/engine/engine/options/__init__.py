"""Options trading Phase A (long calls/puts only — see docs/OPTIONS_PLAN.md
and CLAUDE.md's v1 scope table).

Public surface:
    evaluate_option(proposal, context, caps, *, specialists=()) -> RiskDecision
    to_risk_proposal(*, symbol, side, qty, ..., option) -> RiskProposal
    dte(expiry, now) -> int
    is_expiry_day(expiry, now) -> bool
    fetch_option_candidates(underlying_symbol, *, api_key, secret_key, now) ->
        tuple[ContractQuote, ...]
    select_contract(inputs: ContractSelectionInputs) -> ContractSelectionResult
    options_position_size(inputs: OptionsSizingInputs) -> OptionsSizingDecision

Architecture rule (CLAUDE.md): agents propose, deterministic code
disposes. Nothing in this package is LLM-driven; every rule is a pure,
named, deterministic function, exactly like ``engine.risk.rules``.

Scope split, two parallel tracks that both landed here:
    engine.options.{contracts,expiry,greeks,risk,rules}   risk/execution-
        safety (``to_risk_proposal``, ``evaluate_option``, ``dte``, the
        11 named veto rules).
    engine.options.{selection,sizing}   contract/strike/expiry selection +
        premium-at-risk sizing (``select_contract``, ``options_position_size``).
``contracts.fetch_option_candidates`` ORCHESTRATES chain fetching (merges
two ``packages/broker`` calls into the ``ContractQuote`` shape
``selection.select_contract`` needs) but does not itself talk to Alpaca —
the alpaca-py-typed calls stay in ``packages/broker``, matching this
workspace's ``agents -> engine -> broker -> alpaca-py`` layering.
Execution still lives in ``apps/api/app/services/orders``.
"""

from __future__ import annotations

from engine.options.contracts import (
    OccSymbol,
    contract_type_of,
    fetch_option_candidates,
    to_risk_proposal,
)
from engine.options.expiry import dte, is_expiry_day
from engine.options.risk import evaluate_option
from engine.options.selection import (
    ContractQuote,
    ContractSelectionInputs,
    ContractSelectionResult,
    select_contract,
)
from engine.options.sizing import (
    OptionsSizingDecision,
    OptionsSizingInputs,
    options_position_size,
)

__all__ = [
    "ContractQuote",
    "ContractSelectionInputs",
    "ContractSelectionResult",
    "OccSymbol",
    "OptionsSizingDecision",
    "OptionsSizingInputs",
    "contract_type_of",
    "dte",
    "evaluate_option",
    "fetch_option_candidates",
    "is_expiry_day",
    "options_position_size",
    "select_contract",
    "to_risk_proposal",
]
