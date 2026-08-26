"""forbid_short_phase_0 — the master switch for the short side.

Blocks any SELL that would open (or extend) a short. Long-only is still
the DEFAULT: ``RiskCaps.forbid_short_phase_0`` is True unless a caller
built its caps with ``RiskCaps.from_env()`` in an environment where
``ALLOW_SHORTS`` is truthy. Nothing else turns shorts on.

A SELL that closes a held long is always allowed, whichever way the switch
is set — reducing exposure is not the thing being gated. The two cases are
told apart by ``rules/_short.opens_short``, which also treats a SELL that
crosses *through* flat (sell 150 against a held 100) as opening a short,
because it does.

When the switch is OFF this rule is the only short control that runs. When
it is ON, three more do: ``shortable_check`` (can we borrow it),
``short_requires_stop`` (is the downside bounded at all), and
``short_unbounded_loss_cap`` (is it small enough that an unbounded loss
still cannot end the account).

veto_rule: forbid_short_phase_0
"""

from __future__ import annotations

from engine.risk.rules._short import held_long_qty, opens_short
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def forbid_short_phase_0(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if not caps.forbid_short_phase_0:
        return None
    if not opens_short(proposal, context):
        return None

    held = held_long_qty(proposal, context)
    detail = (
        "with no held long position"
        if held == 0
        else f"— only {held} share(s) held, so {proposal.qty - held} would go short"
    )
    return RiskDecision(
        approved=False,
        reason=(
            f"Shorts are disabled (ALLOW_SHORTS is off). Refusing SELL "
            f"{proposal.qty} {proposal.symbol} {detail}."
        ),
        veto_rule="forbid_short_phase_0",
    )
