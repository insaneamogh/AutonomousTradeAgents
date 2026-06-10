"""correlation_cap — block when adding to a cluster already at the cap.

PLAN.md §6.2: "Correlation cap — don't open 5 bank stocks because they all
triggered the same signal." Concretely: if the user already holds three
mega-cap-tech names and the council proposes a fourth, refuse.

Cluster membership is resolved via ``engine.risk.assets.cluster_for``.
Symbols without a cluster (most of them) are not subject to this rule —
they fall through to single-name + sector concentration.

Adding to an EXISTING cluster member (same symbol the user already holds)
is fine — that's sizing into a position, not adding a new cluster member.

Phase 0/1 simplification: cluster membership is a hand-curated proxy for a
real ρ-matrix. Phase 2 swaps in actual correlation computed from
historical returns when there's enough price history.

veto_rule: correlation_cap
"""

from __future__ import annotations

from engine.risk.assets import cluster_for
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def correlation_cap(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.side is not Side.BUY:
        return None

    cluster = cluster_for(proposal.symbol)
    if cluster is None:
        return None  # unclustered symbol — fall through

    # Count DISTINCT held symbols in the same cluster, excluding the
    # proposal's own symbol (adding to an existing position isn't a new
    # cluster member — that's the single-name rule's job).
    held_in_cluster = {
        p.symbol for p in context.open_positions
        if p.qty > 0 and cluster_for(p.symbol) == cluster
    }
    held_in_cluster.discard(proposal.symbol)

    if len(held_in_cluster) >= caps.max_correlation_cluster:
        return RiskDecision(
            approved=False,
            reason=(
                f"Already holding {len(held_in_cluster)} names in cluster "
                f"'{cluster}' (cap {caps.max_correlation_cluster}). Adding "
                f"{proposal.symbol} would correlate beyond policy. "
                f"Held: {sorted(held_in_cluster)}."
            ),
            veto_rule="correlation_cap",
        )
    return None
