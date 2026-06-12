"""ReconcilerFleet — per-user reconciliation against the REAL broker.

Replaces the single fixture-user ``Reconciler(MockBrokerPoller())`` from the
Phase 0/1 lifespan. Every tick:

  1. List active Alpaca connections → the set of users to reconcile.
  2. Per user: run the engine ``Reconciler.tick()`` (positions snapshot +
     circuit-breaker evaluation) against a ``UserBrokerPoller`` that
     decrypts the user's token PER TICK (decrypt-on-use — no long-lived
     plaintext, matching ``broker_use``'s design).
  3. Per user: sync open order rows against the broker (fills → order_fills
     + decision fill columns + PDT ledger) and detect positions the user
     closed directly at the broker. (Wired by ``order_sync``.)

Dev fallback: when NO user has a broker connection and the deployment is
non-production, the fixture user is ticked with the old MockBrokerPoller so
local demos keep producing snapshots. In production the fallback is OFF —
silence is better than fake equity feeding the circuit breaker.

Per-user errors are isolated: one user's expired token or broker outage
never stops the other users' ticks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engine.reconciler import (
    MockBrokerPoller,
    RawAccountState,
    Reconciler,
    ReconcilerConfig,
)
from engine.risk import PortfolioPosition, sector_for

from app.services.broker_use import with_broker_client

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.broker_store import BrokerStore

logger = logging.getLogger("api.reconciler_fleet")

_FIXTURE_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@dataclass
class UserBrokerPoller:
    """``BrokerPoller`` that opens the user's broker connection per tick.

    Deliberately does NOT hold an AlpacaBroker instance — the decrypted
    OAuth token would live for the whole process lifetime. One decrypt per
    tick keeps the plaintext window to milliseconds, same tradeoff as the
    executor path.
    """

    user_id: str
    name: str = "alpaca"

    async def get_account_state(self) -> RawAccountState:
        async with with_broker_client(self.user_id, broker="alpaca") as (broker, conn):
            equity = await broker.get_account_equity()
            buying_power = await broker.get_buying_power()
            broker_positions = await broker.list_positions()
            positions = tuple(
                PortfolioPosition(
                    symbol=p.symbol,
                    qty=p.qty,
                    avg_entry_price=p.avg_entry_price,
                    market_value=p.market_value,
                    sector=sector_for(p.symbol),
                )
                for p in broker_positions
            )
            cash = max(0.0, equity - sum(p.market_value for p in positions))
            return RawAccountState(
                equity=equity,
                cash=cash,
                buying_power=buying_power,
                open_positions=positions,
                raw={
                    "source": "alpaca",
                    "is_paper": conn.is_paper,
                    "connection_id": conn.id,
                },
            )


@dataclass
class FleetConfig:
    interval_seconds: float = 30.0
    halt_threshold_pct: float = -3.0
    # Non-production only: tick the fixture user with MockBrokerPoller when
    # nobody has a broker connection, so local demos still get snapshots.
    allow_mock_fallback: bool = False


@dataclass
class ReconcilerFleet:
    session_factory: async_sessionmaker
    broker_store: BrokerStore
    config: FleetConfig = field(default_factory=FleetConfig)

    def __post_init__(self) -> None:
        self._reconcilers: dict[str, Reconciler] = {}
        self._mock_reconciler: Reconciler | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def _reconciler_for(self, user_id: str) -> Reconciler:
        rec = self._reconcilers.get(user_id)
        if rec is None:
            rec = Reconciler(
                poller=UserBrokerPoller(user_id=user_id),
                session_factory=self.session_factory,
                user_id=uuid.UUID(user_id),
                config=ReconcilerConfig(
                    interval_seconds=self.config.interval_seconds,
                    halt_threshold_pct=self.config.halt_threshold_pct,
                ),
            )
            self._reconcilers[user_id] = rec
        return rec

    def _mock_fallback(self) -> Reconciler:
        if self._mock_reconciler is None:
            self._mock_reconciler = Reconciler(
                poller=MockBrokerPoller(),
                session_factory=self.session_factory,
                user_id=_FIXTURE_USER_ID,
                config=ReconcilerConfig(
                    interval_seconds=self.config.interval_seconds,
                    halt_threshold_pct=self.config.halt_threshold_pct,
                ),
            )
        return self._mock_reconciler

    async def tick(self) -> int:
        """One fleet pass. Returns the number of users reconciled."""
        try:
            conns = await self.broker_store.list_active_connections_by_broker("alpaca")
        except Exception:
            logger.exception("fleet: connection listing failed — skipping tick")
            return 0

        user_ids = sorted({c.user_id for c in conns})

        if not user_ids:
            if self.config.allow_mock_fallback:
                try:
                    await self._mock_fallback().tick()
                    return 1
                except Exception:
                    logger.exception("fleet: mock fallback tick failed")
            return 0

        reconciled = 0
        for uid in user_ids:
            try:
                result = await self._reconciler_for(uid).tick()
                reconciled += 1
                if result.transition.tripped:
                    logger.warning(
                        "fleet: breaker TRIPPED for user=%s (%s)",
                        uid, result.transition.reason,
                    )
            except Exception:
                logger.exception("fleet: reconcile tick failed for user=%s", uid)

            try:
                from app.services.order_sync import sync_user_orders_and_positions

                await sync_user_orders_and_positions(
                    user_id=uid, session_factory=self.session_factory
                )
            except Exception:
                logger.exception("fleet: order/position sync failed for user=%s", uid)

            try:
                from app.services.position_manager import manage_positions_for_user

                closes = await manage_positions_for_user(
                    user_id=uid, session_factory=self.session_factory
                )
                if closes:
                    logger.info("fleet: position manager initiated %d close(s) for %s", closes, uid)
            except Exception:
                logger.exception("fleet: position manager failed for user=%s", uid)

        return reconciled

    async def run_forever(self) -> None:
        logger.info(
            "reconciler fleet starting — interval=%ss threshold=%s%% mock_fallback=%s",
            self.config.interval_seconds,
            self.config.halt_threshold_pct,
            self.config.allow_mock_fallback,
        )
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("fleet tick raised — continuing")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.interval_seconds
                )
        logger.info("reconciler fleet stopped")

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.run_forever(), name="reconciler-fleet")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                logger.exception("fleet task raised on shutdown")
        self._task = None
