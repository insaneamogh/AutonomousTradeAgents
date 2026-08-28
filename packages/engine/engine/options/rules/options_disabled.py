"""options_disabled — the master switch for the whole options pipeline.

Mirrors ``engine.risk.rules.forbid_short.forbid_short_phase_0``: default ON
(blocking), flipped off only by ``ALLOW_OPTIONS=1`` via
``RiskCaps.from_env`` — nothing else turns options on. Entry-only: a
``sell_to_close`` is always allowed even if the flag turns off after
entry, the same "de-risking is not the thing being gated" carve-out
``forbid_short_phase_0`` uses for a SELL that closes a held long.

veto_rule: options_disabled
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def options_disabled(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    if not caps.options_disabled:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            "Options trading is disabled (ALLOW_OPTIONS is off). Refusing "
            f"the new {option.contract_type} position on "
            f"{option.underlying_symbol} ({option.occ_symbol})."
        ),
        veto_rule="options_disabled",
    )
