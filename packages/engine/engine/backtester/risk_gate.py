"""RiskGate — the bridge between strategy proposals and the sim broker.

PLAN.md §6.1 explicitly says backtests need to use the **same** veto rules
that fire live. Without this gate, the backtester's results overstate what
the agent would actually be allowed to do in production — every honest
backtest in this codebase routes proposals through ``engine.risk.evaluate``.

Inputs:
  - ``OrderRequest``  (broker-agnostic, what the strategy emits)
  - ``Portfolio``     (the backtester's running cash + position state)
  - ``Bar`` close price (used as the mark for ``RiskContext.account_equity``)

Outputs:
  - ``GateOutcome`` with: approved, optionally-trimmed OrderRequest, veto
    metadata (rule name + reason) when blocked.
  - Veto / trim events are appended to ``VetoEvent`` lists on the
    ``BacktestResult`` for offline inspection.

Confidence: strategies don't emit a council-style confidence number in
Phase 0/1. The gate uses ``RiskGate.default_confidence`` (0.7) — well above
the 0.5 floor so the strategy doesn't get blocked by the confidence rule
unless the caller deliberately raises it. Phase 2 strategies that emit real
confidence will plumb through a wrapper proposal type.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from broker.types import OrderRequest, Side as BrokerSide
from engine.backtester.events import Bar
from engine.backtester.portfolio import Portfolio
from engine.risk import (
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    RiskProposal,
    Side,
    evaluate,
)


@dataclass(frozen=True)
class VetoEvent:
    timestamp: datetime
    symbol: str
    side: str
    requested_qty: int
    veto_rule: str
    reason: str


@dataclass(frozen=True)
class TrimEvent:
    timestamp: datetime
    symbol: str
    side: str
    requested_qty: int
    adjusted_qty: int
    reason: str


@dataclass(frozen=True)
class GateOutcome:
    approved: bool
    request: OrderRequest | None  # the (possibly-trimmed) request to forward
    veto: VetoEvent | None = None
    trim: TrimEvent | None = None


@dataclass
class RiskGate:
    """Runs ``engine.risk.evaluate`` against every strategy proposal."""

    caps: RiskCaps = field(default_factory=RiskCaps)
    default_confidence: float = 0.7

    def evaluate(
        self,
        request: OrderRequest,
        bar: Bar,
        portfolio: Portfolio,
    ) -> GateOutcome:
        prices = {bar.symbol: bar.close}
        equity = portfolio.mark_to_market(prices)
        proposal = RiskProposal(
            symbol=request.symbol,
            side=Side(request.side.value),  # BrokerSide ↔ engine.risk.Side share string values
            qty=request.qty,
            estimated_notional=request.qty * bar.close,
            last_price=bar.close,
            confidence=self.default_confidence,
            closes_intraday_position=False,  # daily bars: positions are held overnight
        )

        ctx = RiskContext(
            account_equity=equity,
            cash=portfolio.cash,
            buying_power=portfolio.cash,  # backtester is cash-account by default
            open_positions=tuple(_portfolio_positions(portfolio, prices)),
            day_trades_last_5d=0,
            daily_pnl=0.0,
            daily_pnl_pct=0.0,
            drawdown_halted=False,
        )

        decision = evaluate(proposal, ctx, self.caps)

        if not decision.approved:
            return GateOutcome(
                approved=False,
                request=None,
                veto=VetoEvent(
                    timestamp=bar.timestamp,
                    symbol=request.symbol,
                    side=request.side.value,
                    requested_qty=request.qty,
                    veto_rule=decision.veto_rule or "unknown",
                    reason=decision.reason,
                ),
            )

        if decision.adjusted_qty is not None and decision.adjusted_qty != request.qty:
            trimmed = replace(request, qty=decision.adjusted_qty)
            return GateOutcome(
                approved=True,
                request=trimmed,
                trim=TrimEvent(
                    timestamp=bar.timestamp,
                    symbol=request.symbol,
                    side=request.side.value,
                    requested_qty=request.qty,
                    adjusted_qty=decision.adjusted_qty,
                    reason=decision.reason,
                ),
            )

        return GateOutcome(approved=True, request=request)


def _portfolio_positions(
    portfolio: Portfolio,
    prices: dict[str, float],
) -> list[PortfolioPosition]:
    out: list[PortfolioPosition] = []
    for sym, pos in portfolio.positions.items():
        if pos.qty == 0:
            continue
        mark = prices.get(sym, pos.avg_entry_price)
        out.append(
            PortfolioPosition(
                symbol=sym,
                qty=pos.qty,
                avg_entry_price=pos.avg_entry_price,
                market_value=pos.qty * mark,
                sector=None,  # resolved by engine.risk.assets.sector_for in the rule
            )
        )
    return out


# Keep BrokerSide noticeably imported so static analyzers don't strip it.
_ = BrokerSide
