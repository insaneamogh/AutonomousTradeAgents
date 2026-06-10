"""CLI smoke test for the broker package.

Usage:
    uv run python -m broker --smoke
    uv run python -m broker --smoke --symbol AAPL --qty 1

Requires the env vars from .env.example:
    ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL (paper)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import uuid

from broker.alpaca import AlpacaBroker
from broker.types import OrderRequest, OrderType, Side

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s — %(message)s",
)
log = logging.getLogger("broker.smoke")


async def smoke(symbol: str, qty: int, place_order: bool) -> int:
    if "ALPACA_API_KEY" not in os.environ or "ALPACA_API_SECRET" not in os.environ:
        log.error("Missing ALPACA_API_KEY / ALPACA_API_SECRET. Copy .env.example to .env and fill in.")
        return 1

    broker = AlpacaBroker.from_env()
    log.info("Connected to Alpaca (paper=%s)", broker.is_paper)
    if not broker.is_paper:
        log.error("Smoke test refuses to run against LIVE. Set ALPACA_BASE_URL to the paper endpoint.")
        return 2

    equity = await broker.get_account_equity()
    bp = await broker.get_buying_power()
    log.info("Account: equity=$%.2f  buying_power=$%.2f", equity, bp)

    positions = await broker.list_positions()
    log.info("Open positions: %d", len(positions))
    for p in positions[:5]:
        log.info("  %-6s qty=%-6d avg=$%-9.4f mv=$%-12.2f upl=$%.2f (%.2f%%)",
                 p.symbol, p.qty, p.avg_entry_price, p.market_value,
                 p.unrealized_pl, p.unrealized_pl_pct)

    if not place_order:
        log.info("Skipping order placement (--no-order). Smoke test complete.")
        return 0

    client_order_id = f"smoke-{uuid.uuid4().hex[:12]}"
    req = OrderRequest(
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=client_order_id,
    )
    log.info("Placing test BUY %s qty=%d client_order_id=%s", symbol, qty, client_order_id)
    order = await broker.place_order(req)
    log.info("Submitted: broker_order_id=%s status=%s", order.broker_order_id, order.status.value)

    await asyncio.sleep(2)
    fresh = await broker.get_order(order.broker_order_id)
    log.info("Polled:    status=%s filled_qty=%d avg=%s",
             fresh.status.value, fresh.filled_qty, fresh.avg_fill_price)

    if fresh.status.value not in ("filled", "canceled", "rejected", "expired"):
        log.info("Canceling to keep the paper account clean…")
        canceled = await broker.cancel_order(order.broker_order_id)
        log.info("Canceled:  status=%s", canceled.status.value)

    log.info("Smoke test complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Alpaca paper smoke test.")
    parser.add_argument("--smoke", action="store_true", help="run the smoke test")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument(
        "--no-order",
        action="store_true",
        help="skip order placement (account read-only check)",
    )
    args = parser.parse_args()

    if not args.smoke:
        parser.print_help()
        return 0

    return asyncio.run(smoke(args.symbol, args.qty, place_order=not args.no_order))


if __name__ == "__main__":
    raise SystemExit(main())
