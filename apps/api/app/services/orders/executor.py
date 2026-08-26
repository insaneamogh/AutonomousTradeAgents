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
  5. Call ``engine.risk.evaluate`` again with the SAME inputs the council
     used — its confidence, its specialist scores, and the intraday flags
     (see ``load_risk_inputs``). Without them ``pdt_block``,
     ``mis_square_off_block`` and ``min_specialist_avg_score`` cannot fire
     at the one moment that matters. The deterministic chain is the last
     line of defense, so it must not be weaker here than at drafting.
  6. Claim the decision row (``user_response`` NULL → 'executing', a
     compare-and-swap). Exactly one concurrent approval may place an
     order; the loser is refused by name.
  7. Call ``place_order``. ``client_order_id`` is derived from the
     proposal id so retries are idempotent — natively at Alpaca's side
     (~24h dedupe), tag-emulated within the day at Zerodha's.
  8. Persist the ``Order`` (with link to the originating agent_decision),
     convert the claim into the final 'approved' state, and return the
     camelCase DTO.

Out of scope this round:
  - Fill polling / partial-fill reconciliation (Phase 4 hardening).
  - Real Postgres ``orders`` persistence — for the Postgres backend this
    lands in a follow-on; today we return the in-memory ``Order`` DTO.
    Mobile uses TanStack Query's optimistic update so the UX is fine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.schemas.approvals import ApprovalProposalDto
from app.schemas.orders import ExecuteResponse
from app.services.broker.broker_use import (
    BrokerUnavailableError,
    with_broker_client,
)
from app.services.council.store import Store, get_store
from app.services.orders.execute_response import build_execute_response
from app.services.orders.execution_claim import (
    claim_decision_for_execution,
    finalize_execution_claim,
    release_execution_claim,
)
from app.services.orders.live_trading_gate import check_live_trading_gate
from app.services.orders.order_store import persist_order_result, persist_order_submit
from app.services.orders.paper_broker import get_paper_store, trading_mode

# ``broker.types`` is pure-stdlib + dataclasses — safe to import at module
# load. ``broker.alpaca`` pulls in alpaca-py which may not be uv-sync'd
# yet, so the AlpacaBroker reference is type-only here + via the lazy
# import in app.services.broker.broker_use.
from broker.types import OrderRequest, OrderType, Side, TimeInForce
from engine.env import env_flag
from engine.risk import (
    DbRiskState,
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    RiskProposal,
    SpecialistScore,
    evaluate,
    load_db_risk_state,
    market_of,
    sector_for,
)
from engine.risk import (
    Side as RiskSide,
)

if TYPE_CHECKING:
    from broker.base import BrokerInterface

logger = logging.getLogger("api.executor")


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

    # Scope to the caller — they can only execute their OWN pending proposal.
    proposal = await _find_pending_proposal(s, proposal_id, user_id)
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
        # 0. Live-trading gate — see live_trading_gate.py for the two-key
        # rule (operator env + per-connection consent). Either missing →
        # refuse, named for audit.
        blocked = check_live_trading_gate(conn, proposal_id=proposal_id, user_id=user_id)
        if blocked is not None:
            return blocked

        # 1. Re-evaluate risk against the BROKER's view of the world,
        # merged with OUR halt/PDT state. Fails closed if state is unreadable.
        risk_ctx = await _build_risk_context(broker, user_id=user_id)
        risk_inputs = await load_risk_inputs(proposal, user_id=user_id)
        risk_decision = _re_run_risk(proposal, risk_ctx, risk_caps, risk_inputs)

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

        # 2. Bracket legs: agent-managed entries carry the user-approved exit
        # plan to the broker (OCO stop + target survive our downtime). GTC
        # so the children outlive the entry day — this is a swing product.
        # A SELL-to-open (short) needs and gets a bracket exactly like a
        # BUY-to-open (long) — the inverted geometry (stop above entry) is
        # already correct by the time it reaches here (engine.sizing.atr).
        use_bracket = (
            exit_mode == "agent"
            and proposal.stop_loss is not None
            and proposal.target_price is not None
        )
        if exit_mode == "agent" and not use_bracket:
            if not conn.is_paper:
                # Real money: REFUSE rather than silently demote. Without
                # broker-side legs the position manager's time-stop is the
                # only exit, and it dies with our process — the approval
                # card promised a stop the broker would honor. This applies
                # to a short exactly as much as a long: an unprotected live
                # short has UNBOUNDED downside with nothing watching it.
                logger.warning(
                    "executor: live agent-mode %s %s BLOCKED — no "
                    "stop_loss/target_price to bracket with.",
                    proposal.side, proposal.symbol,
                )
                return ExecuteResponse(
                    order=None,
                    risk_blocked=True,
                    risk_reason=(
                        "Agent-managed entry has no stop-loss/target to place "
                        "as broker-side protective legs. Live orders are not "
                        "placed unprotected — re-run the council, or approve "
                        "with exit_mode='manual' to own the close yourself."
                    ),
                    risk_veto_rule="bracket_legs_required",
                    informational_flags=list(risk_decision.informational_flags),
                )
            # Paper: warn only, so demos on an incomplete proposal still run.
            logger.warning(
                "executor[paper]: agent-mode %s %s placed WITHOUT a bracket "
                "(missing stop_loss/target_price) — broker-side protection "
                "absent; relying on the time-stop only.",
                proposal.side, proposal.symbol,
            )

        # 3. Claim the proposal BEFORE touching the broker. Two concurrent
        # approvals both found it pending; exactly one may place an order.
        # The loser used to fall through, get order_row_id=None from the
        # ON CONFLICT DO NOTHING insert, skip persist_order_result, and
        # return a fabricated order id matching no row anywhere.
        if not await claim_decision_for_execution(
            user_id=user_id, proposal_id=proposal_id
        ):
            logger.warning(
                "executor: concurrent execution claim lost for proposal=%s "
                "user=%s — refusing the duplicate approval",
                proposal_id, user_id,
            )
            return ExecuteResponse(
                order=None,
                risk_blocked=True,
                risk_reason=(
                    "Another approval for this proposal is already executing. "
                    "No second order was placed."
                ),
                risk_veto_rule="concurrent_execution_claim",
                informational_flags=[],
            )

        # 4. Persist intent (audit chain: decision → order), then place.
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
            await release_execution_claim(user_id=user_id, proposal_id=proposal_id)
            raise
        except Exception as exc:  # noqa: BLE001 — unrecorded order = audit break
            await release_execution_claim(user_id=user_id, proposal_id=proposal_id)
            raise ExecutorError(
                "Execution blocked: order could not be recorded before "
                "submission — failing closed. See server logs."
            ) from exc

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
            # Release the claim for the same reason.
            await release_execution_claim(user_id=user_id, proposal_id=proposal_id)
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

    # 5. Best-effort: mark the proposal "approved" so it leaves the pending
    # list, carrying the user's exit-mode choice onto the decision row.
    # ``finalize_execution_claim`` converts the 'executing' claim we hold;
    # ``Store.decide`` only matches user_response IS NULL, so it is the
    # right call only when no decision row was claimed (MockStore mode).
    try:
        finalized = await finalize_execution_claim(
            user_id=user_id,
            proposal_id=proposal_id,
            outcome="approved",
            exit_mode=exit_mode,
        )
        if not finalized:
            await s.decide(proposal_id, "approved", user_id=user_id, exit_mode=exit_mode)
    except Exception as exc:  # noqa: BLE001
        # The order is already placed — don't fail the route just because
        # the proposal-state write hiccupped. Reconciler will catch up.
        logger.warning("executor: post-place decide() failed for %s — %s", proposal_id, exc)

    return build_execute_response(
        # No DB row means Postgres is inactive (dev). Use the
        # client_order_id — a real, stable identifier the broker also
        # knows — instead of a fabricated UUID that matches nothing.
        order_id=str(order_row_id) if order_row_id is not None else client_order_id,
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
        risk_reason="risk re-eval passed",
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
    risk_inputs = await load_risk_inputs(proposal, user_id=user_id)
    risk_decision = _re_run_risk(proposal, risk_ctx, risk_caps, risk_inputs)

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

    # Same one-winner claim as the broker path — the simulator books a real
    # position, so a double-approve here double-fills the paper portfolio.
    if not await claim_decision_for_execution(
        user_id=user_id, proposal_id=proposal.id
    ):
        logger.warning(
            "executor[paper]: concurrent execution claim lost for proposal=%s",
            proposal.id,
        )
        return ExecuteResponse(
            order=None,
            risk_blocked=True,
            risk_reason=(
                "Another approval for this proposal is already executing. "
                "No second fill was booked."
            ),
            risk_veto_rule="concurrent_execution_claim",
            informational_flags=["paper_mode"],
        )

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
        finalized = await finalize_execution_claim(
            user_id=user_id,
            proposal_id=proposal.id,
            outcome="approved",
            exit_mode=exit_mode,
        )
        if not finalized:
            await store.decide(
                proposal.id, "approved", user_id=user_id, exit_mode=exit_mode
            )
    except Exception as exc:  # noqa: BLE001 — fill already booked; don't fail the route
        logger.warning("executor[paper]: post-fill decide() failed — %s", exc)

    flags = list(risk_decision.informational_flags) + ["paper_mode"]
    if exit_mode == "agent" and proposal.stop_loss is not None:
        # The in-memory book can't hold OCO children. Connected Alpaca
        # paper accounts get real brackets — this flag is the nudge.
        flags.append("no_bracket_in_memory")
        logger.warning(
            "executor[paper]: agent-mode %s filled on the IN-MEMORY book — "
            "no broker-side bracket exists; the position-manager time-stop is "
            "the only exit. Connect an Alpaca paper account for real brackets.",
            proposal.symbol,
        )

    return build_execute_response(
        # The simulated book's fill id IS the order's identity here —
        # there is no orders row to point at, and a random UUID would
        # match nothing on either side.
        order_id=fill.id,
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
        risk_reason="paper fill - simulated, no broker order placed",
        informational_flags=flags,
    )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


async def _find_pending_proposal(
    store: Store, proposal_id: str, user_id: str | None = None
) -> ApprovalProposalDto | None:
    """Locate the proposal in the user's pending queue.

    Returns None if it's not there. The caller raises ProposalNotFound —
    we don't here because ProposalAlreadyExecuted (out-of-pending because
    it was already approved) needs the same code path but a different
    error, and we don't yet track that distinction in the Store.
    """
    for p in await store.list_pending(user_id):
        if p.id == proposal_id:
            return p
    return None


async def _load_db_state_or_fail(user_id: str, current_equity: float | None) -> DbRiskState:
    """Halt + PDT + daily-drawdown state from Postgres — FAIL CLOSED.

    The execution moment is the one place the system must never run blind:
    if the DB-owned risk state can't be read, we refuse to place the order
    rather than evaluating with no-halt/no-PDT defaults.

    MockStore dev mode (USE_POSTGRES unset) has no halt/PDT tables at all —
    returns defaults with a loud log so a misconfigured prod box is visible.
    """
    if not env_flag("USE_POSTGRES"):
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


@dataclass(frozen=True)
class RiskInputs:
    """Everything the risk chain needs that the DTO doesn't carry.

    Assembled by ``load_risk_inputs`` from the originating
    ``agent_decisions`` row + the orders table. Defaults are the
    conservative "we know nothing" position, not a permissive one: an
    absent council confidence falls back to conviction (logged), and the
    intraday flags stay False only when nothing says otherwise.
    """

    council_confidence: float | None = None
    specialists: tuple[SpecialistScore, ...] = ()
    closes_intraday_position: bool = False
    is_intraday: bool = False


def _flag_from(source: dict[str, object], *names: str) -> bool | None:
    for name in names:
        if name in source:
            return bool(source[name])
    return None


async def load_risk_inputs(
    proposal: ApprovalProposalDto, *, user_id: str
) -> RiskInputs:
    """Rebuild the council's risk inputs for the execution-time re-run.

    Without this the executor's re-check was strictly WEAKER than the
    council's: ``pdt_block`` (FINRA), ``mis_square_off_block`` and
    ``min_specialist_avg_score`` could never fire at the moment an order
    actually goes to the broker, and the confidence floor was checked
    against ``conviction/5`` rather than the number the council emitted.
    """
    from app.services.orders.decision_risk import had_same_day_entry, load_decision_risk_row

    row = await load_decision_risk_row(proposal_id=proposal.id, user_id=user_id)
    stored = row.proposal if row is not None else {}

    confidence = row.council_confidence if row is not None else None
    if confidence is None and row is not None:
        confidence = row.judge_confidence
    if confidence is None:
        logger.warning(
            "executor: no council confidence recorded for proposal=%s — "
            "falling back to conviction_level/5. The confidence floor is "
            "being checked against a different quantity than at drafting.",
            proposal.id,
        )

    specialists = tuple(
        SpecialistScore(name=name, score=score, confidence=conf)
        for name, score, conf in (row.specialists if row is not None else ())
    )

    # Explicit flags on the stored proposal win; otherwise derive. A SELL of
    # a name we bought earlier in the same NY session IS a day trade.
    closes_intraday = _flag_from(
        stored, "closesIntradayPosition", "closes_intraday_position"
    )
    if closes_intraday is None:
        closes_intraday = proposal.side == "SELL" and await had_same_day_entry(
            user_id=user_id, symbol=proposal.symbol
        )

    is_intraday = _flag_from(stored, "isIntraday", "is_intraday") or False

    return RiskInputs(
        council_confidence=confidence,
        specialists=specialists,
        closes_intraday_position=bool(closes_intraday),
        is_intraday=bool(is_intraday),
    )


def _re_run_risk(
    proposal: ApprovalProposalDto,
    context: RiskContext,
    caps: RiskCaps | None,
    inputs: RiskInputs | None = None,
) -> "RiskDecisionLike":
    """Translate ApprovalProposalDto → RiskProposal + call evaluate.

    The mapping is lossy on purpose — the risk engine doesn't care about
    bull/bear narrative, just the trade shape — but it must not be lossy
    about anything a RULE reads. ``inputs`` carries the council fields the
    DTO drops (confidence, specialist scores) and the intraday flags the
    regulatory rules gate on.
    """
    inputs = inputs or RiskInputs()
    last_price = proposal.estimated_notional / max(proposal.qty, 1)
    confidence = (
        inputs.council_confidence
        if inputs.council_confidence is not None
        # Conviction (1-5, "how big a bet") is NOT the council's confidence
        # ("how likely to work") — the drafter emits them separately. Legacy
        # rows predate persisting the real value; load_risk_inputs logs it.
        else proposal.conviction_level / 5.0
    )
    risk_proposal = RiskProposal(
        symbol=proposal.symbol,
        side=RiskSide(proposal.side),
        qty=proposal.qty,
        estimated_notional=proposal.estimated_notional,
        last_price=last_price,
        confidence=confidence,
        closes_intraday_position=inputs.closes_intraday_position,
        is_intraday=inputs.is_intraday,
        # Short-side inputs. Without these, shortable_check/short_requires_stop
        # veto EVERY short unconditionally at this last-line-of-defense
        # check (both read None as "unknown" and fail closed) regardless of
        # whether ALLOW_SHORTS is on and the council already cleared it.
        stop_price=proposal.stop_loss,
        shortable=proposal.shortable,
        easy_to_borrow=proposal.easy_to_borrow,
    )
    return evaluate(risk_proposal, context, caps, specialists=inputs.specialists)


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
