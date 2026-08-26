"""Risk rules — one file per rule. Each rule is a pure function with a stable
``veto_rule`` name so audit logs can identify which rule fired without
parsing prose.

Ordering matters in ``engine.evaluate`` — see that module for the
canonical sequence (catastrophic / state-level rules first, then sizing,
then aggregate-exposure rules — which see the post-trim qty). The
informational ``wash_sale`` rule runs LAST and only contributes flags.
"""

from engine.risk.rules._short import held_long_qty, opens_short
from engine.risk.rules.confidence import min_council_confidence
from engine.risk.rules.correlation_cap import correlation_cap
from engine.risk.rules.derivative_notional import derivative_notional_cap
from engine.risk.rules.drawdown_halt import drawdown_halt
from engine.risk.rules.forbid_short import forbid_short_phase_0
from engine.risk.rules.lot_size import lot_size_block
from engine.risk.rules.max_open_positions import max_open_positions
from engine.risk.rules.mis_square_off import mis_square_off_block
from engine.risk.rules.pdt_block import pdt_block
from engine.risk.rules.position_size import position_size_cap
from engine.risk.rules.sector_concentration import sector_concentration
from engine.risk.rules.short_exposure import short_unbounded_loss_cap
from engine.risk.rules.short_requires_stop import short_requires_stop
from engine.risk.rules.shortable import shortable_check
from engine.risk.rules.single_name import single_name_concentration
from engine.risk.rules.specialist_avg_score import min_specialist_avg_score
from engine.risk.rules.wash_sale import wash_sale

__all__ = [
    "correlation_cap",
    "derivative_notional_cap",
    "drawdown_halt",
    "forbid_short_phase_0",
    "held_long_qty",
    "lot_size_block",
    "max_open_positions",
    "min_council_confidence",
    "min_specialist_avg_score",
    "mis_square_off_block",
    "opens_short",
    "pdt_block",
    "position_size_cap",
    "sector_concentration",
    "short_requires_stop",
    "short_unbounded_loss_cap",
    "shortable_check",
    "single_name_concentration",
    "wash_sale",
]
