"""Risk engine — pre-trade veto layer.

Public surface:
    evaluate(proposal, context, caps, *, specialists=()) -> RiskDecision

Architecture rule (PLAN.md §6.2): every veto rule must have a stable
``veto_rule`` name so audit logs can identify which check fired without
parsing prose. The full list of names lives in each rule module's docstring.

``RiskDecision.checks_passed`` names every rule that ran and did not
block, so an approval is as auditable as a veto.

Implemented rules — evaluated in this order, first veto wins.
The informational ``wash_sale`` rule runs LAST and only contributes flags
(never returns approved=False):
    drawdown_halt_active / drawdown_halt_just_tripped
    forbid_short_phase_0          ← ALLOW_SHORTS master switch (default off)
    shortable_check               ← [SHORT] borrow eligibility
    short_requires_stop           ← [SHORT] no stop leg, no short
    min_council_confidence
    min_specialist_avg_score
    pdt_block                     ← US Pattern Day Trader rule
    max_open_positions
    max_position_pct(_trim)       ← [LONG] may TRIM; downstream sees trimmed qty
    short_unbounded_loss_cap      ← [SHORT] tighter cap; may TRIM
    short_gross_exposure_cap      ← [SHORT] book-wide short notional ceiling
    correlation_cap               ← cluster breadth (tighter than sector)
    sector_concentration
    single_name_concentration
    wash_sale_warning             ← INFORMATIONAL flag only; closes §6.2

PLAN.md §6.2 deterministic risk-rule list is complete as of (r).
"""

from engine.risk.assets import cluster_for, sector_for
from engine.risk.context import MockRiskContextProvider, RiskContextProvider
from engine.risk.engine import evaluate
from engine.risk.markets import is_derivative, market_of
from engine.risk.postgres_context import (
    DbRiskState,
    PostgresRiskContextProvider,
    load_db_risk_state,
)
from engine.risk.rules import covers_short_only, held_long_qty, held_short_qty, opens_short
from engine.risk.types import (
    ClosedTrade,
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    RiskDecision,
    RiskProposal,
    Side,
    SpecialistScore,
)

__all__ = [
    "ClosedTrade",
    "DbRiskState",
    "MockRiskContextProvider",
    "PortfolioPosition",
    "PostgresRiskContextProvider",
    "RiskCaps",
    "RiskContext",
    "RiskContextProvider",
    "RiskDecision",
    "RiskProposal",
    "Side",
    "SpecialistScore",
    "cluster_for",
    "covers_short_only",
    "evaluate",
    "held_long_qty",
    "held_short_qty",
    "is_derivative",
    "load_db_risk_state",
    "market_of",
    "opens_short",
    "sector_for",
]
