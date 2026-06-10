"""engine.risk.evaluate — the canonical pre-trade risk gate.

Rules run in a fixed order. First veto wins.

Ordering rationale (catastrophic / state-level → trim → aggregate exposure
→ informational). Market-specific rules self-gate on the symbol's market
(US = bare symbols, IN = NSE:/BSE:/NFO:/… prefixes — see ``markets.py``):
   1. drawdown_halt              account-level circuit breaker
   2. forbid_short_phase_0       category block before anything else
   3. lot_size_block             [IN] F&O whole-lot validity (may add flag)
   4. min_council_confidence     don't even score a low-conviction trade
   5. min_specialist_avg_score   council disagreement floor
   6. pdt_block                  [US] regulatory hard line
   7. mis_square_off_block       [IN] no new intraday entries near close
   8. max_open_positions         portfolio breadth
   9. position_size_cap          single-trade sizing — may TRIM the qty
  10. derivative_notional_cap    [IN] post-trim contract-notional ceiling
  11. correlation_cap            cluster-level breadth (tighter than sector)
  12. sector_concentration       checked against the (possibly-trimmed) qty
  13. single_name_concentration  checked against the (possibly-trimmed) qty
  14. wash_sale                  [US] INFORMATIONAL — flag only, never blocks

Why trim BEFORE the aggregate-exposure checks? A user submits BUY 80 NVDA;
position-size cap trims to 38 shares; the trimmed proposal has lower
notional, so single-name + sector are evaluated against the smaller number.
This is what users expect — "size me to fit your risk policy" — and it
matches what production should do.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

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
    single_name_concentration,
    wash_sale,
)
from engine.risk.types import (
    RiskCaps,
    RiskContext,
    RiskDecision,
    RiskProposal,
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
    """
    caps = caps or RiskCaps()
    informational: list[str] = []
    working = proposal

    # ── 1. Drawdown circuit breaker ─────────────────────────────────
    d = drawdown_halt(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 2. Forbid short (Phase 0/1 long-only) ───────────────────────
    d = forbid_short_phase_0(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 3. [IN] Lot-size validity — veto on off-lot F&O qty, flag on
    #     unknown underlying. ───────────────────────────────────────
    d = lot_size_block(working, context, caps)
    if d is not None:
        if not d.approved:
            return d
        informational.extend(d.informational_flags)

    # ── 4. Confidence floor ─────────────────────────────────────────
    d = min_council_confidence(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 5. Specialist-average score floor ───────────────────────────
    d = min_specialist_avg_score(working, context, caps, specialists=specialists)
    if d is not None and not d.approved:
        return d

    # ── 6. [US] PDT (regulatory) ────────────────────────────────────
    d = pdt_block(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 7. [IN] MIS square-off window ───────────────────────────────
    d = mis_square_off_block(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 8. Open-positions cap ───────────────────────────────────────
    d = max_open_positions(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 9. Position size — may TRIM. Runs BEFORE aggregate-exposure
    #     rules so they see the (possibly-trimmed) qty. ──────────────
    trim_d = position_size_cap(working, context, caps)
    if trim_d is not None:
        if not trim_d.approved:
            return trim_d
        if trim_d.adjusted_qty is not None and trim_d.adjusted_qty != working.qty:
            informational.append(f"trimmed:{working.qty}->{trim_d.adjusted_qty}")
            working = replace(working, qty=trim_d.adjusted_qty)

    # ── 10. [IN] Derivative contract-notional ceiling (post-trim) ───
    d = derivative_notional_cap(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 11. Correlation cluster (post-trim) — tighter than sector ───
    d = correlation_cap(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 12. Sector concentration (post-trim) ────────────────────────
    d = sector_concentration(working, context, caps)
    if d is not None:
        if not d.approved:
            return d
        informational.extend(d.informational_flags)

    # ── 13. Single-name concentration (post-trim) ───────────────────
    d = single_name_concentration(working, context, caps)
    if d is not None and not d.approved:
        return d

    # ── 14. [US] Wash-sale (informational only — never vetoes) ─────
    ws = wash_sale(working, context, caps)
    if ws is not None and ws.informational_flags:
        informational.extend(ws.informational_flags)

    return RiskDecision(
        approved=True,
        reason="All risk checks passed.",
        adjusted_qty=working.qty if working.qty != proposal.qty else None,
        informational_flags=tuple(informational),
    )
