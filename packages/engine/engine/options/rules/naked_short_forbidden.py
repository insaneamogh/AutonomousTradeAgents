"""naked_short_forbidden — unconditional defense-in-depth against any
option action other than the two Phase A permits.

``OptionLegDetails.action``'s ``Literal["buy_to_open", "sell_to_close"]``
already restricts this at the type level, and
``ApprovalProposalDto.option_action`` restricts it again at the API
boundary (a free 422 before ``engine.risk.evaluate`` ever runs) — but
``engine.risk.types`` is deliberately Pydantic-free and gets no validation
of its own at this boundary, so neither upstream restriction is
guaranteed to have run before a ``RiskProposal`` reaches here.

Unlike every other rule in this package, this one has NO entry/close
self-gate: it runs on every options proposal, unconditionally, because its
whole job is validating the action value itself, not gating on it.

veto_rule: naked_short_forbidden
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal

_ALLOWED_ACTIONS = frozenset({"buy_to_open", "sell_to_close"})


def naked_short_forbidden(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    action = proposal.option.action
    if action in _ALLOWED_ACTIONS:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"Option action {action!r} is not permitted in Phase A — only "
            "buy_to_open (long entry) and sell_to_close (closing a long) "
            "are ever constructed. Refused unconditionally, independent of "
            "any upstream validation."
        ),
        veto_rule="naked_short_forbidden",
    )
