"""Per-broker portfolio summary — the "show me real profits" read path.

For every ACTIVE broker connection of the caller this builds one
``BrokerPortfolioDto``:

  - live equity / buying power / open positions (with unrealized P&L)
    straight from the broker via decrypt-on-use;
  - a profit window: realized P&L over the last N days from the agent's
    own ``DecisionLog`` (closed trades carry ``realized_pnl``), attributed
    to the broker by SYMBOL MARKET — exchange-prefixed Indian symbols
    ("NSE:RELIANCE", "NFO:…") belong to zerodha, bare US symbols to
    alpaca. That mapping is exact today because each market routes to
    exactly one broker; a ``broker`` column on the decision log is the
    cleaner Phase 4 upgrade and the DTO carries an ``attribution`` marker
    so clients know which scheme produced the number.

Failure isolation: one broker's failure (expired daily Zerodha token, SDK
missing, network) must never 500 the endpoint — that broker's entry gets
``status: token_expired | unavailable`` + a human detail string while the
other broker still reports.

Currency discipline: alpaca → USD, zerodha → INR. No cross-broker sums.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from engine.risk.markets import market_of

from app.schemas.portfolio import (
    BrokerPortfolioDto,
    PortfolioPositionDto,
    PortfolioSummaryResponse,
    ProfitWindowDto,
)
from app.services.broker_store import BrokerStore, get_broker_store
from app.services.broker_use import BrokerUnavailableError, with_broker_client
from app.services.paper_broker import get_paper_store, trading_mode

logger = logging.getLogger("api.portfolio")

_CURRENCY_BY_BROKER = {"alpaca": "USD", "zerodha": "INR"}
_MARKET_BY_BROKER = {"alpaca": "US", "zerodha": "IN"}


async def build_portfolio_summary(
    user_id: str,
    *,
    window_days: int = 30,
    store: BrokerStore | None = None,
) -> PortfolioSummaryResponse:
    """One DTO per active connection; per-broker errors degrade, not raise."""
    s = store or get_broker_store()
    rows = await s.list_connections(user_id)
    active = [r for r in rows if r.status == "active"]

    realized = await _realized_by_market(user_id, window_days)

    out: list[BrokerPortfolioDto] = []

    # In paper mode the paper book IS the agent's track record — surface it
    # first, one entry per market that has any activity (plus US by default
    # so day-1 users see their starting cash).
    if trading_mode() == "paper":
        paper_store = get_paper_store()
        for market in ("US", "IN"):
            if market == "US" or paper_store.has_book(user_id, market):
                out.append(_paper_dto(user_id, market, window_days))

    for conn in active:
        out.append(
            await _broker_dto(
                user_id,
                conn.broker,
                conn.is_paper,
                conn.account_number,
                window_days,
                realized.get(_MARKET_BY_BROKER.get(conn.broker, "US"), _EMPTY_REALIZED),
                store=s,
            )
        )
    return PortfolioSummaryResponse(brokers=out)


def _paper_dto(user_id: str, market: str, window_days: int) -> BrokerPortfolioDto:
    """The simulated book for one market — fills + marks from the paper engine."""
    pf = get_paper_store().portfolio(user_id, market)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    realized = 0.0
    completed = wins = losses = 0
    for f in pf.fills:
        if f.realized_pnl is None or f.filled_at < cutoff:
            continue
        realized += f.realized_pnl
        completed += 1
        if f.realized_pnl >= 0:
            wins += 1
        else:
            losses += 1

    positions = [
        PortfolioPositionDto(
            symbol=h.symbol,
            qty=h.qty,
            avg_entry_price=h.avg_entry_price,
            market_value=h.qty * h.mark,
            unrealized_pl=(h.mark - h.avg_entry_price) * h.qty,
            unrealized_pl_pct=(
                ((h.mark - h.avg_entry_price) / h.avg_entry_price) * 100
                if h.avg_entry_price
                else 0.0
            ),
        )
        for h in pf.holdings.values()
    ]

    return BrokerPortfolioDto(
        broker="paper",
        is_paper=True,
        currency="USD" if market == "US" else "INR",
        status="ok",
        account_number=f"PAPER-{market}",
        equity=pf.equity(),
        buying_power=pf.cash,
        positions=positions,
        profit_window=ProfitWindowDto(
            window_days=window_days,
            realized_pnl=realized,
            completed_trades=completed,
            wins=wins,
            losses=losses,
            unrealized_pnl=sum(p.unrealized_pl for p in positions),
            attribution="paper_engine",
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Realized window P&L from the decision log, bucketed by market
# ─────────────────────────────────────────────────────────────────────


_EMPTY_REALIZED = {"realized_pnl": 0.0, "completed": 0, "wins": 0, "losses": 0}


async def _realized_by_market(user_id: str, window_days: int) -> dict[str, dict[str, float | int]]:
    """{"US": {...}, "IN": {...}} from completed decisions in the window."""
    # Lazy import: the agents package is on PYTHONPATH in every deploy
    # config, but keep the API importable standalone just in case.
    try:
        from trading_agents.memory import get_decision_log
    except ImportError:  # pragma: no cover — agents package always shipped
        logger.warning("portfolio: trading_agents not importable — realized P&L empty")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    buckets: dict[str, dict[str, float | int]] = {
        "US": dict(_EMPTY_REALIZED),
        "IN": dict(_EMPTY_REALIZED),
    }
    try:
        decisions = await get_decision_log().all_decisions()
    except Exception as exc:  # noqa: BLE001 — telemetry read must not break the route
        logger.warning("portfolio: decision log read failed — %s", exc)
        return buckets

    for d in decisions:
        if d.realized_pnl is None:
            continue
        if d.user_id is not None and d.user_id != user_id:
            continue
        if d.triggered_at < cutoff:
            continue
        b = buckets[market_of(d.symbol)]
        b["realized_pnl"] = float(b["realized_pnl"]) + float(d.realized_pnl)
        b["completed"] = int(b["completed"]) + 1
        if d.realized_pnl >= 0:
            b["wins"] = int(b["wins"]) + 1
        else:
            b["losses"] = int(b["losses"]) + 1
    return buckets


# ─────────────────────────────────────────────────────────────────────
# Per-broker live read
# ─────────────────────────────────────────────────────────────────────


async def _broker_dto(
    user_id: str,
    broker: str,
    is_paper: bool,
    account_number: str | None,
    window_days: int,
    realized: dict[str, float | int],
    *,
    store: BrokerStore,
) -> BrokerPortfolioDto:
    currency = _CURRENCY_BY_BROKER.get(broker, "USD")
    try:
        async with with_broker_client(user_id, broker=broker, store=store) as (client, conn):
            equity = await client.get_account_equity()
            buying_power = await client.get_buying_power()
            positions = await client.list_positions()
    except BrokerUnavailableError as exc:
        status = "token_expired" if "expired" in str(exc).lower() else "unavailable"
        return BrokerPortfolioDto(
            broker=broker,
            is_paper=is_paper,
            currency=currency,
            status=status,
            detail=str(exc),
            account_number=account_number,
        )
    except Exception as exc:  # noqa: BLE001 — broker 5xx / network must degrade
        logger.warning("portfolio: %s live read failed — %s", broker, exc)
        return BrokerPortfolioDto(
            broker=broker,
            is_paper=is_paper,
            currency=currency,
            status="unavailable",
            detail=f"broker read failed: {exc}",
            account_number=account_number,
        )

    position_dtos = [
        PortfolioPositionDto(
            symbol=p.symbol,
            qty=p.qty,
            avg_entry_price=p.avg_entry_price,
            market_value=p.market_value,
            unrealized_pl=p.unrealized_pl,
            unrealized_pl_pct=p.unrealized_pl_pct,
        )
        for p in positions
    ]
    unrealized = sum(p.unrealized_pl for p in position_dtos)

    return BrokerPortfolioDto(
        broker=broker,
        is_paper=conn.is_paper,
        currency=currency,
        status="ok",
        account_number=conn.account_number or account_number,
        equity=equity,
        buying_power=buying_power,
        positions=position_dtos,
        profit_window=ProfitWindowDto(
            window_days=window_days,
            realized_pnl=float(realized["realized_pnl"]),
            completed_trades=int(realized["completed"]),
            wins=int(realized["wins"]),
            losses=int(realized["losses"]),
            unrealized_pnl=unrealized,
        ),
    )
