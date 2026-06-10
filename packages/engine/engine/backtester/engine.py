"""The event loop.

One pass per bar:
    1. Mark-to-market: record equity at this bar's close.
    2. Sim broker fills any pending orders queued at the previous bar's close.
    3. Strategy sees this bar's close → may emit new OrderRequests.
    4. Each request passes through ``RiskGate.evaluate`` — same rules that
       fire live. Vetoed requests are recorded but NOT forwarded; trimmed
       requests are forwarded with the adjusted qty.
    5. Approved requests are queued for the next bar's open.

PLAN.md §6.1: "build backtester before any agent" + "use the same vetoes
that fire live." Without the gate, backtest results overstate what the
agent could actually do in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, pstdev

from engine.backtester.events import Bar, FillEvent
from engine.backtester.feed import BarFeed
from engine.backtester.portfolio import Portfolio
from engine.backtester.risk_gate import RiskGate, TrimEvent, VetoEvent
from engine.backtester.sim_broker import SimulatedBroker
from engine.backtester.strategy import Strategy

logger = logging.getLogger("engine.backtester")


@dataclass
class BacktestResult:
    starting_cash: float
    ending_equity: float
    return_pct: float
    max_drawdown_pct: float
    sharpe_daily: float
    bars: int
    trades: int
    fills: list[FillEvent] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    risk_vetoes: list[VetoEvent] = field(default_factory=list)
    risk_trims: list[TrimEvent] = field(default_factory=list)


def _max_drawdown_pct(equity_curve: list[tuple[datetime, float]]) -> float:
    peak = float("-inf")
    worst = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak
            worst = max(worst, dd)
    return worst * 100.0


def _sharpe_daily(equity_curve: list[tuple[datetime, float]]) -> float:
    if len(equity_curve) < 3:
        return 0.0
    rets: list[float] = []
    for (_, e0), (_, e1) in zip(equity_curve, equity_curve[1:]):
        if e0 == 0:
            continue
        rets.append((e1 - e0) / e0)
    if not rets:
        return 0.0
    sd = pstdev(rets)
    if sd == 0:
        return 0.0
    # Annualize assuming ~252 trading days. Risk-free ignored at scaffold stage.
    return (mean(rets) / sd) * (252 ** 0.5)


@dataclass
class Engine:
    portfolio: Portfolio
    strategy: Strategy
    broker: SimulatedBroker
    risk_gate: RiskGate | None = field(default_factory=RiskGate)

    def run(self, feed: BarFeed) -> BacktestResult:
        all_fills: list[FillEvent] = []
        vetoes: list[VetoEvent] = []
        trims: list[TrimEvent] = []
        bars_seen = 0
        last_close_by_symbol: dict[str, float] = {}

        for bar in feed:
            bars_seen += 1

            # 1. Mark-to-market on this bar's close.
            last_close_by_symbol[bar.symbol] = bar.close

            # 2. Fill any pending orders at this bar's open.
            fills = self.broker.on_bar(bar)
            for f in fills:
                self.portfolio.on_fill(
                    symbol=f.symbol,
                    side=f.side,
                    qty=f.qty,
                    fill_price=f.fill_price,
                    sec_fee=f.sec_fee,
                    finra_taf=f.finra_taf,
                )
            all_fills.extend(fills)

            # Record equity AFTER fills, AT this bar's close.
            self.portfolio.record_equity(bar.timestamp, last_close_by_symbol)

            # 3. Strategy gets the bar close → may emit new orders.
            requests = self.strategy.on_bar(bar)

            # 4. Route every request through the risk gate.
            for req in requests:
                if self.risk_gate is None:
                    self.broker.submit(req)
                    continue
                outcome = self.risk_gate.evaluate(req, bar, self.portfolio)
                if outcome.veto is not None:
                    vetoes.append(outcome.veto)
                    logger.info(
                        "VETO  %s  %s %s qty=%d  rule=%s",
                        bar.timestamp.date(), req.side.value, req.symbol,
                        req.qty, outcome.veto.veto_rule,
                    )
                if outcome.trim is not None:
                    trims.append(outcome.trim)
                    logger.info(
                        "TRIM  %s  %s %s qty=%d→%d",
                        bar.timestamp.date(), req.side.value, req.symbol,
                        req.qty, outcome.trim.adjusted_qty,
                    )
                if outcome.approved and outcome.request is not None:
                    self.broker.submit(outcome.request)

        ending = self.portfolio.equity_curve[-1][1] if self.portfolio.equity_curve else self.portfolio.cash
        return BacktestResult(
            starting_cash=self.portfolio.starting_cash,
            ending_equity=ending,
            return_pct=(ending / self.portfolio.starting_cash - 1.0) * 100.0,
            max_drawdown_pct=_max_drawdown_pct(self.portfolio.equity_curve),
            sharpe_daily=_sharpe_daily(self.portfolio.equity_curve),
            bars=bars_seen,
            trades=len(all_fills),
            fills=all_fills,
            equity_curve=self.portfolio.equity_curve,
            risk_vetoes=vetoes,
            risk_trims=trims,
        )
