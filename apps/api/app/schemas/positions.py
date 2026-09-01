"""Wire schemas for /api/v1/positions — open agent positions + close +
closed-position history.

One ``OpenPositionDto`` per open agent-managed decision (approved + not
closed) OR per unmanaged broker position. Carries the exit plan so the
mobile screen can show "who closes this and when" and offer a close-now
button — including an unmanaged row (``decision_id=None``), which closes
through the separate ``POST /positions/unmanaged/{symbol}/close`` route
instead of ``POST /positions/{decision_id}/close``. Both return this same
``ClosePositionResponse`` shape.

Includes rows still awaiting a fill (``status="pending_fill"``): an
approved proposal used to disappear from ``/approvals/pending`` the
instant it was decided and only reappear here once the broker filled it —
invisible in between, most visibly outside market hours, when an order
can sit accepted-but-unfilled for hours. There was no other surface that
listed it: ``/orders`` has no GET, so an approved order was nowhere in
the app until the market opened.

``ClosedPositionDto`` is the OTHER half of the position lifecycle — GET
/positions/history reads every decision this app ever saw both open AND
close, so the user can answer "where did my P&L actually come from" with
a per-trade list instead of a single account-level number. See its own
docstring for the one gap (unmanaged-position closes).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from app.schemas.base import CamelCaseModel


class OpenPositionDto(CamelCaseModel):
    # None for an unmanaged position — one that exists at the broker with
    # no agent decision behind it, so there is nothing to close *through*
    # the decision lifecycle.
    decision_id: str | None = None
    # False when the agent did not open this position (opened directly at
    # the broker, or before this deployment's decision history). Such a
    # position still counts against the account, so hiding it made
    # /account report open positions that /positions could not show.
    managed: bool = True
    # "pending_fill": the order was placed at the broker but hasn't filled
    # yet (common outside market hours) — there is no entry price, no
    # live mark, and nothing to close, only an order to watch. "open":
    # a real position, filled and live. Unmanaged rows are always "open"
    # — they exist at the broker BECAUSE they already filled.
    status: Literal["open", "pending_fill"] = "open"
    symbol: str
    side: Literal["BUY", "SELL"]
    # Derived from the entry proposal's own "direction", falling back to
    # side == "SELL" for rows that predate that field. Always populated —
    # never optional — so the mobile/desktop direction badges never guess.
    direction: Literal["long", "short"]
    qty: int
    avg_entry_price: float | None = None
    # Live mark from the latest reconciler snapshot, when available.
    last_price: float | None = None
    unrealized_pnl: float | None = None
    # Exit plan (the user approved this at entry).
    exit_mode: Literal["agent", "manual"]
    stop_loss: float | None = None
    target_price: float | None = None
    time_stop_days: int | None = None
    opened_at: datetime
    # ── Options facts (Phase A) — all optional, absent for an equity
    # position. Mirrors packages/broker/broker/types.py's Position
    # .is_option/.multiplier; contract_type/strike/expiry_date/occ_symbol
    # would be derived server-side from the OCC symbol (never parsed
    # client-side — see broker.types.OccSymbol.parse). NOT YET populated
    # for a real broker position — the service that builds this dto
    # (apps/api/app/services/orders/positions_service.py) is a separate
    # track's scope. Added here, purely additive, so the wire contract is
    # ready the moment that track wires population.
    is_option: bool = False
    contract_type: Literal["call", "put"] | None = None
    strike: float | None = None
    expiry_date: date | None = None
    occ_symbol: str | None = None
    multiplier: int = 1


class ClosePositionResponse(CamelCaseModel):
    # None for the symbol-keyed unmanaged-close response (see `symbol`
    # below) — there is no decision row to name.
    decision_id: str | None = None
    # Populated instead of decision_id when this response came from
    # POST /positions/unmanaged/{symbol}/close — a close with no agent
    # decision behind it at all. Exactly one of decision_id/symbol is set.
    symbol: str | None = None
    closed: bool
    # None on success; otherwise not_found / not_owner / already_closed /
    # no_open_position / close_in_flight / risk_vetoed.
    error: str | None = None
    detail: str | None = None


class ClosedPositionDto(CamelCaseModel):
    """One row in the closed-position history — GET /positions/history.

    Source is ``agent_decisions.closed_at IS NOT NULL``, so every row here
    was, at some point, an ``OpenPositionDto`` with ``managed=True``. A
    position that was NEVER tracked as open by this app (opened before this
    deployment's decision history, or opened directly at the broker and
    later closed via ``close_unmanaged_position_now``) has no
    ``AgentDecision`` row at all and cannot appear here — see
    ``positions_service.list_closed_positions`` for the exact gap and why
    it is currently unobserved (zero such closes exist in production as of
    2026-09-01), not merely undiscovered.
    """

    decision_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    direction: Literal["long", "short"]
    qty: int
    avg_entry_price: float | None = None
    # The broker's own fill price when a matching close Order row exists
    # (an agent close or an in-app user close both create one). Back-solved
    # from realized_pnl (see exit_price_source) for an `external_broker`
    # close, where the user exited directly at Alpaca and order_sync only
    # ever had an approximate mark for it — never a real fill.
    exit_price: float | None = None
    exit_price_source: Literal["order_fill", "estimated_from_pnl"] | None = None
    realized_pnl: float | None = None
    opened_at: datetime
    closed_at: datetime
    # agent_time / agent_signal / agent_expiry / option_take_profit /
    # option_stop_loss / option_trail_stop (position_manager's own closes) /
    # user_manual (a tap in this app) / external_broker (order_sync detected
    # the user closed it directly at Alpaca). None for a pre-migration row.
    close_reason: str | None = None
    # Whether the CLOSE was the position manager's own doing ('agent') or
    # not ('manual' — a user tap in this app, or an external broker close).
    # This is the exit plan's owner at the time it closed, mirroring
    # OpenPositionDto.exit_mode — it does not by itself distinguish
    # user_manual from external_broker; close_reason does that.
    exit_mode: Literal["agent", "manual"]
    approval_mode: str
    # ── Options facts — mirrors OpenPositionDto's identical block.
    is_option: bool = False
    contract_type: Literal["call", "put"] | None = None
    strike: float | None = None
    expiry_date: date | None = None
    occ_symbol: str | None = None
    multiplier: int = 1


class ClosedPositionListResponse(CamelCaseModel):
    positions: list[ClosedPositionDto]
    total: int
    limit: int
    offset: int
