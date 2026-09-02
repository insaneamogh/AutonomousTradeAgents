"""Position-manager exit-condition tests — pure logic, mocked session.

The broker-touching close path follows the executor's already-tested
plumbing; what must be pinned here is WHEN the agent decides to close:

  - time stop fires at the proposal's disclosed horizon, not before
  - a newer council SELL on the same symbol fires the signal exit
  - manual-mode positions are never selected (query-level, asserted via
    the worker's filter in integration; here we pin the per-decision rule)
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.orders import position_manager as position_manager_mod
from app.services.orders.position_manager import (
    _close_position,
    _close_unmanaged_position,
    _exit_reason,
    _has_in_flight_close,
    _has_in_flight_unmanaged_close,
    _option_exit_peak_update_stmt,
    _persist_option_exit_peak,
    _ratchet_outcome_for,
    close_unmanaged_position_now,
    manage_positions_for_user,
)
from broker.types import Side
from engine.options.exits import RatchetOutcome
from engine.risk import RiskCaps

NOW = datetime(2026, 6, 12, 15, 0, tzinfo=UTC)


def _decision(*, days_held: int, time_stop_days: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        symbol="NVDA",
        horizon="short",
        proposal={"timeStopDays": time_stop_days},
        user_responded_at=NOW - timedelta(days=days_held),
        triggered_at=NOW - timedelta(days=days_held),
    )


def _session(newer_sell_exists: bool) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(
        return_value=uuid.uuid4() if newer_sell_exists else None
    )
    session.execute = AsyncMock(return_value=result)
    return session


async def test_time_stop_fires_at_horizon() -> None:
    reason = await _exit_reason(_session(False), _decision(days_held=5), NOW)
    assert reason == "agent_time"


async def test_no_exit_before_horizon_without_signal() -> None:
    reason = await _exit_reason(_session(False), _decision(days_held=2), NOW)
    assert reason is None


async def test_newer_council_sell_fires_signal_exit() -> None:
    reason = await _exit_reason(_session(True), _decision(days_held=2), NOW)
    assert reason == "agent_signal"


async def test_time_stop_wins_over_signal_check() -> None:
    """At horizon, the time stop is reported even if a signal also exists —
    the labels matter for the audit trail."""
    reason = await _exit_reason(_session(True), _decision(days_held=9), NOW)
    assert reason == "agent_time"


async def test_old_proposals_without_time_stop_use_horizon_fallback() -> None:
    decision = _decision(days_held=5)
    decision.proposal = {}  # pre-0009 proposal shape
    reason = await _exit_reason(_session(False), decision, NOW)
    assert reason == "agent_time"  # 'short' horizon → 5d fallback


# ── Premium exits (options only) ──────────────────────────────────────


def _option_decision(*, days_held: int = 1, occ: str = "NVDA260918C00250000") -> SimpleNamespace:
    d = _decision(days_held=days_held)
    d.proposal = {"timeStopDays": 5, "isOption": True, "occSymbol": occ}
    return d


async def test_premium_take_profit_fires_before_the_time_stop() -> None:
    """Ordering is the point: a contract already past the ratchet's hard
    take-profit backstop must not sit two more sessions waiting on the
    calendar.

    ``RiskCaps.options_ratchet_enabled`` defaults True, so `_exit_reason`'s
    options branch runs the ratchet, not the flat `option_exit_signal` —
    which is why this uses a pl (160%) above the ratchet's hard-take-profit
    default (150%) rather than the old flat 60% threshold. See
    `test_ratchet_disabled_reverts_to_the_flat_take_profit` below for that
    flat threshold pinned explicitly via the revert flag.
    """
    reason = await _exit_reason(
        _session(False),
        _option_decision(days_held=1),
        NOW,
        caps=RiskCaps(),  # ratchet on by default; hard_take_profit_pct=150.0
        option_pl_pct={"NVDA260918C00250000": 160.0},
    )
    assert reason == "option_take_profit"


async def test_ratchet_arms_and_holds_instead_of_closing_at_the_old_flat_threshold() -> None:
    """The entire point of PLAN_EXIT_AGENT.md: a gain that used to trip the
    flat +60% take-profit now arms the trail and holds instead, so a
    bigger winner has room to keep running."""
    reason = await _exit_reason(
        _session(False),
        _option_decision(days_held=1),
        NOW,
        caps=RiskCaps(),  # ratchet on by default
        option_pl_pct={"NVDA260918C00250000": 72.4},
    )
    assert reason is None


async def test_ratchet_reads_the_persisted_peak_and_closes_on_retracement() -> None:
    """The high-water mark from a PRIOR tick — stored on
    `decision.reasoning["option_exit"]["peak_pl_pct"]` — must feed the
    ratchet's `peak_pl_pct` input. Without this wiring every tick would
    see a fresh position and never detect a retracement from an earlier
    high."""
    decision = _option_decision(days_held=1)
    decision.reasoning = {
        "option_exit": {"peak_pl_pct": 82.4},
        "contract_funnel": {"stages": ["irrelevant here"]},
    }
    reason = await _exit_reason(
        _session(False),
        decision,
        NOW,
        caps=RiskCaps(),
        option_pl_pct={"NVDA260918C00250000": 50.0},
    )
    assert reason == "option_trail_stop"


async def test_ratchet_disabled_reverts_to_the_flat_take_profit() -> None:
    """`options_ratchet_enabled=False` must reproduce `option_exit_signal`'s
    exact flat-threshold behavior — the single-flag revert the whole
    feature is designed around."""
    reason = await _exit_reason(
        _session(False),
        _option_decision(days_held=1),
        NOW,
        caps=RiskCaps(
            options_ratchet_enabled=False, options_take_profit_pct=60.0,
            options_stop_loss_pct=50.0,
        ),
        option_pl_pct={"NVDA260918C00250000": 72.4},
    )
    assert reason == "option_take_profit"


async def test_ratchet_disabled_ignores_a_persisted_peak() -> None:
    """Reverting must be a COMPLETE revert: a peak persisted from before the
    flag was flipped off must not leak into the flat-threshold path."""
    decision = _option_decision(days_held=1)
    decision.reasoning = {"option_exit": {"peak_pl_pct": 82.4}}
    reason = await _exit_reason(
        _session(False),
        decision,
        NOW,
        caps=RiskCaps(
            options_ratchet_enabled=False, options_take_profit_pct=60.0,
            options_stop_loss_pct=50.0,
        ),
        option_pl_pct={"NVDA260918C00250000": 50.0},
    )
    assert reason is None  # 50.0 is inside both flat thresholds


async def test_premium_stop_loss_fires() -> None:
    reason = await _exit_reason(
        _session(False),
        _option_decision(days_held=1),
        NOW,
        caps=RiskCaps(options_take_profit_pct=60.0, options_stop_loss_pct=50.0),
        option_pl_pct={"NVDA260918C00250000": -58.0},
    )
    assert reason == "option_stop_loss"


async def test_option_inside_both_thresholds_still_holds() -> None:
    reason = await _exit_reason(
        _session(False),
        _option_decision(days_held=1),
        NOW,
        caps=RiskCaps(options_take_profit_pct=60.0, options_stop_loss_pct=50.0),
        option_pl_pct={"NVDA260918C00250000": 12.0},
    )
    assert reason is None


async def test_missing_broker_mark_falls_through_to_the_time_stop() -> None:
    """A broker read that failed must not close anything on its own — but
    it also must not disable the exits that don't need a price."""
    reason = await _exit_reason(
        _session(False),
        _option_decision(days_held=9),
        NOW,
        caps=RiskCaps(options_take_profit_pct=60.0, options_stop_loss_pct=50.0),
        option_pl_pct={},
    )
    assert reason == "agent_time"


async def test_premium_exit_is_keyed_on_the_occ_symbol_not_the_underlying() -> None:
    """The broker keys option positions by OCC. Matching on `NVDA` would
    read the equity position's P&L — a real number for the wrong asset."""
    reason = await _exit_reason(
        _session(False),
        _option_decision(days_held=1),
        NOW,
        caps=RiskCaps(options_take_profit_pct=60.0, options_stop_loss_pct=50.0),
        option_pl_pct={"NVDA": 90.0},
    )
    assert reason is None


async def test_equity_position_is_never_premium_exited() -> None:
    reason = await _exit_reason(
        _session(False),
        _decision(days_held=1),
        NOW,
        caps=RiskCaps(options_take_profit_pct=60.0, options_stop_loss_pct=50.0),
        option_pl_pct={"NVDA": 90.0},
    )
    assert reason is None


# ── _ratchet_outcome_for — the glue between the DB row and the pure state
# machine ────────────────────────────────────────────────────────────────


def test_ratchet_outcome_is_none_for_a_non_option_decision() -> None:
    assert _ratchet_outcome_for(_decision(days_held=1), {}, RiskCaps()) is None


def test_ratchet_outcome_is_none_when_disabled() -> None:
    decision = _option_decision(days_held=1)
    assert (
        _ratchet_outcome_for(decision, {"NVDA260918C00250000": 40.0},
                              RiskCaps(options_ratchet_enabled=False))
        is None
    )


def test_ratchet_outcome_is_none_without_an_occ_symbol() -> None:
    decision = _decision(days_held=1)
    decision.proposal = {"timeStopDays": 5, "isOption": True}  # no occSymbol at all
    assert _ratchet_outcome_for(decision, {}, RiskCaps()) is None


def test_ratchet_outcome_reads_the_persisted_peak_off_the_row() -> None:
    decision = _option_decision(days_held=1)
    decision.reasoning = {"option_exit": {"peak_pl_pct": 82.4}}
    outcome = _ratchet_outcome_for(
        decision, {"NVDA260918C00250000": 50.0}, RiskCaps()
    )
    assert outcome is not None
    assert outcome.peak_pl_pct == 82.4  # unchanged: 50 < 82.4
    assert outcome.action == "CLOSE"
    assert outcome.reason == "option_trail_stop"


def test_ratchet_outcome_defaults_the_peak_when_the_row_has_no_reasoning() -> None:
    """A decision row with no `.reasoning` attribute at all (older fixtures,
    or a fresh row before any tick has written to it) must not raise —
    same defensive `getattr` pattern `_close_position` already uses for
    `.proposal`."""
    decision = _option_decision(days_held=1)
    assert not hasattr(decision, "reasoning")
    outcome = _ratchet_outcome_for(decision, {"NVDA260918C00250000": 20.0}, RiskCaps())
    assert outcome is not None
    assert outcome.peak_pl_pct == 20.0


# ── _option_exit_peak_update_stmt — the jsonb_set write, SQL-text level ──
#
# No live Postgres in this suite (every test in this package mocks the
# session — see fable5findings.md). These assert the emitted SQL and bound
# params directly, which is exactly what would catch the two regressions
# PLAN_EXIT_AGENT.md §9 names: replacing jsonb_set with a whole-column
# overwrite, and dropping the COALESCE.


def test_peak_write_uses_jsonb_set_not_a_whole_column_overwrite() -> None:
    """Break this by replacing the statement with a plain
    `SET reasoning = :payload` and this fails: `contract_funnel` and
    `strategy_fit` would vanish on the next write, since a whole-column
    overwrite has no way to know about sibling keys."""
    stmt, _params = _option_exit_peak_update_stmt(
        decision_id=uuid.uuid4(), payload={"peak_pl_pct": 10.0}
    )
    sql = str(stmt)
    assert "jsonb_set(" in sql
    assert "{option_exit}" in sql
    assert "reasoning = jsonb_set" in sql  # writes back into the SAME column


def test_jsonb_set_writes_into_a_null_reasoning_column() -> None:
    """Break this by dropping the COALESCE and this fails: Postgres's
    `jsonb_set(NULL, ...)` returns NULL, so a decision row with no
    `reasoning` yet would have the whole column silently blanked instead
    of gaining an `option_exit` key."""
    stmt, _params = _option_exit_peak_update_stmt(
        decision_id=uuid.uuid4(), payload={"peak_pl_pct": 10.0}
    )
    assert "COALESCE(reasoning, '{}'::jsonb)" in str(stmt)


def test_peak_write_params_carry_the_decision_id_and_json_payload() -> None:
    did = uuid.uuid4()
    _stmt, params = _option_exit_peak_update_stmt(
        decision_id=did, payload={"peak_pl_pct": 55.5, "armed": True}
    )
    assert params["id"] == str(did)
    assert json.loads(params["payload"]) == {"peak_pl_pct": 55.5, "armed": True}


async def test_persist_option_exit_peak_merges_over_existing_option_exit_state() -> None:
    """Fields this module does not own yet (a future exit-agent's
    `consults`/`log`) must survive a ratchet-only write untouched — see
    `_persist_option_exit_peak`'s own docstring for why."""
    decision = SimpleNamespace(
        id=uuid.uuid4(),
        symbol="NVDA",
        reasoning={
            "option_exit": {"peak_pl_pct": 40.0, "consults": 2, "log": ["x"]},
            "contract_funnel": {"stages": []},  # a SIBLING key, never read here
        },
    )
    outcome = RatchetOutcome(
        action="HOLD", reason=None, detail="d", pnl_pct=45.0, peak_pl_pct=45.0,
        trail_line_pct=31.5, armed=True, may_consult=True, peak_advanced=True,
    )
    session_cm = _FakeSessionCM()
    await _persist_option_exit_peak(
        lambda: session_cm, decision=decision, outcome=outcome
    )

    session_cm.session.execute.assert_awaited_once()
    session_cm.session.commit.assert_awaited_once()
    _stmt, params = session_cm.session.execute.call_args.args
    payload = json.loads(params["payload"])
    assert payload["peak_pl_pct"] == 45.0  # the new value
    assert payload["consults"] == 2  # preserved, not owned by this write
    assert payload["log"] == ["x"]  # preserved
    assert "contract_funnel" not in payload  # never touched — a sibling key


# ── manage_positions_for_user — the peak-write gate ──────────────────────


async def test_manage_positions_persists_the_peak_only_when_it_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two armed-but-holding options in one tick: one whose peak just moved,
    one whose peak did not. Exactly one write must happen."""
    from app.services.orders.position_manager import manage_positions_for_user

    # `manage_positions_for_user` computes `now` as the REAL wall clock
    # (unlike every other test in this file, which passes the fixed `NOW`
    # constant straight into `_exit_reason`) — so these two decisions need
    # a genuinely recent `triggered_at`/`user_responded_at`, or the time
    # stop fires regardless of the ratchet and this test would pass for
    # the wrong reason (it would still pass — the persist gate is checked
    # before the reason is used — but `_close_position` would then also be
    # attempted and fail noisily on these bare fixtures).
    recent = datetime.now(UTC) - timedelta(hours=1)
    d_advances = _option_decision(occ="AAA250101C00100000", days_held=1)
    d_advances.symbol = "AAA"
    d_advances.triggered_at = recent
    d_advances.user_responded_at = recent
    d_holds = _option_decision(occ="BBB250101C00100000", days_held=1)
    d_holds.symbol = "BBB"
    d_holds.triggered_at = recent
    d_holds.user_responded_at = recent
    d_holds.reasoning = {"option_exit": {"peak_pl_pct": 50.0}}

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            _ScalarsResult([d_advances, d_holds]),
            _ScalarOneResult(None),  # newer-sell check for d_advances
            _ScalarOneResult(None),  # newer-sell check for d_holds
        ]
    )
    session_cm = _FakeSessionCM()
    session_cm.session = session

    monkeypatch.setattr(
        position_manager_mod,
        "_option_pl_pct_by_symbol",
        AsyncMock(
            return_value={
                "AAA250101C00100000": 20.0,  # fresh position: peak 0 -> 20, HOLD (not armed)
                "BBB250101C00100000": 40.0,  # peak stays 50 (40 < 50): armed, HOLD, no advance
            }
        ),
    )
    persisted: list[str] = []

    async def _fake_persist(_session_factory, *, decision, outcome) -> None:
        persisted.append(decision.symbol)

    monkeypatch.setattr(position_manager_mod, "_persist_option_exit_peak", _fake_persist)

    count = await manage_positions_for_user(
        user_id="00000000-0000-0000-0000-000000000001",
        session_factory=lambda: session_cm,
        caps=RiskCaps(),
    )

    assert count == 0  # both hold — nothing closes this tick
    assert persisted == ["AAA"]  # only the position whose peak actually moved


# ── Escalation wiring (docs/IMPL_OPTIONS_AGENTS.md §5) ─────────────────
#
# The escalation LOGIC (trigger detection, rate limits, the fail-safe) is
# unit- and integration-tested in apps/agents/tests/test_options_agents.py
# against trading_agents.options.escalation directly. What belongs HERE is
# narrower: does manage_positions_for_user call the escalation check AT
# ALL, for the RIGHT decisions, with the RIGHT arguments, and does a
# failure inside it get swallowed the same way _persist_option_exit_peak's
# already is? `maybe_escalate_option_position` is monkeypatched to a fake
# throughout — driving the REAL LLM/guard path through here would need
# `is_us_market_open(datetime.now(UTC))` to be true at whatever real
# moment the suite happens to run, which is exactly the environment-
# dependent flakiness this split avoids.


async def test_manage_positions_calls_escalation_when_ratchet_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recent = datetime.now(UTC) - timedelta(hours=1)
    d = _option_decision(occ="AAA250101C00100000", days_held=1)
    d.symbol = "AAA"
    d.triggered_at = recent
    d.user_responded_at = recent

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_ScalarsResult([d]), _ScalarOneResult(None)]
    )
    session_cm = _FakeSessionCM()
    session_cm.session = session

    monkeypatch.setattr(
        position_manager_mod, "_option_pl_pct_by_symbol",
        AsyncMock(return_value={"AAA250101C00100000": 40.0}),  # armed, HOLD
    )
    monkeypatch.setattr(position_manager_mod, "_persist_option_exit_peak", AsyncMock())

    calls: list[dict[str, Any]] = []

    async def _fake_maybe_escalate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(should_escalate=True, material_change="ratchet_armed", reason=None)

    monkeypatch.setattr(
        position_manager_mod, "maybe_escalate_option_position", _fake_maybe_escalate
    )

    budget = object()  # a plain sentinel — this test only checks identity
    llm = object()
    guard = object()
    count = await manage_positions_for_user(
        user_id="00000000-0000-0000-0000-000000000001",
        session_factory=lambda: session_cm,
        caps=RiskCaps(),
        escalation_budget=budget,
        llm=llm,
        guard=guard,
    )

    assert count == 0  # the ratchet held — nothing closes
    assert len(calls) == 1
    call = calls[0]
    assert call["decision"] is d
    assert call["dte"] is None  # this fixture's proposal has no expiryDate
    assert call["budget"] is budget  # threaded through unchanged, not rebuilt
    assert call["llm"] is llm
    assert call["guard"] is guard
    assert call["ratchet_outcome"].armed is True
    assert call["ratchet_outcome"].action == "HOLD"


async def test_manage_positions_never_escalates_an_equity_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ratchet_outcome is None` for a non-option decision — the
    escalation check must never even be attempted, since "armed"/"trail
    line" mean nothing for an equity position."""
    # `_decision`'s own `days_held` is relative to the FIXED `NOW`
    # constant, but `manage_positions_for_user` computes `now` as the REAL
    # wall clock (see test_manage_positions_persists_the_peak_only_when_it_advanced's
    # own docstring for this exact gotcha) — an un-adjusted `triggered_at`
    # would fire the TIME STOP instead, routing this decision to the close
    # path and never reaching the branch this test means to exercise.
    d = _decision(days_held=1)
    recent = datetime.now(UTC) - timedelta(hours=1)
    d.triggered_at = recent
    d.user_responded_at = recent

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_ScalarsResult([d]), _ScalarOneResult(None)]
    )
    session_cm = _FakeSessionCM()
    session_cm.session = session

    calls: list[Any] = []

    async def _fake_maybe_escalate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise AssertionError("must not be called for an equity position")

    monkeypatch.setattr(
        position_manager_mod, "maybe_escalate_option_position", _fake_maybe_escalate
    )

    count = await manage_positions_for_user(
        user_id="00000000-0000-0000-0000-000000000001",
        session_factory=lambda: session_cm,
        caps=RiskCaps(),
    )

    assert count == 0
    assert calls == []


async def test_manage_positions_swallows_an_escalation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the existing "continuing without it" convention right next
    to this call site (`_persist_option_exit_peak`'s own try/except): an
    exception raised inside the escalation check must not crash the rest
    of this user's tick, and must not be mistaken for a reason to close."""
    recent = datetime.now(UTC) - timedelta(hours=1)
    d = _option_decision(occ="AAA250101C00100000", days_held=1)
    d.symbol = "AAA"
    d.triggered_at = recent
    d.user_responded_at = recent

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_ScalarsResult([d]), _ScalarOneResult(None)]
    )
    session_cm = _FakeSessionCM()
    session_cm.session = session

    monkeypatch.setattr(
        position_manager_mod, "_option_pl_pct_by_symbol",
        AsyncMock(return_value={"AAA250101C00100000": 40.0}),
    )
    monkeypatch.setattr(position_manager_mod, "_persist_option_exit_peak", AsyncMock())

    async def _raising_maybe_escalate(**_kwargs: Any) -> Any:
        raise RuntimeError("simulated escalation bug")

    monkeypatch.setattr(
        position_manager_mod, "maybe_escalate_option_position", _raising_maybe_escalate
    )

    count = await manage_positions_for_user(
        user_id="00000000-0000-0000-0000-000000000001",
        session_factory=lambda: session_cm,
        caps=RiskCaps(),
    )

    assert count == 0  # the tick completed despite the escalation bug


# ── close_reason length — String(20) in the DB ───────────────────────────


def test_new_close_reasons_fit_in_the_close_reason_column() -> None:
    """`close_reason` is `String(20)`. `option_trailing_stop` (the name
    PLAN_EXIT_AGENT.md explicitly warns against) is exactly 20 and is not
    the name used here; `option_trail_stop` (17) is."""
    for reason in ("option_trail_stop", "option_agent_close"):
        assert len(reason) <= 20, reason


async def test_in_flight_close_guard_detects_pending_sell() -> None:
    """Re-entrance guard: a pending/accepted SELL for the decision means a
    close is already live → the manager must not re-submit."""
    assert await _has_in_flight_close(_session(newer_sell_exists=True), uuid.uuid4()) is True


async def test_in_flight_close_guard_clear_when_no_open_sell() -> None:
    assert await _has_in_flight_close(_session(newer_sell_exists=False), uuid.uuid4()) is False


# ─────────────────────────────────────────────────────────────────────
# _close_position — a short must be covered with a BUY, not another SELL
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _FakePosition:
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float
    unrealized_pl: float = 0.0
    unrealized_pl_pct: float = 0.0
    multiplier: int = 1
    is_option: bool = False


@dataclass
class _FakeCloseOrder:
    broker_order_id: str
    client_order_id: str | None
    symbol: str
    side: Any
    qty: int
    filled_qty: int = 0
    avg_fill_price: float | None = None
    status: Any = "accepted"
    submitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    filled_at: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class _FakeCloseBroker:
    positions: list[Any] = field(default_factory=list)
    placed: list[Any] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    options_trading_level: int | None = None
    requests: list[Any] = field(default_factory=list)
    """Full submitted OrderRequest per call — placed/_FakeCloseOrder only
    decomposes the fields the pre-options tests needed (side, qty); the
    options tests also need order_type/limit_price, hence this."""

    async def get_account_equity(self) -> float:
        return 100_000.0

    async def get_buying_power(self) -> float:
        return 100_000.0

    async def get_options_trading_level(self) -> int | None:
        return self.options_trading_level

    async def list_positions(self) -> list[Any]:
        return list(self.positions)

    async def cancel_open_orders(self, symbol: str) -> int:
        self.canceled.append(symbol)
        return 0

    async def place_order(self, request: Any) -> _FakeCloseOrder:
        self.requests.append(request)
        order = _FakeCloseOrder(
            broker_order_id="alp-close-0001",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
        )
        self.placed.append(order)
        return order


class _FakeSessionCM:
    """Async-context-manager stand-in for ``session_factory()``."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.execute = AsyncMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _short_decision() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        symbol="NVDA",
        fill_qty=10,
        fill_avg_price=100.0,
    )


async def test_close_position_covers_a_short_with_a_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short is held (qty=-10) — closing it must place a BUY for 10
    shares, not another SELL. Before the fix, ``_close_position`` hardcoded
    SELL for every close, which for a short doesn't increase the position
    (the risk engine vetoes it outright — see the docstring) so the
    observable bug was that a short could never be closed through this
    path at all, agent or manual.
    """
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(
                symbol="NVDA", qty=-10, avg_entry_price=100.0, market_value=-1000.0
            )
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    session_cm = _FakeSessionCM()
    initiated = await _close_position(
        lambda: session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=_short_decision(),
        reason="agent_time",
    )

    assert initiated is True
    assert len(broker.placed) == 1
    placed = broker.placed[0]
    assert placed.side == Side.BUY
    assert placed.qty == 10


async def test_close_position_closes_a_long_with_a_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged behavior pin: a long (positive qty) still closes with a
    SELL, exactly as before this fix."""
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(
                symbol="NVDA", qty=10, avg_entry_price=100.0, market_value=1000.0
            )
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    session_cm = _FakeSessionCM()
    initiated = await _close_position(
        lambda: session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=_short_decision(),
        reason="agent_time",
    )

    assert initiated is True
    assert len(broker.placed) == 1
    placed = broker.placed[0]
    assert placed.side == Side.SELL
    assert placed.qty == 10


async def test_close_position_closes_an_option_with_sell_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An option position's own branch: Phase A never holds a short option
    leg to cover, so the close is always SELL_TO_CLOSE — never a "buy to
    cover", regardless of the held qty's sign. The order must be LIMIT
    (never MARKET), priced off the freshly-fetched position's own mark
    (divided by the multiplier, not the raw market_value/qty)."""
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(
                symbol="AAPL260828C00250000",
                qty=1,
                avg_entry_price=2.50,
                market_value=300.0,  # 1 contract * $3.00 mark * 100
                multiplier=100,
                is_option=True,
            )
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    decision = SimpleNamespace(
        id=uuid.uuid4(),
        # UNDERLYING on the row; the contract lives on the proposal.
        symbol="AAPL",
        fill_qty=1,
        fill_avg_price=2.50,
        proposal={
            "isOption": True,
            "multiplier": 100,
            "occSymbol": "AAPL260828C00250000",
        },
    )

    session_cm = _FakeSessionCM()
    initiated = await _close_position(
        lambda: session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=decision,
        reason="agent_expiry",
    )

    assert initiated is True
    assert len(broker.placed) == 1
    placed = broker.placed[0]
    assert placed.side == Side.SELL_TO_CLOSE
    assert placed.qty == 1

    request = broker.requests[0]
    assert request.order_type.value == "LIMIT"
    # market_value(300) / (qty(1) * multiplier(100)) = 3.00 per contract.
    assert request.limit_price == 3.00

    # THE SEAM. Alpaca keys option positions and orders by OCC, so every
    # broker-facing use must be the contract while the decision row keeps the
    # underlying. Matching the held position on "AAPL" finds nothing, and an
    # agent-managed option could then never be closed at all.
    assert request.symbol == "AAPL260828C00250000"
    assert request.symbol != decision.symbol
    assert broker.canceled == ["AAPL260828C00250000"]


async def test_close_position_option_falls_back_to_proposal_when_unheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker already shows this position as flat (e.g. expired/
    exercised) — is_option/multiplier must still be read, from the
    decision's OWN persisted proposal, so the close still routes as an
    option close rather than silently defaulting to the equity branch."""
    broker = _FakeCloseBroker(positions=[])  # nothing held at the broker
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    decision = SimpleNamespace(
        id=uuid.uuid4(),
        symbol="AAPL260828C00250000",
        fill_qty=1,
        fill_avg_price=2.50,
        proposal={"isOption": True, "multiplier": 100},
    )

    session_cm = _FakeSessionCM()
    initiated = await _close_position(
        lambda: session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=decision,
        reason="agent_expiry",
    )

    assert initiated is True
    placed = broker.placed[0]
    assert placed.side == Side.SELL_TO_CLOSE


# ─────────────────────────────────────────────────────────────────────
# sweep_expiring_options_for_user — the mandatory pre-expiry force-close
# ─────────────────────────────────────────────────────────────────────


def _sweep_decision(
    *,
    symbol: str,
    is_option: bool,
    expiry_offset_days: int,
    occ_symbol: str | None = None,
) -> SimpleNamespace:
    """``symbol`` is the UNDERLYING; ``occ_symbol`` is the contract.

    An options decision row stores the underlying (that is what the cron
    dedup, ghost marking and the UI read) and carries the OCC string on the
    proposal. Defaulting ``occ_symbol`` to ``symbol`` keeps the equity
    fixtures unchanged.
    """
    expiry = (datetime.now(UTC) + timedelta(days=expiry_offset_days)).date().isoformat()
    proposal: dict[str, object] = {
        "isOption": is_option,
        "expiryDate": expiry,
        "multiplier": 100,
    }
    if is_option:
        proposal["occSymbol"] = occ_symbol or symbol
    return SimpleNamespace(
        id=uuid.uuid4(),
        symbol=symbol,
        fill_qty=1,
        proposal=proposal,
    )


class _ScalarsResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[object]:
        return self._rows


class _ScalarOneResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


async def test_sweep_expiring_options_closes_only_near_expiry_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filters to is_option=True AND dte <= options_expiry_sweep_dte (2):
    a near-expiry option closes, a far-expiry option and an equity
    decision (however close to some notion of "expiry") do not."""
    from app.services.orders.position_manager import sweep_expiring_options_for_user

    near = _sweep_decision(symbol="AAPL260828C00250000", is_option=True, expiry_offset_days=1)
    far = _sweep_decision(symbol="MSFT260930C00400000", is_option=True, expiry_offset_days=30)
    equity = _sweep_decision(symbol="NVDA", is_option=False, expiry_offset_days=1)

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            _ScalarsResult([near, far, equity]),  # the open-decisions query
            _ScalarOneResult(None),  # _has_in_flight_close for `near` only
        ]
    )
    session_cm = _FakeSessionCM()
    session_cm.session = session

    closed: list[str] = []

    async def _fake_close(_session_factory, *, user_id, decision, reason):
        closed.append(decision.symbol)
        assert reason == "agent_expiry"
        return True

    monkeypatch.setattr(position_manager_mod, "_close_position", _fake_close)

    count = await sweep_expiring_options_for_user(
        user_id="00000000-0000-0000-0000-000000000001",
        session_factory=lambda: session_cm,
    )

    assert count == 1
    assert closed == ["AAPL260828C00250000"]


async def test_sweep_expiring_options_skips_when_already_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.orders.position_manager import sweep_expiring_options_for_user

    near = _sweep_decision(symbol="AAPL260828C00250000", is_option=True, expiry_offset_days=0)

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            _ScalarsResult([near]),
            _ScalarOneResult(object()),  # an in-flight close already exists
        ]
    )
    session_cm = _FakeSessionCM()
    session_cm.session = session

    called = False

    async def _fake_close(*_a, **_kw):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(position_manager_mod, "_close_position", _fake_close)

    count = await sweep_expiring_options_for_user(
        user_id="00000000-0000-0000-0000-000000000001",
        session_factory=lambda: session_cm,
    )

    assert count == 0
    assert called is False


async def test_sweep_expiring_options_skips_unparseable_expiry_rather_than_closing_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed/missing expiry must not be treated as "already expired"
    — skip the sweep check for that row rather than force-closing on bad
    data."""
    from app.services.orders.position_manager import sweep_expiring_options_for_user

    bad = SimpleNamespace(
        id=uuid.uuid4(),
        symbol="AAPL260828C00250000",
        fill_qty=1,
        proposal={"isOption": True, "expiryDate": None},
    )

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_ScalarsResult([bad])])
    session_cm = _FakeSessionCM()
    session_cm.session = session

    called = False

    async def _fake_close(*_a, **_kw):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(position_manager_mod, "_close_position", _fake_close)

    count = await sweep_expiring_options_for_user(
        user_id="00000000-0000-0000-0000-000000000001",
        session_factory=lambda: session_cm,
    )

    assert count == 0
    assert called is False


# ─────────────────────────────────────────────────────────────────────
# cancel_pending_order_now — stopping an order that hasn't filled
#
# An approved proposal with no fill yet used to have NO way to be
# stopped: "no_open_position" was accurate but unhelpful, since there was
# never anything TO close — only an order still working at the broker.
# ─────────────────────────────────────────────────────────────────────


class _FakeCancelBroker:
    def __init__(self, *, final_status: str = "canceled") -> None:
        self.cancelled_ids: list[str] = []
        self._final_status = final_status

    async def cancel_order(self, broker_order_id: str) -> SimpleNamespace:
        self.cancelled_ids.append(broker_order_id)
        return SimpleNamespace(status=self._final_status)


def _fake_read_session(order_row: object | None) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=order_row)
    session.execute = AsyncMock(return_value=result)
    return session


async def test_cancel_pending_order_cancels_at_the_broker_and_updates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.orders.position_manager import cancel_pending_order_now

    order_row = SimpleNamespace(
        id=uuid.uuid4(), broker_order_id="brk-order-1", status="accepted"
    )
    broker = _FakeCancelBroker(final_status="canceled")
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    write_session_cm = _FakeSessionCM()
    result = await cancel_pending_order_now(
        _fake_read_session(order_row),
        lambda: write_session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=SimpleNamespace(id=uuid.uuid4(), symbol="KO"),
    )

    assert result == {"closed": True, "error": None}
    assert broker.cancelled_ids == ["brk-order-1"]
    write_session_cm.session.execute.assert_awaited_once()
    write_session_cm.session.commit.assert_awaited_once()


async def test_cancel_pending_order_with_no_working_order_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order already filled (or was already cancelled) between the
    list and the tap — nothing left to cancel. Must not touch the broker."""
    from app.services.orders.position_manager import cancel_pending_order_now

    broker = _FakeCancelBroker()

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, SimpleNamespace(id="conn-1", is_paper=True)

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    result = await cancel_pending_order_now(
        _fake_read_session(None),
        lambda: _FakeSessionCM(),
        user_id="00000000-0000-0000-0000-000000000001",
        decision=SimpleNamespace(id=uuid.uuid4(), symbol="KO"),
    )

    assert result == {"closed": False, "error": "no_pending_order"}
    assert broker.cancelled_ids == []


# ─────────────────────────────────────────────────────────────────────
# close_unmanaged_position_now / _close_unmanaged_position — closing a
# broker position with NO agent decision behind it at all.
#
# Same close mechanics as _close_position above (risk gate, bracket
# cancel, correct side derivation), keyed by symbol instead of a decision
# row. The worker (_close_unmanaged_position) mirrors _close_position's
# tests almost exactly; the public wrapper additionally owns the
# Postgres gate, uuid validation, and the in-flight re-entrance guard —
# pinned separately since _close_position's own wrapper (close_position_now)
# isn't unit-tested at all in this file (it needs a real decision row).
# ─────────────────────────────────────────────────────────────────────


async def test_close_unmanaged_position_closes_a_long_with_a_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(symbol="NVDA", qty=10, avg_entry_price=100.0, market_value=1000.0)
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    result = await _close_unmanaged_position(
        user_id="00000000-0000-0000-0000-000000000001", symbol="NVDA"
    )

    assert result == {"closed": True, "error": None}
    assert len(broker.placed) == 1
    placed = broker.placed[0]
    assert placed.side == Side.SELL
    assert placed.qty == 10


async def test_close_unmanaged_position_covers_a_short_with_a_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short (qty=-10) closes with a BUY-to-cover, not another SELL —
    same short-covering fix _close_position already needed, re-derived
    here from the HELD broker position since there is no decision row to
    (wrongly) assume a side from."""
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(symbol="TSLA", qty=-10, avg_entry_price=320.0, market_value=-3000.0)
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    result = await _close_unmanaged_position(
        user_id="00000000-0000-0000-0000-000000000001", symbol="TSLA"
    )

    assert result == {"closed": True, "error": None}
    placed = broker.placed[0]
    assert placed.side == Side.BUY
    assert placed.qty == 10


async def test_close_unmanaged_position_closes_an_option_with_sell_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Option branch: always SELL_TO_CLOSE + LIMIT, priced off the broker's
    own fresh mark divided by the multiplier — is_option/multiplier read
    straight off the HELD broker position (there is no stored proposal to
    read them from at all, unlike the decision-keyed close's fallback)."""
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(
                symbol="AAPL260828C00250000",
                qty=1,
                avg_entry_price=2.50,
                market_value=300.0,  # 1 contract * $3.00 mark * 100
                multiplier=100,
                is_option=True,
            )
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    result = await _close_unmanaged_position(
        user_id="00000000-0000-0000-0000-000000000001", symbol="AAPL260828C00250000"
    )

    assert result == {"closed": True, "error": None}
    placed = broker.placed[0]
    assert placed.side == Side.SELL_TO_CLOSE
    assert placed.qty == 1

    request = broker.requests[0]
    assert request.order_type.value == "LIMIT"
    assert request.limit_price == 3.00
    assert request.symbol == "AAPL260828C00250000"
    assert broker.canceled == ["AAPL260828C00250000"]


async def test_close_unmanaged_position_no_open_position_when_not_held_at_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of this path: a symbol the caller does NOT actually
    hold (typo, already closed, or someone else's ticker) simply isn't in
    THEIR OWN broker connection's position list — this is also how
    cross-user ownership is enforced structurally (see the docstring)."""
    broker = _FakeCloseBroker(positions=[])
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    result = await _close_unmanaged_position(
        user_id="00000000-0000-0000-0000-000000000001", symbol="GME"
    )

    assert result == {"closed": False, "error": "no_open_position"}
    assert broker.placed == []


async def test_close_unmanaged_position_persists_an_unlinked_order_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit-trail assertion: the write goes through
    persist_unlinked_order_submit (agent_decision_id always NULL), never
    persist_linked_order_submit — structurally, there is no decision_id
    parameter available to pass one even by accident."""
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(symbol="NVDA", qty=5, avg_entry_price=100.0, market_value=500.0)
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    persist_unlinked = AsyncMock(return_value=None)
    persist_linked = AsyncMock(return_value=None)
    monkeypatch.setattr(position_manager_mod, "persist_unlinked_order_submit", persist_unlinked)
    monkeypatch.setattr(position_manager_mod, "persist_linked_order_submit", persist_linked)

    result = await _close_unmanaged_position(
        user_id="00000000-0000-0000-0000-000000000001", symbol="NVDA"
    )

    assert result == {"closed": True, "error": None}
    persist_unlinked.assert_awaited_once()
    persist_linked.assert_not_awaited()
    call_kwargs = persist_unlinked.await_args.kwargs
    assert "decision_id" not in call_kwargs
    assert call_kwargs["symbol"] == "NVDA"
    assert call_kwargs["side"] == "SELL"
    assert call_kwargs["qty"] == 5


async def test_has_in_flight_unmanaged_close_detects_pending_order() -> None:
    assert (
        await _has_in_flight_unmanaged_close(
            _session(newer_sell_exists=True), user_id=uuid.uuid4(), symbol="NVDA"
        )
        is True
    )


async def test_has_in_flight_unmanaged_close_clear_when_none_pending() -> None:
    assert (
        await _has_in_flight_unmanaged_close(
            _session(newer_sell_exists=False), user_id=uuid.uuid4(), symbol="NVDA"
        )
        is False
    )


# ── close_unmanaged_position_now — the gated public wrapper ───────────


async def test_close_unmanaged_position_now_is_a_noop_in_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USE_POSTGRES unset (the default dev/test mode) — there is no
    unmanaged-position ledger to read at all, so this must refuse WITHOUT
    ever touching the broker. Proven by making with_broker_client raise if
    it's ever entered."""
    monkeypatch.delenv("USE_POSTGRES", raising=False)

    @asynccontextmanager
    async def exploding_broker_cm(*_a, **_kw):
        raise AssertionError("must not touch the broker in mock mode")
        yield  # pragma: no cover - unreachable, satisfies the generator shape

    monkeypatch.setattr(position_manager_mod, "with_broker_client", exploding_broker_cm)

    result = await close_unmanaged_position_now(
        user_id="00000000-0000-0000-0000-000000000001",
        symbol="NVDA",
        session_factory=lambda: _FakeSessionCM(),
    )

    assert result == {"closed": False, "error": "no_open_position"}


async def test_close_unmanaged_position_now_rejects_a_non_uuid_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_POSTGRES", "1")

    @asynccontextmanager
    async def exploding_broker_cm(*_a, **_kw):
        raise AssertionError("must not touch the broker for an invalid user_id")
        yield  # pragma: no cover

    monkeypatch.setattr(position_manager_mod, "with_broker_client", exploding_broker_cm)

    result = await close_unmanaged_position_now(
        user_id="not-a-uuid",
        symbol="NVDA",
        session_factory=lambda: _FakeSessionCM(),
    )

    assert result == {"closed": False, "error": "no_open_position"}


async def test_close_unmanaged_position_now_blocks_when_already_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_POSTGRES", "1")

    @asynccontextmanager
    async def exploding_broker_cm(*_a, **_kw):
        raise AssertionError("must not touch the broker while a close is in flight")
        yield  # pragma: no cover

    monkeypatch.setattr(position_manager_mod, "with_broker_client", exploding_broker_cm)

    result = await close_unmanaged_position_now(
        user_id="00000000-0000-0000-0000-000000000001",
        symbol="NVDA",
        session_factory=_in_flight_session_factory(in_flight=True),
    )

    assert result == {"closed": False, "error": "close_in_flight"}


async def test_close_unmanaged_position_now_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the public wrapper: gate passes, no in-flight
    close, delegates to the worker, which talks to the (fake) broker.

    USE_POSTGRES=1 makes `_build_risk_context` -> `_load_db_state_or_fail`
    try to reach a REAL Postgres connection too (same env var, checked a
    second time deeper in the call chain) — harmless in production where
    USE_POSTGRES=1 implies a real DB is actually configured, but there is
    none in this test process, so that real halt/PDT read is faked here
    the same way the broker is."""
    monkeypatch.setenv("USE_POSTGRES", "1")

    from app.services.orders import executor as executor_mod
    from engine.risk import DbRiskState

    monkeypatch.setattr(
        executor_mod, "_load_db_state_or_fail", AsyncMock(return_value=DbRiskState())
    )

    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(symbol="NVDA", qty=3, avg_entry_price=100.0, market_value=300.0)
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)
    monkeypatch.setattr(
        position_manager_mod, "persist_unlinked_order_submit", AsyncMock(return_value=None)
    )

    result = await close_unmanaged_position_now(
        user_id="00000000-0000-0000-0000-000000000001",
        # Lowercase + whitespace on the way in — must still match the
        # broker's upper-cased symbol.
        symbol=" nvda ",
        session_factory=_in_flight_session_factory(in_flight=False),
    )

    assert result == {"closed": True, "error": None}
    assert broker.placed[0].side == Side.SELL
    assert broker.placed[0].qty == 3


def _in_flight_session_factory(*, in_flight: bool):
    """A ``session_factory`` whose async-CM session reports (or doesn't)
    an in-flight order via ``scalar_one_or_none()`` — what
    ``_has_in_flight_unmanaged_close`` reads. Plain ``_FakeSessionCM``
    can't be reused as-is here: its ``execute`` is an UNCONFIGURED
    ``AsyncMock``, whose auto-generated return value is a truthy
    ``MagicMock`` — i.e. it would silently report "in flight" for every
    caller, happy-path tests included, unless overridden like this.
    """
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=uuid.uuid4() if in_flight else None)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    class _CM:
        async def __aenter__(self) -> MagicMock:
            return session

        async def __aexit__(self, *exc: object) -> bool:
            return False

    return lambda: _CM()


# ─────────────────────────────────────────────────────────────────────
# The resting protective stop must NOT read as a close in flight
# ─────────────────────────────────────────────────────────────────────


async def test_a_resting_protective_stop_is_not_treated_as_a_close_in_flight() -> None:
    """The single interaction that makes broker-side option stops safe.

    ``_has_in_flight_close`` matches ANY open order tied to a decision,
    deliberately unfiltered by side. A protective stop is exactly that —
    an open order that is MEANT to sit there for the life of the position
    — so without an explicit exclusion every protected option would look
    permanently "closing" and the manager would skip it on every tick,
    silently disabling the time stop, the signal exit, the ratchet close
    and escalation. That trades four working exits for one.

    Asserted on the compiled SQL because the guard's filtering happens in
    the database, not in Python: a fake session returning a canned row
    cannot distinguish "the query excluded the stop" from "the query
    matched it and the fake said no".
    """
    from app.services.orders.option_stops import PROTECTIVE_STOP_PREFIX
    from app.services.orders.position_manager import _has_in_flight_close

    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=result)

    await _has_in_flight_close(session, uuid.uuid4())

    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "NOT" in sql.upper() and "LIKE" in sql.upper(), (
        "the in-flight-close query must EXCLUDE resting protective stops"
    )
    assert PROTECTIVE_STOP_PREFIX in sql, (
        f"the exclusion must key off {PROTECTIVE_STOP_PREFIX!r} — the prefix "
        "option_stops actually places its orders under"
    )
