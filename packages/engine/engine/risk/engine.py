"""engine.risk.evaluate — the canonical pre-trade risk gate.

Rules run in a fixed order. First veto wins.

Ordering rationale (catastrophic / state-level → direction eligibility →
trim → aggregate exposure → informational). Market-specific rules self-gate
on the symbol's market (US = bare symbols, IN = NSE:/BSE:/NFO:/… prefixes —
see ``markets.py``); the short rules self-gate on whether the proposal
actually opens a short (see ``rules/_short.py``):
   1. drawdown_halt              account-level circuit breaker
   2. forbid_short_phase_0       category block before anything else
   3. shortable_check            [SHORT] can we even borrow it
   4. short_requires_stop        [SHORT] no stop leg → no short
   5. lot_size_block             [IN] F&O whole-lot validity (may add flag)
   6. min_council_confidence     don't even score a low-conviction trade
   7. min_specialist_avg_score   council disagreement floor
   8. pdt_block                  [US] regulatory hard line
   9. mis_square_off_block       [IN] no new intraday entries near close
  10. max_open_positions         portfolio breadth
  11. position_size_cap          single LONG sizing — may TRIM the qty
  12. short_unbounded_loss_cap   single SHORT sizing — may TRIM; + book gross
  13. derivative_notional_cap    [IN] post-trim contract-notional ceiling
  14. correlation_cap            cluster-level breadth (tighter than sector)
  15. sector_concentration       checked against the (possibly-trimmed) qty
  16. single_name_concentration  checked against the (possibly-trimmed) qty
  17. wash_sale                  [US] INFORMATIONAL — flag only, never blocks

Why trim BEFORE the aggregate-exposure checks? A user submits BUY 80 NVDA;
position-size cap trims to 38 shares; the trimmed proposal has lower
notional, so single-name + sector are evaluated against the smaller number.
This is what users expect — "size me to fit your risk policy" — and it
matches what production should do.

Why the two short-eligibility rules run so early: both are *categorical*
answers (we cannot borrow it / it has no stop), and there is no point
spending the rest of the chain — or a trim computation — on a trade that
can never be placed. They sit immediately after ``forbid_short_phase_0``
for the same reason it sits second.

**Audit surface.** ``RiskDecision.checks_passed`` names every rule that ran
and did not block. A veto name explains a refusal; the pass list explains
an approval, which is the half a user actually sees. Rules that self-gate
out (an India rule on a US symbol, a short rule on a long) are absent from
the list — they did not run, so they did not pass.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from engine.risk.rules import (
    correlation_cap,
    derivative_notional_cap,
    drawdown_halt,
    forbid_short_phase_0,
    lot_size_block,
    max_open_positions,
    min_council_confidence,
    min_specialist_avg_score,
    mis_square_off_block,
    pdt_block,
    position_size_cap,
    sector_concentration,
    short_requires_stop,
    short_unbounded_loss_cap,
    shortable_check,
    single_name_concentration,
    wash_sale,
)
from engine.risk.rules._short import opens_short
from engine.risk.types import (
    RiskCaps,
    RiskContext,
    RiskDecision,
    RiskProposal,
    Side,
    SpecialistScore,
)


def evaluate(
    proposal: RiskProposal,
    context: RiskContext,
    caps: RiskCaps | None = None,
    *,
    specialists: Iterable[SpecialistScore] = (),
) -> RiskDecision:
    """Run every rule in order. Return the first veto, or ``approved=True``
    (potentially with an adjusted_qty) if every rule passes.

    ``caps=None`` resolves to ``RiskCaps.from_env()``, NOT bare
    ``RiskCaps()``. This matters end-to-end: the executor re-runs this
    function at order-placement time and passes ``caps=None``, so a bare
    default would veto — with ``forbid_short_phase_0`` — a short the
    council had already approved under ALLOW_SHORTS. One switch, honored
    at every call site, is the only version of this that cannot half-apply.
    ``from_env`` still fails closed on an unset or unrecognised value.
    """
    caps = caps or RiskCaps.from_env()
    informational: list[str] = []
    passed: list[str] = []
    working = proposal

    # ── 1. Drawdown circuit breaker ─────────────────────────────────
    d = drawdown_halt(working, context, caps)
    if d is not None and not d.approved:
        return d
    if d is None:
        passed.append("drawdown_halt")

    # ── 2. Forbid short (long-only unless ALLOW_SHORTS) ─────────────
    d = forbid_short_phase_0(working, context, caps)
    if d is not None and not d.approved:
        return d
    if d is None and caps.forbid_short_phase_0:
        passed.append("forbid_short_phase_0")

    # ── 3. [SHORT] Borrow eligibility ───────────────────────────────
    d = shortable_check(working, context, caps)
    if d is not None:
        if not d.approved:
            return d
    else:
        _note_if_short(working, context, passed, "shortable_check")

    # ── 4. [SHORT] Protective stop leg ──────────────────────────────
    d = short_requires_stop(working, context, caps)
    if d is not None and not d.approved:
        return d
    if d is None:
        _note_if_short(working, context, passed, "short_requires_stop")

    # ── 5. [IN] Lot-size validity — veto on off-lot F&O qty, flag on
    #     unknown underlying. ───────────────────────────────────────
    d = lot_size_block(working, context, caps)
    if d is not None:
        if not d.approved:
            return d
        informational.extend(d.informational_flags)
        passed.append("lot_size_block")

    # ── 6. Confidence floor ─────────────────────────────────────────
    d = min_council_confidence(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("min_council_confidence")

    # ── 7. Specialist-average score floor ───────────────────────────
    d = min_specialist_avg_score(working, context, caps, specialists=specialists)
    if d is not None and not d.approved:
        return d
    passed.append("min_specialist_avg_score")

    # ── 8. [US] PDT (regulatory) ────────────────────────────────────
    d = pdt_block(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("pdt_block")

    # ── 9. [IN] MIS square-off window ───────────────────────────────
    d = mis_square_off_block(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 10. Open-positions cap ──────────────────────────────────────
    d = max_open_positions(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("max_open_positions")

    # ── 11. LONG position size — may TRIM. Runs BEFORE aggregate-
    #     exposure rules so they see the (possibly-trimmed) qty. ─────
    trim_d = position_size_cap(working, context, caps)
    if trim_d is not None:
        if not trim_d.approved:
            return trim_d
        if trim_d.adjusted_qty is not None and trim_d.adjusted_qty != working.qty:
            informational.append(f"trimmed:{working.qty}->{trim_d.adjusted_qty}")
            working = replace(working, qty=trim_d.adjusted_qty)
    if working.side is Side.BUY:
        passed.append("max_position_pct")

    # ── 12. SHORT position size + book-wide gross — may TRIM. ───────
    short_d = short_unbounded_loss_cap(working, context, caps)
    if short_d is not None:
        if not short_d.approved:
            return short_d
        if short_d.adjusted_qty is not None and short_d.adjusted_qty != working.qty:
            informational.append(f"trimmed:{working.qty}->{short_d.adjusted_qty}")
            working = replace(working, qty=short_d.adjusted_qty)
    _note_if_short(working, context, passed, "short_unbounded_loss_cap")

    # ── 13. [IN] Derivative contract-notional ceiling (post-trim) ───
    d = derivative_notional_cap(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 14. Correlation cluster (post-trim) — tighter than sector ───
    d = correlation_cap(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("correlation_cap")

    # ── 15. Sector concentration (post-trim) ────────────────────────
    d = sector_concentration(working, context, caps)
    if d is not None:
        if not d.approved:
            return d
        informational.extend(d.informational_flags)
    passed.append("max_sector_pct")

    # ── 16. Single-name concentration (post-trim) ───────────────────
    d = single_name_concentration(working, context, caps)
    if d is not None and not d.approved:
        return d
    passed.append("max_single_name_pct")

    # ── 17. [US] Wash-sale (informational only — never vetoes) ─────
    ws = wash_sale(working, context, caps)
    if ws is not None and ws.informational_flags:
        informational.extend(ws.informational_flags)

    return RiskDecision(
        approved=True,
        reason="All risk checks passed.",
        adjusted_qty=working.qty if working.qty != proposal.qty else None,
        informational_flags=tuple(informational),
        checks_passed=tuple(passed),
    )


def _note_if_short(
    proposal: RiskProposal, context: RiskContext, passed: list[str], name: str
) -> None:
    """Record a short-side pass only when the rule actually applied.

    Listing ``shortable_check`` as "passed" on a long BUY would be a lie by
    omission — the rule never looked at anything.
    """
    if opens_short(proposal, context):
        passed.append(name)
