"""mis_square_off_block — no new India intraday entries near the close.

Indian brokers force-square-off MIS (intraday) positions around 15:20 IST;
NSE cash closes 15:30 IST. A NEW intraday entry after ~15:00 IST has
minutes to work before the broker liquidates it at market — that's a fee
machine, not a trade. Blocked past the configurable cutoff.

Applies only to IN-market proposals flagged ``is_intraday=True``. Delivery
(CNC) and overnight derivative (NRML) entries are untouched. The clock is
``context.now_utc`` when injected (tests), else the real wall clock.

veto_rule: mis_square_off_block
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.risk.markets import market_of
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal

IST = timezone(timedelta(hours=5, minutes=30))


def mis_square_off_block(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if market_of(proposal.symbol) != "IN" or not proposal.is_intraday:
        return None

    now_utc = context.now_utc or datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)
    cutoff = now_ist.replace(
        hour=caps.mis_entry_cutoff_hour_ist,
        minute=caps.mis_entry_cutoff_minute_ist,
        second=0,
        microsecond=0,
    )
    if now_ist >= cutoff:
        return RiskDecision(
            approved=False,
            reason=(
                f"New intraday (MIS) entries blocked after "
                f"{cutoff.strftime('%H:%M')} IST — broker square-off at "
                f"~15:20 IST leaves no time for the trade to work. "
                f"(now {now_ist.strftime('%H:%M')} IST)"
            ),
            veto_rule="mis_square_off_block",
        )
    return None
