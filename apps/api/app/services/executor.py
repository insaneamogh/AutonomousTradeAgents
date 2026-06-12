"""Executor service — turns an approved proposal into a real broker order.

Flow inside ``execute_proposal``:

  1. Look up the proposal in the user's pending queue (404 if missing,
     409 if already executed).
  2. Open the broker via ``with_broker_client(user_id)`` — decrypts the
     access token, yields whichever broker the user has connected
     (Alpaca or Zerodha; ``BROKER_PREFERENCE`` env breaks ties).
  3. **Live-trading gate** — non-paper connections (Alpaca live, all of
     Zerodha) are refused with the named rule ``live_trading_disabled``
     unless ``LIVE_TRADING_ENABLED=1``. Deterministic, env-driven, audited.
  4. Fetch a fresh ``RiskContext`` (account equity, open positions, halt
     state) — proposals can age between draft and approval, so we
     re-evaluate against the latest snapshot.
  5. Call ``engine.risk.evaluate`` again. If the answer changed since the
     council drafted (drawdown tripped, new same-day day-trade, etc.) we
     refuse here. The deterministic chain is the last line of defense.
  6. Call ``place_order``. ``client_order_id`` is derived from the
     proposal id so retries are idempotent — natively at Alpaca's side
     (~24h dedupe), tag-emulated within the day at Zerodha's.
  7. Persist the ``Order`` (with link to the originating agent_decision)
     + return the camelCase DTO.

Out of scope this round:
  - Fill polling / partial-fill reconciliation (Phase 4 hardening).
  - Real Postgres ``orders`` persistence — for the Postgres backend this
    lands in a follow-on; today we return the in-memory ``Order`` DTO.
    Mobile uses TanStack Query's optimistic update so the UX is fine.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

# ``broker.types`` is pure-stdlib + dataclasses — safe to import at module
# load. ``broker.alpaca`` pulls in alpaca-py which may not be uv-sync'd
# yet, so the AlpacaBroker reference is type-only here + via the lazy
# import in app.services.broker_use.
from broker.types import OrderRequest, OrderType, Side, TimeInForce
from engine.risk import (
    DbRiskState,
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    RiskProposal,
    Side as RiskSide,
    evaluate,
    load_db_risk_state,
    market_of,
    sector_for,
)

from app.schemas.approvals import ApprovalProposalDto
from app.schemas.orders import ExecuteResponse, OrderResponse
from app.services.broker_use import (
    BrokerUnavailableError,
    with_broker_client,
)
from app.services.broker_store import BrokerConnectionRecord
from app.services.order_store import (
    persist_order_result,
    persist_order_submit,
)
from app.services.paper_broker import get_paper_store, trading_mode
from app.services.store import Store, get_store

if TYPE_CHECKING:
    from broker.base import BrokerInterface

logger = logging.getLogger("api.executor")


def _live_trading_enabled() -> bool:
    """Single switch for real-money orders. Default OFF — paper only."""
    return os.environ.get("LIVE_TRADING_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class ExecutorError(Exception):
    """User-visible executor failure. Routers translate to 4xx/5xx."""


class ProposalNotFound(ExecutorError):
    pass


class ProposalAlreadyExecuted(ExecutorError):
    pass


# ─────────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────────


async def execute_proposal(
    *,
    user_id: str,
    proposal_id: str,
    store: Store | None = None,
    risk_caps: RiskCaps | None = None,
    exit_mode: str = "agent",
) -> ExecuteResponse:
    """Resolve → re-risk → place → persist. Idempotent on ``proposal_id``.

    ``exit_mode`` is the user's per-position choice from the approval card:
      - 'agent'  → bracket legs (stop + target) ride with the entry at the
        broker, and the position manager may time-stop / early-exit it.
      - 'manual' → no protective legs, no agent exits; the user owns the
        close entirely.

    Routing: paper mode prefers the user's REAL Alpaca paper account (real
    market fills, working brackets — the whole reason v1 is Alpaca-only).
    The in-memory simulator only remains as the no-broker-connected dev
    fallback. Live mode requires a connection, full stop.
    """
    s = store or get_store()

    proposal = await _find_pending_proposal(s, proposal_id)
    if proposal is None:
        raise ProposalNotFound(f"No pending proposal with id={proposal_id!r}")

    if exit_mode not in ("agent", "manual"):
        raise ExecutorError(f"exit_mode must be 'agent' or 'manual', got {exit_mode!r}")

    if trading_mode() == "paper":
        try:
            return await _execute_via_broker(
                s, user_id=user_id, proposal=proposal,
                risk_caps=risk_caps, exit_mode=exit_mode,
            )
        except BrokerUnavailableError as exc:
            logger.info(
                "executor[paper]: no usable broker connection (%s) — "
                "falling back to the in-memory simulator", exc,
            )
            return await _execute_paper(
                store=s, user_id=user_id, proposal=proposal,
                risk_caps=risk_caps, exit_mode=exit_mode,
            )

    return await _execute_via_broker(
        s, user_id=user_id, proposal=proposal, risk_caps=risk_caps, exit_mode=exit_mode
    )


async def _execute_via_broker(
    s: Store,
    *,
    user_id: str,
    proposal: ApprovalProposalDto,
    risk_caps: RiskCaps | None,
    exit_mode: str,
) -> ExecuteResponse:
    proposal_id = proposal.id
    async with with_broker_client(user_id) as (broker, conn):
        # 0. Live-trading gate. Alpaca-live and all Zerodha connections are
        # real money — refuse unless the operator deliberately flipped the
        # env. Surfaced as a named deterministic rule for the audit trail.
        if not conn.is_paper and not _live_trading_enabled():
            logger.warning(
                "executor: live order BLOCKED proposal=%s user=%s broker=%s — "
                "LIVE_TRADING_ENABLED is not set",
                proposal_id, user_id, conn.broker,
            )
            return ExecuteResponse(
                order=None,
                risk_blocked=True,
                risk_reason=(
                    f"{conn.broker} connection is live (real money) and "
                    "LIVE_TRADING_ENABLED is not set on the API."
                ),
                risk_veto_rule="live_trading_disabled",
                informational_flags=[],
            )

        # 1. Re-evaluate risk against the BROKER's view of the world,
        # merged with OUR halt/PDT state. Fails closed if state is unreadable.
        risk_ctx = await _build_risk_context(broker, user_id=user_id)
        risk_decision = _re_run_risk(proposal, risk_ctx, risk_caps)

        if not risk_decision.approved:
            logger.info(
                "executor: risk re-eval BLOCKED proposal=%s user=%s rule=%s reason=%s",
                proposal_id, user_id, risk_decision.veto_rule, risk_decision.reason,
            )
            return ExecuteResponse(
                order=None,
                risk_blocked=True,
                risk_reason=risk_decision.reason,
                risk_veto_rule=risk_decision.veto_rule,
                informational_flags=list(risk_decision.informational_flags),
            )

        adjusted_qty = (
            risk_decision.adjusted_qty
            if risk_decision.adjusted_qty is not None
            else proposal.qty
        )

        # 2. Persist intent FIRST (audit chain: decision → order), then place.
        # The client_order_id is the proposal id — Alpaca de-dupes on it for
        # ~24h, so a retry of this whole function won't double-submit, and
        # the DB insert is ON CONFLICT DO NOTHING on the same key.
        client_order_id = _client_order_id_for(proposal.id)
        try:
            order_row_id = await persist_order_submit(
                user_id=user_id,
                broker_connection_id=conn.id,
                proposal=proposal,
                client_order_id=client_order_id,
                qty=adjusted_qty,
                is_paper=conn.is_paper,
            )
        except ExecutorError:
            raise
        except Exception as exc:  # noqa: BLE001 — unrecorded order = audit break
            raise ExecutorError(
                "Execution blocked: order could not be recorded before "
                "submission — failing closed. See server logs."
            ) from exc

        # Bracket legs: agent-managed BUYs carry the user-approved exit plan
        # to the broker (OCO stop + target survive our downtime). GTC so
        # the children outlive the entry day — this is a swing product.
        use_bracket = (
            exit_mode == "agent"
            and proposal.side == "BUY"
            and proposal.stop_loss is not None
            and proposal.target_price is not None
        )

        try:
            order = await broker.place_order(
                OrderRequest(
                    symbol=proposal.symbol,
                    side=Side(proposal.side),
                    qty=adjusted_qty,
                    order_type=OrderType.MARKET if proposal.order_type == "MARKET" else OrderType.LIMIT,
                    limit_price=proposal.limit_price,
                    time_in_force=TimeInForce.GTC if use_bracket else TimeInForce.DAY,
                    client_order_id=client_order_id,
                    take_profit_price=proposal.target_price if use_bracket else None,
                    stop_loss_price=proposal.stop_loss if use_bracket else None,
                )
            )
        except Exception:
            # Row stays status='pending' on purpose — a transient failure is
            # retryable: the retry reuses the same client_order_id, lands on
            # the existing broker order if one was actually accepted, and the
            # order poller reconciles true broker-side rejections into
            # status='rejected'. Marking rejected here would kill the retry.
            logger.exception(
                "executor: broker.place_order failed for %s — row %s stays pending",
                proposal_id, order_row_id,
            )
            raise

        if order_row_id is not None:
            try:
                await persist_order_result(order_row_id=order_row_id, broker_order=order)
            except Exception:  # noqa: BLE001 — order placed; poller heals the row
                logger.exception(
                    "executor: persist_order_result failed for %s — order poller will reconcile",
                    order_row_id,
                )

    logger.info(
        "executor: placed order proposal=%s user=%s symbol=%s qty=%d (trimmed_from=%d) broker_order_id=%s",
        proposal_id, user_id, proposal.symbol,
        adjusted_qty, proposal.qty, order.broker_order_id,
    )

    # 3. Best-effort: mark the proposal "approved" so it leaves the pending
    # list, carrying the user's exit-mode choice onto the decision row.
    try:
        await s.decide(proposal_id, "approved", exit_mode=exit_mode)
    except Exception as exc:  # noqa: BLE001
        # The order is already placed — don't fail the route just because
        # the proposal-state write hiccupped. Reconciler will catch up.
        logger.warning("executor: post-place decide() failed for %s — %s", proposal_id, exc)

    return ExecuteResponse(
        order=OrderResponse(
            id=str(order_row_id) if order_row_id is not None else str(uuid.uuid4()),
            proposal_id=proposal_id,
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id or _client_order_id_for(proposal.id),
            symbol=order.symbol,
            side=order.side.value if hasattr(order.side, "value") else str(order.side),
            qty=order.qty,
            requested_qty=proposal.qty,
            order_type=proposal.order_type,
            limit_price=proposal.limit_price,
            status=order.status.value if hasattr(order.status, "value") else str(order.status),
            filled_qty=order.filled_qty,
            avg_fill_price=order.avg_fill_price,
            is_paper=conn.is_paper,
            submitted_at=order.submitted_at,
        ),
        risk_blocked=False,
        risk_reason="risk re-eval passed",
        risk_veto_rule=None,
        informational_flags=list(risk_decision.informational_flags),
    )


# ─────────────────────────────────────────────────────────────────────
# Paper execution — simulated fill, real risk chain
# ─────────────────────────────────────────────────────────────────────


async def _execute_paper(
    *,
    store: Store,
    user_id: str,
    proposal: ApprovalProposalDto,
    risk_caps: RiskCaps | None,
    exit_mode: str = "agent",
) -> ExecuteResponse:
    """In-memory simulated execution — the NO-BROKER-CONNECTED fallback.

    Risk re-eval against the paper portfolio, then an immediate fill at the
    proposal's limit/last price. Connected users get the real Alpaca paper
    account instead (real market fills, working brackets); this simulator
    can't hold bracket children, which is surfaced as an informational flag.
    Idempotent on the proposal-derived client_order_id like real brokers.
    """
    market = market_of(proposal.symbol)
    pf = get_paper_store().portfolio(user_id, market)

    last_price = proposal.estimated_notional / max(proposal.qty, 1)
    pf.mark(proposal.symbol, last_price)

    # Halt + PDT state applies to paper exactly like live — the whole point
    # of the paper phase is exercising the identical rule chain. Fails
    # closed on a DB error like the live path.
    db_state = await _load_db_state_or_fail(user_id, pf.equity())

    risk_ctx = RiskContext(
        account_equity=pf.equity(),
        cash=pf.cash,
        buying_power=pf.cash,
        open_positions=tuple(
            PortfolioPosition(
                symbol=h.symbol,
                qty=h.qty,
                avg_entry_price=h.avg_entry_price,
                market_value=h.qty * h.mark,
                sector=sector_for(h.symbol),
            )
            for h in pf.holdings.values()
        ),
        day_trades_last_5d=db_state.day_trades_last_5d,
        recent_losing_closes=db_state.recent_losing_closes,
        daily_pnl=db_state.daily_pnl,
        daily_pnl_pct=db_state.daily_pnl_pct,
        drawdown_halted=db_state.drawdown_halted,
        drawdown_halt_reason=db_state.drawdown_halt_reason,
        drawdown_halted_at=db_state.drawdown_halted_at,
    )
    risk_decision = _re_run_risk(proposal, risk_ctx, risk_caps)

    if not risk_decision.approved:
        logger.info(
            "executor[paper]: risk BLOCKED proposal=%s user=%s rule=%s",
            proposal.id, user_id, risk_decision.veto_rule,
        )
        return ExecuteResponse(
            order=None,
            risk_blocked=True,
            risk_reason=risk_decision.reason,
            risk_veto_rule=risk_decision.veto_rule,
            informational_flags=list(risk_decision.informational_flags),
        )

    adjusted_qty = (
        risk_decision.adjusted_qty
        if risk_decision.adjusted_qty is not None
        else proposal.qty
    )
    fill_price = proposal.limit_price or last_price

    fill = pf.fill(
        symbol=proposal.symbol,
        side=proposal.side,
        qty=adjusted_qty,
        price=fill_price,
        proposal_id=proposal.id,
        client_order_id=_client_order_id_for(proposal.id),
    )

    logger.info(
        "executor[paper]: filled proposal=%s user=%s %s %d %s @ %.2f (%s book)",
        proposal.id, user_id, fill.side, fill.qty, fill.symbol, fill.price, market,
    )

    try:
        await store.decide(proposal.id, "approved", exit_mode=exit_mode)
    except Exception as exc:  # noqa: BLE001 — fill already booked; don't fail the route
        logger.warning("executor[paper]: post-fill decide() failed — %s", exc)

    flags = list(risk_decision.informational_flags) + ["paper_mode"]
    if exit_mode == "agent" and proposal.stop_loss is not None:
        # The in-memory book can't hold OCO children. Connected Alpaca
        # paper accounts get real brackets — this flag is the nudge.
        flags.append("no_bracket_in_memory")

    return ExecuteResponse(
        order=OrderResponse(
            id=str(uuid.uuid4()),
            proposal_id=proposal.id,
            broker_order_id=fill.id,
            client_order_id=fill.client_order_id or _client_order_id_for(proposal.id),
            symbol=fill.symbol,
            side=fill.side,
            qty=fill.qty,
            requested_qty=proposal.qty,
            order_type=proposal.order_type,
            limit_price=proposal.limit_price,
            status="filled",
            filled_qty=fill.qty,
            avg_fill_price=fill.price,
            is_paper=True,
            submitted_at=fill.filled_at,
        ),
        risk_blocked=False,
        risk_reason="paper fill — simulated, no broker order placed",
        risk_veto_rule=None,
        informational_flags=flags,
    )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


async def _find_pending_proposal(store: Store, proposal_id: str) -> ApprovalProposalDto | None:
    """Locate the proposal in the user's pending queue.

    Returns None if it's not there. The caller raises ProposalNotFound —
    we don't here because ProposalAlreadyExecuted (out-of-pending because
    it was already approved) needs the same code path but a different
    error, and we don't yet track that distinction in the Store.
    """
    for p in await store.list_pending():
        if p.id == proposal_id:
            return p
    return None


def _postgres_active() -> bool:
    v = os.environ.get("USE_POSTGRES")
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


async def _load_db_state_or_fail(user_id: str, current_equity: float | None) -> DbRiskState:
    """Halt + PDT + daily-drawdown state from Postgres — FAIL CLOSED.

    The execution moment is the one place the system must never run blind:
    if the DB-owned risk state can't be read, we refuse to place the order
    rather than evaluating with no-halt/no-PDT defaults.

    MockStore dev mode (USE_POSTGRES unset) has no halt/PDT tables at all —
    returns defaults with a loud log so a misconfigured prod box is visible.
    """
    if not _postgres_active():
        logger.warning(
            "executor: USE_POSTGRES is off — halt/PDT state unavailable, "
            "using permissive dev defaults. NEVER run live trading this way."
        )
        return DbRiskState()
    try:
        from engine.db.session import async_session_factory

        return await load_db_risk_state(
            async_session_factory(), user_id=user_id, current_equity=current_equity
        )
    except Exception as exc:  # noqa: BLE001 — any failure here fails closed
        raise ExecutorError(
            "Execution blocked: halt/PDT risk state could not be loaded — "
            "failing closed. See server logs."
        ) from exc


async def _build_risk_context(broker: "BrokerInterface", *, user_id: str) -> RiskContext:
    """Broker = freshest equity/positions; Postgres = halt + PDT state.

    We deliberately don't pull equity/positions from ``positions_snapshot``
    — the reconciler's snapshot can be up to 30s stale, and the
    order-placement moment is exactly when we want the freshest read. The
    DB still owns what the broker can't tell us: circuit-breaker status,
    PDT day-trade count, wash-sale history, and today's drawdown baseline.
    """
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

    db_state = await _load_db_state_or_fail(user_id, equity)

    return RiskContext(
        account_equity=equity,
        cash=cash,
        buying_power=buying_power,
        open_positions=positions,
        day_trades_last_5d=db_state.day_trades_last_5d,
        recent_losing_closes=db_state.recent_losing_closes,
        daily_pnl=db_state.daily_pnl,
        daily_pnl_pct=db_state.daily_pnl_pct,
        drawdown_halted=db_state.drawdown_halted,
        drawdown_halt_reason=db_state.drawdown_halt_reason,
        drawdown_halted_at=db_state.drawdown_halted_at,
    )


def _re_run_risk(
    proposal: ApprovalProposalDto,
    context: RiskContext,
    caps: RiskCaps | None,
) -> "RiskDecisionLike":
    """Translate ApprovalProposalDto → RiskProposal + call evaluate.

    The mapping is lossy on purpose — risk engine doesn't care about
    bull/bear narrative, just the trade shape.
    """
    last_price = proposal.estimated_notional / max(proposal.qty, 1)
    risk_proposal = RiskProposal(
        symbol=proposal.symbol,
        side=RiskSide(proposal.side),
        qty=proposal.qty,
        estimated_notional=proposal.estimated_notional,
        last_price=last_price,
        confidence=proposal.conviction_level / 5.0,  # 1-5 → 0..1
    )
    return evaluate(risk_proposal, context, caps)


def _client_order_id_for(proposal_id: str) -> str:
    """Stable per-proposal client order id. Alpaca de-dupes on this for
    ~24h, so a retry of execute_proposal with the same proposal lands
    on the EXISTING order, not a duplicate.
    """
    # Alpaca's max length is 128 chars; our proposal ids fit comfortably.
    return f"agent-exec-{proposal_id}"


# Forward-decl alias for the return type — RiskDecision is a frozen
# dataclass; we re-export the name here to keep _re_run_risk's signature
# readable without dragging the import into the public surface.
from engine.risk import RiskDecision as RiskDecisionLike  # noqa: E402,F401
