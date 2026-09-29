"""End-of-day job: mark the ghosts, then tell the operator how the day went.

docs/PLAN_PLATFORM.md Phase 6. Two things this replaces:

1. **Ghost P&L only ran inside the council cron.** ``daily_cron.main``
   marks refusals after its council loop, and returns early (exit 2) when
   the LLM cannot be constructed under AGENTS_REQUIRE_REAL_LLM. The
   Anthropic key was removed 2026-09-11, so from then on the Refusal
   Ledger stopped being marked at all, silently. Ghost marking is
   deterministic (daily closes, no LLM), so it runs here on its own
   schedule, whatever state the council is in.

2. **Nobody was told how a day went.** The breaker latched on 2026-09-08
   and the desk sat halted for days before a human noticed. One report
   per session: equity and day P&L, realized P&L and closes by reason,
   open positions, the breaker state, and LLM spend.

Delivery. The full report, with dollar amounts, goes to the log and to
OPS_ALERT_WEBHOOK_URL (the operator's own channel). The push to the
user's devices carries counts only: push bodies are lock-screen-safe by
this repo's rule (notifications.py), and account values are not.

One report per market (docs/PLAN_ZERODHA.md Z5). A user with an Alpaca
and a Zerodha account has a USD and an INR book: each report reads its
own broker's snapshot and its own market's decisions, in its own
currency, on its own day (the NSE report runs after the NSE close, on the
IST date). LLM spend is not per market, so each report shows the day's
total, labelled as such.

Deterministic reads only. Never raises into the scheduler.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select

from engine.risk.markets import BROKER_FOR_MARKET, MARKET_FOR_BROKER, market_of

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger("api.eod_report")


@dataclass(frozen=True)
class DailyReport:
    day: date
    equity: float | None = None
    day_pnl: float | None = None
    day_pnl_pct: float | None = None
    realized_today: float = 0.0
    closes_by_reason: dict[str, int] = field(default_factory=dict)
    open_positions: int = 0
    unrealized: float | None = None
    breaker_status: str = "normal"
    decisions_today: int = 0
    llm_spend_usd: float = 0.0
    llm_calls: int = 0
    ghost: dict[str, Any] | None = None
    market: str = "US"
    delivered: bool = True

    @property
    def currency(self) -> str:
        return CURRENCY.get(self.market, "$")

    @property
    def has_activity(self) -> bool:
        return (self.equity is not None or bool(self.closes_by_reason)
                or self.decisions_today > 0)


CURRENCY = {"US": "$", "IN": "₹"}
_MARKET_TZ = {"US": UTC, "IN": ZoneInfo("Asia/Kolkata")}


def _money(v: float, cur: str = "$") -> str:
    return f"-{cur}{abs(v):,.2f}" if v < 0 else f"+{cur}{v:,.2f}"


def _spend(v: float) -> str:
    """GLM calls cost fractions of a cent; "$0.00" over 40 real calls
    would read as "nothing ran"."""
    return "<$0.01" if 0 < v < 0.005 else f"${v:.2f}"


def render_report(r: DailyReport) -> tuple[str, str]:
    """Title and full body, dollar amounts included. For the log and the
    operator webhook, never for a push."""
    cur = r.currency
    title = (f"Daily report {r.day.isoformat()}" if r.market == "US"
             else f"NSE daily report {r.day.isoformat()}")
    lines: list[str] = []
    if r.equity is not None:
        day = ""
        if r.day_pnl is not None:
            pct = f" ({r.day_pnl_pct:+.2f}%)" if r.day_pnl_pct is not None else ""
            day = f", day {_money(r.day_pnl, cur)}{pct}"
        lines.append(f"Equity {cur}{r.equity:,.2f}{day}")
    else:
        lines.append("Equity unknown (no account snapshot)")
    closes = sum(r.closes_by_reason.values())
    if closes:
        by_reason = ", ".join(f"{k} {v}" for k, v in sorted(r.closes_by_reason.items()))
        lines.append(f"Closed {closes}: realized {_money(r.realized_today, cur)} ({by_reason})")
    else:
        lines.append("Closed 0")
    unreal = f", unrealized {_money(r.unrealized, cur)}" if r.unrealized is not None else ""
    lines.append(f"Open positions {r.open_positions}{unreal}")
    scope = "" if r.market == "US" else " (all markets)"
    lines.append(f"Council decisions {r.decisions_today}, LLM{scope} {_spend(r.llm_spend_usd)} "
                 f"over {r.llm_calls} call(s)")
    if r.breaker_status == "halted":
        lines.append("Breaker HALTED: no new entries until acknowledged")
    if r.ghost is not None:
        lines.append(
            f"Ghost marks: created {r.ghost.get('created', 0)}, "
            f"updated {r.ghost.get('updated', 0)}, finalized {r.ghost.get('finalized', 0)}"
        )
    return title, "\n".join(lines)


def push_body(r: DailyReport) -> str:
    """Lock-screen-safe: counts and states, never an account value."""
    parts = [
        f"{sum(r.closes_by_reason.values())} closed",
        f"{r.open_positions} open",
        f"{r.decisions_today} decisions",
    ]
    if r.breaker_status == "halted":
        parts.append("breaker HALTED")
    lead = "Day closed" if r.market == "US" else "NSE day closed"
    return f"{lead}: " + ", ".join(parts) + ". Open the app for P&L."


def _day_bounds(day: date, market: str = "US") -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=_MARKET_TZ.get(market, UTC))
    return start, start + timedelta(days=1)


def market_today(market: str) -> date:
    return datetime.now(_MARKET_TZ.get(market, UTC)).date()


async def build_daily_report(
    *, user_id: str, session_factory: async_sessionmaker, day: date, market: str = "US",
) -> DailyReport:
    from engine.db.models import (
        AgentDecision,
        CircuitBreakerState,
        LlmCall,
        PositionsSnapshot,
    )

    uid = uuid.UUID(user_id)
    start, end = _day_bounds(day, market)
    # This market's broker book only; another real broker's snapshot is
    # the other currency (a dev "mock" snapshot still counts).
    others = [b for b in MARKET_FOR_BROKER if b != BROKER_FOR_MARKET[market]]
    async with session_factory() as session:
        snap = (
            await session.execute(
                select(PositionsSnapshot)
                .where(PositionsSnapshot.user_id == uid)
                .where(PositionsSnapshot.source.notin_(others))
                .order_by(desc(PositionsSnapshot.captured_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        closed = [
            (reason, pnl) for sym, reason, pnl in (
                await session.execute(
                    select(AgentDecision.symbol, AgentDecision.close_reason,
                           AgentDecision.realized_pnl)
                    .where(AgentDecision.user_id == uid)
                    .where(AgentDecision.closed_at >= start)
                    .where(AgentDecision.closed_at < end)
                )
            ).all()
            if market_of(str(sym)) == market
        ]
        decisions = sum(
            1 for (sym,) in (
                await session.execute(
                    select(AgentDecision.symbol)
                    .where(AgentDecision.user_id == uid)
                    .where(AgentDecision.triggered_at >= start)
                    .where(AgentDecision.triggered_at < end)
                )
            ).all()
            if market_of(str(sym)) == market
        )
        breaker = (
            await session.execute(
                select(CircuitBreakerState.status).where(CircuitBreakerState.user_id == uid)
            )
        ).scalar_one_or_none()
        spend, calls = (
            await session.execute(
                select(func.coalesce(func.sum(LlmCall.cost_usd), 0), func.count(LlmCall.id))
                .where(LlmCall.called_at >= start)
                .where(LlmCall.called_at < end)
                .where(LlmCall.is_mock.is_(False))
            )
        ).one()

    positions = list(getattr(snap, "open_positions", None) or [])
    unrealized = (
        round(sum(float(p.get("unrealized_pl") or 0) for p in positions), 2)
        if snap is not None else None
    )
    return DailyReport(
        day=day,
        equity=float(snap.account_equity) if snap is not None else None,
        day_pnl=float(snap.daily_pnl) if snap is not None and snap.daily_pnl is not None else None,
        day_pnl_pct=(
            float(snap.daily_pnl_pct)
            if snap is not None and snap.daily_pnl_pct is not None else None
        ),
        realized_today=round(sum(float(p or 0) for _r, p in closed), 2),
        closes_by_reason=dict(Counter((r or "unknown") for r, _p in closed)),
        open_positions=len(positions),
        unrealized=unrealized,
        breaker_status=str(breaker or "normal"),
        decisions_today=int(decisions or 0),
        llm_spend_usd=round(float(spend or 0), 4),
        llm_calls=int(calls or 0),
        market=market,
    )


async def _mark_ghosts(
    day: date, *, market: str = "US", kite_client_factory: Any = None
) -> dict[str, Any] | None:
    try:
        from trading_agents.jobs.ghost_eval import evaluate_ghosts

        return dict(await evaluate_ghosts(
            today=day, market=market, kite_client_factory=kite_client_factory,
        ))
    except Exception:
        logger.exception("eod: ghost marking failed; the report still goes out")
        return None


def _deliver(user_id: str, report: DailyReport) -> None:
    title, body = render_report(report)
    logger.info("EOD %s\n%s", title, body)
    try:
        from app.services.notifications.ops_alerts import post_ops_webhook

        post_ops_webhook(f"{title}\n{body}")
    except Exception:
        logger.exception("eod: webhook delivery failed")
    try:
        from app.services.notifications.notifications import (
            schedule_position_event_notification,
        )

        schedule_position_event_notification(
            user_id=user_id,
            title="Trading day closed" if report.market == "US" else "NSE trading day closed",
            body=push_body(report), data_kind="daily_report",
        )
    except Exception:
        logger.exception("eod: push delivery failed")


async def run_eod(
    *, user_id: str, session_factory: async_sessionmaker, day: date | None = None,
    market: str = "US", skip_if_empty: bool = False, kite_client_factory: Any = None,
) -> DailyReport | None:
    """Ghosts first, so the report counts today's marks; then the report.

    Each report marks its own market's ghosts: US from Alpaca daily bars,
    NSE from Kite's through ``kite_client_factory`` (without one, NSE
    ghosts are skipped by name, never priced from Alpaca). ``skip_if_empty``
    (the NSE report) returns the report undelivered when the market had
    no snapshot, close or decision that day, so a user with no Zerodha
    account is not sent an empty report every NSE evening."""
    day = day or market_today(market)
    ghost = await _mark_ghosts(day, market=market, kite_client_factory=kite_client_factory)
    try:
        report = await build_daily_report(
            user_id=user_id, session_factory=session_factory, day=day, market=market,
        )
    except Exception:
        logger.exception("eod: could not build the %s daily report", market)
        return None
    report = replace(report, ghost=ghost)
    if skip_if_empty and not report.has_activity:
        logger.info("eod: no %s activity on %s; report not sent", market, day)
        return replace(report, delivered=False)
    _deliver(user_id, report)
    return report
