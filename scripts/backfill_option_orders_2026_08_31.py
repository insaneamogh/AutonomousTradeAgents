"""One-off data repair: backfill the 6 option positions the missing-orders-
row bug left unmanaged (2026-09-01 investigation).

NOT RUN BY THE AGENT THAT WROTE THIS. Prepared, reviewed against live data,
and left here for the operator to run deliberately — see the accompanying
report / fable5findings.md entry for why: this script writes directly to
the production database, and CLAUDE.md's own process for this task was
"fix the code, I'll review and merge personally" — a direct prod data
mutation is at least as consequential as a merge and was not something the
agent had standing to do unasked mid-investigation.

--- Background ---

``apps/agents/trading_agents/options/tools/trade.py``'s ``open_option_trade``
placed 6 REAL option orders at the broker (via the Bull/Bear options
council, ``AUTO_TRADE_ENABLED=1``) but never wrote a row to the ``orders``
table — see the fix in this same commit. Because there is no ``orders``
row, ``order_sync.py`` never had anything to poll, so
``agent_decisions.fill_qty``/``fill_avg_price`` stayed NULL forever even
though all 6 contracts are real, filled, currently-open Alpaca paper
positions. That NULL fill_qty is what:

  1. Made ``position_manager.py``'s ratchet/stop-loss/time-stop
     (``manage_positions_for_user``) and the supposedly-unconditional
     DTE<=2 expiry sweep (``sweep_expiring_options_for_user``) both skip
     these positions entirely — no exit mechanism has been running on any
     of them since they opened.
  2. Made them render as permanently "awaiting fill" in
     ``positions_service.list_open_positions``'s managed list AND (via
     the separate OCC-vs-underlying ``_unmanaged()`` bug, also fixed in
     this commit) as broker-side "unmanaged / no council decision behind
     it" — both wrong, for the same underlying reason.

The code fix (this same commit) stops this from happening to any FUTURE
option trade. It does NOT retroactively fix these 6 already-broken rows —
fixing the code does not rewrite data that already landed wrong. This
script is that retroactive fix.

--- What it does ---

For each of 6 named ``agent_decisions`` rows (all belonging to
user_id=43221580-69bc-4134-8e1e-5af75499d874, the real cron user):

  1. Re-reads the row and asserts it STILL has fill_qty IS NULL and
     closed_at IS NULL (the state verified live on 2026-09-01 — see
     ``EXPECTED`` below). Refuses to touch a row that has since changed
     (someone else fixed it, closed it, or the position moved) rather than
     blindly overwriting.
  2. Sets fill_qty / fill_avg_price from the value ALREADY VERIFIED against
     the live ``positions_snapshot`` (queried 2026-09-01 19:06 UTC
     snapshot — see EXPECTED below) — the same qty/avg_entry_price Alpaca
     itself reports for that contract right now.
  3. Inserts a companion ``orders`` row so the audit trail has SOMETHING
     for each: status='filled', broker_order_id=NULL (the real one was
     never captured — never fabricated), client_order_id=
     'backfill-open-<decision_id>' (obviously synthetic, greppable).

Idempotent: every UPDATE is guarded by ``fill_qty IS NULL AND closed_at IS
NULL``, and every INSERT is ON CONFLICT DO NOTHING on client_order_id — a
second run changes nothing.

--- What it deliberately does NOT do ---

  - Does not touch ``approval_mode``/``user_response`` (already correct:
    'auto'/'approved').
  - Does not attempt to reconstruct the ORIGINAL broker_order_id, fill
    timestamp, or exact commission/fee detail — none of that was ever
    captured, and fabricating it would be worse than leaving it absent.
  - Does not run itself against production. See ``--apply`` below.

--- Usage ---

    # Dry run (default) — prints what WOULD change, touches nothing:
    uv run python scripts/backfill_option_orders_2026_08_31.py

    # Actually write, after you've read the dry-run output:
    DATABASE_URL=postgresql+asyncpg://... \\
        uv run python scripts/backfill_option_orders_2026_08_31.py --apply

Re-verify the EXPECTED values below against a fresh snapshot before
running if any real time has passed since 2026-09-01 — these positions
are live and their marks move every reconciler tick (the qty/avg_entry
values themselves should NOT move unless a scale-in happened, but check).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("backfill")

USER_ID = uuid.UUID("43221580-69bc-4134-8e1e-5af75499d874")
# The one active paper Alpaca broker_connections row for this user,
# confirmed live 2026-09-01 (broker='alpaca', is_paper=True, status='active').
BROKER_CONNECTION_ID = uuid.UUID("cda6b8ce-5c10-4361-8fdc-af517022e38c")

# Verified live against agent_decisions (2026-09-01) + the 2026-08-31
# 19:06:36 UTC positions_snapshot row (broker-reported qty/avg_entry_price
# for each OCC contract, matching exactly). Re-verify before running if
# stale — see the module docstring.
EXPECTED = [
    {
        "decision_id": uuid.UUID("57baf2a5-d6c3-4fd4-9d76-ff8b0ef08976"),
        "underlying": "NVDA",
        "occ_symbol": "NVDA261009C00230000",
        "qty": 4,
        "avg_entry_price": Decimal("5.30"),
    },
    {
        "decision_id": uuid.UUID("c1f5e59e-c1b8-4635-b82a-7d806bd112d8"),
        "underlying": "SPY",
        "occ_symbol": "SPY261002C00771000",
        "qty": 2,
        "avg_entry_price": Decimal("8.73"),
    },
    {
        "decision_id": uuid.UUID("d52b169c-ccb4-4129-83e8-cbfb2936fe7c"),
        "underlying": "NVDA",
        "occ_symbol": "NVDA260918C00215000",
        "qty": 2,
        "avg_entry_price": Decimal("8.70"),
    },
    {
        "decision_id": uuid.UUID("909e076d-827c-456d-abbb-e8ff08de9781"),
        "underlying": "NVDA",
        "occ_symbol": "NVDA261002C00225000",
        "qty": 4,
        "avg_entry_price": Decimal("5.95"),
    },
    {
        "decision_id": uuid.UUID("dc52edd3-03e3-4bfb-bfee-942725c5ab82"),
        "underlying": "SPY",
        "occ_symbol": "SPY260918C00765000",
        "qty": 2,
        "avg_entry_price": Decimal("9.35"),
    },
    {
        "decision_id": uuid.UUID("6dca9c80-4a0f-48b5-ab42-ca666dcf8d66"),
        "underlying": "QQQ",
        "occ_symbol": "QQQ260918C00708000",
        "qty": 1,
        "avg_entry_price": Decimal("16.25"),
    },
]


async def main(apply: bool) -> int:
    from sqlalchemy import select, text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from engine.db.models import AgentDecision, Order
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    changed = 0
    async with factory() as session:
        for row in EXPECTED:
            decision = await session.get(AgentDecision, row["decision_id"])
            if decision is None:
                log.warning("SKIP %s — decision row no longer exists", row["decision_id"])
                continue
            occ = (decision.proposal or {}).get("occSymbol")
            if occ != row["occ_symbol"]:
                log.warning(
                    "SKIP %s — occSymbol changed (expected %s, found %s); re-verify by hand",
                    row["decision_id"], row["occ_symbol"], occ,
                )
                continue
            if decision.closed_at is not None:
                log.warning(
                    "SKIP %s (%s) — closed_at is already set (%s); this position has "
                    "since been closed by the (now-fixed) code, do not touch",
                    row["decision_id"], row["occ_symbol"], decision.closed_at,
                )
                continue
            if decision.fill_qty is not None:
                log.info(
                    "SKIP %s (%s) — fill_qty already set (%s); already backfilled or "
                    "healed by order_sync",
                    row["decision_id"], row["occ_symbol"], decision.fill_qty,
                )
                continue

            client_order_id = f"backfill-open-{row['decision_id']}"
            log.info(
                "%s %s: fill_qty=%s fill_avg_price=%s client_order_id=%s",
                "APPLY" if apply else "WOULD SET",
                row["occ_symbol"], row["qty"], row["avg_entry_price"], client_order_id,
            )
            if not apply:
                changed += 1
                continue

            await session.execute(
                text(
                    "UPDATE agent_decisions SET fill_qty = :qty, fill_avg_price = :price "
                    "WHERE id = :id AND fill_qty IS NULL AND closed_at IS NULL"
                ),
                {"qty": row["qty"], "price": row["avg_entry_price"], "id": row["decision_id"]},
            )
            stmt = (
                pg_insert(Order)
                .values(
                    id=uuid.uuid4(),
                    user_id=USER_ID,
                    broker_connection_id=BROKER_CONNECTION_ID,
                    agent_decision_id=row["decision_id"],
                    client_order_id=client_order_id,
                    broker_order_id=None,  # never captured — not fabricated
                    symbol=row["underlying"],
                    side="BUY",
                    qty=row["qty"],
                    order_type="LIMIT",
                    status="filled",
                    filled_qty=row["qty"],
                    avg_fill_price=row["avg_entry_price"],
                    filled_at=datetime.now(UTC),
                    is_paper=True,
                    is_option=True,
                    multiplier=100,
                    option_action="buy_to_open",
                )
                .on_conflict_do_nothing(constraint="uq_orders_client_order_id")
            )
            await session.execute(stmt)
            changed += 1

        if apply:
            await session.commit()
            log.info("COMMITTED %d row(s)", changed)
        else:
            log.info(
                "DRY RUN — %d row(s) would change. Re-run with --apply to write.", changed
            )

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write. Without this flag, only prints what would change.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(apply=args.apply)))
