"""evaluate_option — the options risk-pipeline orchestrator.

Dispatched to from ``engine.risk.engine.evaluate`` the instant
``proposal.is_option`` is True (see that module's step 1.5 — a full
early-return, so an options proposal never reaches any equity-only rule).
Runs its OWN sequence:

    options_disabled
    naked_short_forbidden
    options_level_insufficient
    expiry_day_entry
    min_dte
    max_dte
    illiquid_contract
    iv_unavailable
    earnings_blackout
    min_council_confidence        ← reused, unmodified
    min_specialist_avg_score      ← reused, unmodified
    pdt_block                     ← reused, unmodified
    max_open_positions            ← reused, unmodified
    max_premium_pct               ← may TRIM
    max_total_premium_pct         ← post-trim
    wash_sale                     ← reused, unmodified, INFORMATIONAL only

The five reused rules need zero modification: each already self-gates
correctly on ``proposal.side``/``market_of(proposal.symbol)`` — none of
them branch on anything equity-specific — so they behave identically here
as they do for a plain equity BUY/SELL.

Same return contract as ``engine.risk.engine.evaluate``: first veto wins;
otherwise ``RiskDecision(approved=True, ...)`` with the accumulated
``checks_passed``/``informational_flags``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from engine.options.rules import (
    earnings_blackout,
    expiry_day_entry,
    illiquid_contract,
    iv_unavailable,
    max_dte,
    max_premium_pct,
    max_total_premium_pct,
    min_dte,
    naked_short_forbidden,
    options_disabled,
    options_level_insufficient,
)
from engine.risk.rules import (
    max_open_positions,
    min_council_confidence,
    min_specialist_avg_score,
    pdt_block,
    wash_sale,
)
from engine.risk.types import (
    RiskCaps,
    RiskContext,
    RiskDecision,
    RiskProposal,
    SpecialistScore,
)


def evaluate_option(
    proposal: RiskProposal,
    context: RiskContext,
    caps: RiskCaps | None = None,
    *,
    specialists: Iterable[SpecialistScore] = (),
) -> RiskDecision:
    """Run every options rule in order. Return the first veto, or
    ``approved=True`` (potentially with an adjusted_qty) if every rule
    passes. ``caps=None`` resolves to ``RiskCaps.from_env()``, same
    contract as ``engine.risk.engine.evaluate``.
    """
    caps = caps or RiskCaps.from_env()
    informational: list[str] = []
    passed: list[str] = []
    trims: list[str] = []
    working = proposal

    if working.option is None:
        # Structurally unreachable via the dispatch in engine.risk.engine
        # (which only diverts here when is_option is True — and every
        # sanctioned constructor, engine.options.contracts.to_risk_proposal,
        # always sets both together) — but RiskProposal.option is nullable
        # at the type level, so a hand-built proposal COULD reach here
        # malformed. Fail closed rather than let every rule below re-derive
        # its own None-guard for a case that should never happen.
        return RiskDecision(
            approved=False,
            reason="Options proposal is missing option leg details — refusing rather than guessing.",
            veto_rule="options_malformed_proposal",
        )

    # ── 1. Master switch (entry-only) ────────────────────────────────
    d = options_disabled(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "options_disabled")

    # ── 2. Action-literal defense-in-depth (unconditional) ───────────
    d = naked_short_forbidden(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("naked_short_forbidden")

    # ── 3. Broker trading-level gate (entry-only) ────────────────────
    d = options_level_insufficient(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "options_level_insufficient")

    # ── 4. No new position expiring today (entry-only) ──────────────
    d = expiry_day_entry(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "expiry_day_entry")

    # ── 5. DTE floor (entry-only) ─────────────────────────────────────
    d = min_dte(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "min_dte")

    # ── 6. DTE ceiling (entry-only) ────────────────────────────────────
    d = max_dte(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "max_dte")

    # ── 7. Liquidity floor (entry-only) ──────────────────────────────
    d = illiquid_contract(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "illiquid_contract")

    # ── 8. IV must be priceable (entry-only) ─────────────────────────
    d = iv_unavailable(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "iv_unavailable")

    # ── 9. Earnings blackout (entry-only; self-gates on missing data) ──
    d = earnings_blackout(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "earnings_blackout")

    # ── 10. Confidence floor (reused, unmodified) ────────────────────
    d = min_council_confidence(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("min_council_confidence")

    # ── 11. Specialist-average score floor (reused, unmodified) ──────
    d = min_specialist_avg_score(working, context, caps, specialists=specialists)
    if d is not None and not d.approved:
        return d
    passed.append("min_specialist_avg_score")

    # ── 12. PDT (reused, unmodified) ─────────────────────────────────
    d = pdt_block(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("pdt_block")

    # ── 13. Open-positions breadth (reused, unmodified) ──────────────
    d = max_open_positions(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("max_open_positions")

    # ── 14. Single-position premium-at-risk — may TRIM (entry-only) ──
    trim_d = max_premium_pct(working, context, caps)
    if trim_d is not None:
        if not trim_d.approved:
            return trim_d
        if trim_d.adjusted_qty is not None and trim_d.adjusted_qty != working.qty:
            informational.append(f"trimmed:{working.qty}->{trim_d.adjusted_qty}")
            # The rule's OWN name, not the anonymous flag above — a trim is
            # a partial refusal and the Refusal Ledger needs to attribute it.
            trims.append(trim_d.veto_rule or "max_premium_pct_trim")
            working = replace(working, qty=trim_d.adjusted_qty)
    _note_if_entry(working, passed, "max_premium_pct")

    # ── 15. Portfolio-aggregate premium (post-trim; entry-only) ──────
    d = max_total_premium_pct(working, context, caps)
    if d is not None and not d.approved:
        return d
    _note_if_entry(working, passed, "max_total_premium_pct")

    # ── 16. Wash-sale (reused, unmodified — INFORMATIONAL only) ──────
    ws = wash_sale(working, context, caps)
    if ws is not None and ws.informational_flags:
        informational.extend(ws.informational_flags)

    return RiskDecision(
        approved=True,
        reason="All options risk checks passed.",
        adjusted_qty=working.qty if working.qty != proposal.qty else None,
        informational_flags=tuple(informational),
        checks_passed=tuple(passed),
        trim_rules=tuple(trims),
    )


def _note_if_entry(proposal: RiskProposal, passed: list[str], name: str) -> None:
    """Record an entry-only rule's pass only when it actually applied (a
    buy_to_open) — mirrors ``engine.risk.engine._note_if_short``'s same
    principle for the short-side rules: a self-gated-out rule did not run,
    so it did not pass, and listing it anyway would be a lie by omission.
    """
    if proposal.option is not None and proposal.option.action == "buy_to_open":
        passed.append(name)
