"""Paper trading engine — simulated fills + portfolio, real risk chain.

THE trust-building mode. PLAN.md Phase 4 requires months of paper
validation before real capital; Zerodha has no sandbox environment at
all. This module gives both brokers (and no-broker dev) the same
simulated execution path:

  - ``TRADING_MODE`` env: ``paper`` (DEFAULT) | ``live``.
    In paper mode the executor never opens a broker client for orders —
    fills are simulated here. Broker connections remain useful for
    account reads (portfolio summary) and, later, market data.
    Live mode still passes through the ``live_trading_disabled`` gate
    (``LIVE_TRADING_ENABLED=1``) — flipping to real money is a TWO-key
    operation: TRADING_MODE=live AND LIVE_TRADING_ENABLED=1.

  - One paper portfolio per (user, market). US books in USD, IN books in
    INR — currencies never mix, mirroring the per-broker discipline.
    Starting cash via ``PAPER_STARTING_CASH_US`` (default 100,000) and
    ``PAPER_STARTING_CASH_IN`` (default 1,000,000).

  - Fills are immediate at the proposal's limit price, else its last
    price. Positions are marked opportunistically whenever a new
    proposal references the symbol; continuous marks arrive with the
    real-data ingest (see docs/reference/market-data-options.md).

  - Idempotent on ``client_order_id`` like the real brokers.

Persistence: in-memory (process lifetime). The store is behind a module
factory so a Postgres adapter can land later without touching callers —
same pattern as every other store in the app. For a long-running
personal test, keep the API process up or expect the paper book to
reset on restart (documented in RUNBOOK).
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("api.paper_broker")


def trading_mode() -> str:
    """``paper`` (default — simulated fills) or ``live`` (real brokers)."""
    mode = os.environ.get("TRADING_MODE", "").strip().lower()
    return mode if mode in ("paper", "live") else "paper"


_DEFAULT_STARTING_CASH = {"US": 100_000.0, "IN": 1_000_000.0}


def _starting_cash(market: str) -> float:
    env = os.environ.get(f"PAPER_STARTING_CASH_{market}", "").strip()
    try:
        return float(env) if env else _DEFAULT_STARTING_CASH.get(market, 100_000.0)
    except ValueError:
        return _DEFAULT_STARTING_CASH.get(market, 100_000.0)


@dataclass
class PaperHolding:
    symbol: str
    qty: int
    avg_entry_price: float
    mark: float
    """Last known reference price — updated opportunistically."""


@dataclass(frozen=True)
class PaperFill:
    id: str
    proposal_id: str | None
    client_order_id: str | None
    symbol: str
    market: str
    side: str
    qty: int
    price: float
    realized_pnl: float | None
    """Set on SELLs: (price - avg_entry) * qty. None on BUYs."""
    filled_at: datetime


class PaperPortfolio:
    """One user's simulated book for one market (US or IN)."""

    def __init__(self, market: str) -> None:
        self.market = market
        self.cash: float = _starting_cash(market)
        self.holdings: dict[str, PaperHolding] = {}
        self.fills: list[PaperFill] = []

    # ── Reads ────────────────────────────────────────────────────────

    def equity(self) -> float:
        """Cash + marked value of holdings."""
        return self.cash + sum(h.qty * h.mark for h in self.holdings.values())

    def find_fill_by_client_order_id(self, client_order_id: str) -> PaperFill | None:
        return next(
            (f for f in self.fills if f.client_order_id == client_order_id), None
        )

    # ── Writes ───────────────────────────────────────────────────────

    def mark(self, symbol: str, price: float) -> None:
        """Opportunistic mark-to-market when fresh price info arrives."""
        held = self.holdings.get(symbol)
        if held is not None and price > 0:
            held.mark = price

    def fill(
        self,
        *,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        proposal_id: str | None,
        client_order_id: str | None,
    ) -> PaperFill:
        """Apply an immediate simulated fill. Caller has already passed the
        risk chain; this only does the bookkeeping. SELL qty clamps to the
        held quantity (long-only — forbid_short vetoes naked sells before
        we get here, but a stale proposal could still over-sell).
        """
        if client_order_id:
            existing = self.find_fill_by_client_order_id(client_order_id)
            if existing is not None:
                logger.info(
                    "paper: dedupe hit client_order_id=%s — returning existing fill",
                    client_order_id,
                )
                return existing

        realized: float | None = None
        if side == "BUY":
            held = self.holdings.get(symbol)
            if held is None:
                self.holdings[symbol] = PaperHolding(
                    symbol=symbol, qty=qty, avg_entry_price=price, mark=price
                )
            else:
                total = held.qty + qty
                held.avg_entry_price = (
                    held.avg_entry_price * held.qty + price * qty
                ) / total
                held.qty = total
                held.mark = price
            self.cash -= qty * price
        else:  # SELL
            held = self.holdings.get(symbol)
            if held is None:
                qty = 0
            else:
                qty = min(qty, held.qty)
                realized = (price - held.avg_entry_price) * qty
                held.qty -= qty
                if held.qty == 0:
                    del self.holdings[symbol]
            self.cash += qty * price

        f = PaperFill(
            id=f"paper-{uuid.uuid4().hex[:12]}",
            proposal_id=proposal_id,
            client_order_id=client_order_id,
            symbol=symbol,
            market=self.market,
            side=side,
            qty=qty,
            price=price,
            realized_pnl=realized,
            filled_at=datetime.now(timezone.utc),
        )
        self.fills.append(f)
        return f


class InMemoryPaperStore:
    """Per-(user, market) portfolios. Process-lifetime persistence."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str], PaperPortfolio] = {}

    def portfolio(self, user_id: str, market: str) -> PaperPortfolio:
        key = (user_id, market)
        if key not in self._books:
            self._books[key] = PaperPortfolio(market)
        return self._books[key]

    def has_book(self, user_id: str, market: str) -> bool:
        return (user_id, market) in self._books


_paper_store: InMemoryPaperStore | None = None


def get_paper_store() -> InMemoryPaperStore:
    global _paper_store
    if _paper_store is None:
        _paper_store = InMemoryPaperStore()
    return _paper_store


def reset_paper_store_for_tests() -> None:
    global _paper_store
    _paper_store = None
