"""max_premium_pct — the options analogue of ``position_size_cap``.

Entry-only. Mirrors ``engine.risk.rules.position_size.position_size_cap``'s
exact trim-vs-veto shape: premium-at-risk for this ONE position, as a % of
equity; TRIM the qty down to fit rather than reject outright — but if
even 1 contract exceeds the cap, veto (a contract is a whole-unit
instrument; there is no partial-contract trim). Never blocks a close —
selling to close only reduces risk.

Premium-at-risk is derived from ``proposal.last_price`` (the per-contract
premium) x ``option.multiplier``, NOT from ``proposal.estimated_notional``:
``last_price``/``option`` are the fields every other options rule
re-verifies fresh at each risk-check (see ``OptionLegDetails``'s own
docstring on why ``dte`` is never precomputed) — trusting a
possibly-stale, draft-time-computed aggregate here would undercut that
same freshness guarantee for the one number that decides how much capital
is actually at risk.

The trim floor is hardcoded to 1 contract, not ``caps.min_qty`` — that
cap is a general equity-sizing knob unrelated to "a contract is an
indivisible unit," and coupling the two would let an unrelated equity
tuning change silently move the options floor.

veto_rule (trim):  max_premium_pct_trim
veto_rule (block): max_premium_pct
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def max_premium_pct(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    if context.account_equity <= 0:
        return RiskDecision(
            approved=False,
            reason="Account equity is non-positive; refusing any new options BUY.",
            veto_rule="max_premium_pct",
        )

    premium_per_contract = proposal.last_price * option.multiplier
    if premium_per_contract <= 0:
        return RiskDecision(
            approved=False,
            reason=(
                f"{option.occ_symbol} has no usable premium "
                f"(last_price={proposal.last_price!r}) to size against."
            ),
            veto_rule="max_premium_pct",
        )

    notional = proposal.qty * premium_per_contract
    pct = (notional / context.account_equity) * 100.0
    if pct <= caps.options_max_premium_pct:
        return None

    adjusted = int(
        (caps.options_max_premium_pct / 100.0)
        * context.account_equity
        / premium_per_contract
    )
    if adjusted < 1:
        return RiskDecision(
            approved=False,
            reason=(
                f"Premium would be {pct:.2f}% of equity (cap "
                f"{caps.options_max_premium_pct:.2f}%); trimming rounds to "
                "0 contracts."
            ),
            veto_rule="max_premium_pct",
        )
    return RiskDecision(
        approved=True,
        reason=(
            f"Trimmed {proposal.qty} → {adjusted} contract(s) "
            f"({pct:.2f}% → {caps.options_max_premium_pct:.2f}% of equity)."
        ),
        adjusted_qty=adjusted,
        veto_rule="max_premium_pct_trim",
    )
