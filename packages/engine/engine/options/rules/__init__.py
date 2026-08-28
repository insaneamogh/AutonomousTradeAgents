"""Options risk rules — one file per rule, same shape as
``engine.risk.rules``: a pure function ``(proposal, context, caps) ->
RiskDecision | None``, ``None`` when the rule doesn't apply, a
``RiskDecision`` with a named ``veto_rule`` when it blocks.

Ordering matters in ``engine.options.risk.evaluate_option`` — see that
module for the canonical sequence and for which five equity rules it
reuses unmodified (``min_council_confidence``, ``min_specialist_avg_score``,
``pdt_block``, ``max_open_positions``, ``wash_sale``).

Every rule here except ``naked_short_forbidden`` is ENTRY-ONLY: it
self-gates (returns ``None``) on a ``sell_to_close`` proposal, matching
this codebase's "de-risking is always permitted" principle used
throughout ``engine.risk.rules`` (e.g. ``forbid_short_phase_0``'s "a SELL
that closes a held long is always allowed" carve-out).
"""

from engine.options.rules.earnings_blackout import earnings_blackout
from engine.options.rules.expiry_day_entry import expiry_day_entry
from engine.options.rules.illiquid_contract import illiquid_contract
from engine.options.rules.iv_unavailable import iv_unavailable
from engine.options.rules.max_dte import max_dte
from engine.options.rules.max_premium_pct import max_premium_pct
from engine.options.rules.max_total_premium_pct import max_total_premium_pct
from engine.options.rules.min_dte import min_dte
from engine.options.rules.naked_short_forbidden import naked_short_forbidden
from engine.options.rules.options_disabled import options_disabled
from engine.options.rules.options_level_insufficient import options_level_insufficient

__all__ = [
    "earnings_blackout",
    "expiry_day_entry",
    "illiquid_contract",
    "iv_unavailable",
    "max_dte",
    "max_premium_pct",
    "max_total_premium_pct",
    "min_dte",
    "naked_short_forbidden",
    "options_disabled",
    "options_level_insufficient",
]
