"""Tests for the options-agents tool surface.

Two halves, built independently and combined at merge time:

1. The six read-only tools (``trading_agents.options.tools.readonly`` +
   ``registry``). Covers, per handler: real-shape output (reusing the
   actual aggregation/ratchet/chain-fetch code, not a reimplementation),
   tenant scoping actually enforced (not just "the code looks scoped"),
   and malformed/missing data degrading to ``None``/empty rather than
   raising.
2. End-to-end guard+trade round trips (below, from the ``_guard``/``_ctx``
   fixtures onward) — ``guard.before`` -> ``trade.<handler>`` -> ``guard.
   after`` together, through the real ``dispatch_tool_call`` and the real
   combined ``REGISTRY`` (all eight tools), the way the real tool loop
   actually calls them. See ``test_tool_guard.py`` for the more granular,
   guard-only unit tests (the 12-step stack, the ratchet invariant).

Across both halves: dispatch (``guard.dispatch_tool_call``) never lets an
exception escape, but a tool that itself raises would still be a bug worth
pinning directly, not just one worth catching one layer up.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from test_tool_guard import (
    MARKET_CLOSED_NOW,
    MARKET_OPEN_NOW,
    OPEN_ARGS,
    FakeBroker,
    _call,
    _FakeSessionFactory,
    _seeded_row,
)

from broker.types import Position
from broker.types import Side as BrokerSide
from engine.features.technicals import DailyBar
from engine.options.exits import option_ratchet_signal
from engine.options.selection import ContractQuote
from engine.risk import MockRiskContextProvider, RiskCaps
from trading_agents.cost_ledger import infer_role_from_system_prompt
from trading_agents.llm import LLM, LLMResponse
from trading_agents.llm import ToolCall as LLMToolCall
from trading_agents.memory import InMemoryDecisionLog
from trading_agents.options.agents import run_bull_and_bear, run_options_agents
from trading_agents.options.escalation import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_MAX_PER_DAY,
    EscalationBudget,
    PositionBrief,
    _render_escalation_brief,
    build_position_brief,
    evaluate_escalation_trigger,
    load_escalation_state,
    maybe_escalate,
    run_escalation,
)
from trading_agents.options.prompts import OPTIONS_BEAR, OPTIONS_BULL, OPTIONS_ESCALATION
from trading_agents.options.resolution import AgentView, resolve
from trading_agents.options.tools import readonly
from trading_agents.options.tools import registry as options_registry
from trading_agents.options.tools.guard import GuardContext, ToolGuard, dispatch_tool_call
from trading_agents.options.tools.readonly import (
    _atm_iv,
    _parse_thesis_deadline,
    get_entry_thesis,
    get_funnel_counts,
    get_iv_rank,
    get_option_snapshot,
    get_position_snapshot,
    get_underlying_bars,
)
from trading_agents.options.tools.registry import REGISTRY
from trading_agents.options.tools.schemas import READ_ONLY_TOOLS
from trading_agents.state import CouncilState
from trading_agents.strategies import STRATEGY_REGISTRY


@dataclass
class _Ctx:
    user_id: str


OWNER = str(uuid.uuid4())
OTHER_USER = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _reset_iv_history() -> None:
    """``_IV_HISTORY`` is a module-level dict — tests must not leak samples
    into each other."""
    readonly._IV_HISTORY.clear()


@pytest.fixture(autouse=True)
def _alpaca_data_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path default so most tests don't need to set these themselves;
    the "no_data_credentials" tests explicitly ``delenv`` instead."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")


# ─────────────────────────────────────────────────────────────────────
# Fake async DB session plumbing — same shape as apps/api/tests/
# test_position_manager.py's MagicMock/AsyncMock session doubles, adapted
# to the double-call ``async_session_factory()()`` pattern every handler
# here uses (mirrors app.services.council.ghost_service/funnel_service).
# ─────────────────────────────────────────────────────────────────────


class _FakeAsyncCtx:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, *, get_result: Any = None, execute_rows: list[Any] | None = None) -> None:
        self._get_result = get_result
        self._execute_rows = execute_rows or []

    async def get(self, model: Any, id_: Any) -> Any:
        return self._get_result

    async def execute(self, *_a: object, **_kw: object) -> _FakeResult:
        return _FakeResult(self._execute_rows)


class _NeverQuerySession:
    """Raises if touched at all — proves a code path short-circuits
    before reaching the database."""

    async def get(self, *_a: object, **_kw: object) -> Any:
        raise AssertionError("session.get must not be called")

    async def execute(self, *_a: object, **_kw: object) -> Any:
        raise AssertionError("session.execute must not be called")


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: Any) -> None:
    def _sessionmaker() -> _FakeAsyncCtx:
        return _FakeAsyncCtx(session)

    def _async_session_factory() -> Any:
        return _sessionmaker

    monkeypatch.setattr("engine.db.async_session_factory", _async_session_factory)


def _decision(
    *,
    user_id: str = OWNER,
    decision_id: str | None = None,
    symbol: str = "NVDA",
    proposal: dict[str, Any] | None = None,
    reasoning: dict[str, Any] | None = None,
    fill_avg_price: float | None = 2.20,
    entered_days_ago: int = 3,
    closed_at: datetime | None = None,
) -> SimpleNamespace:
    now = datetime.now(UTC)
    entered_at = now - timedelta(days=entered_days_ago)
    return SimpleNamespace(
        id=uuid.UUID(decision_id) if decision_id else uuid.uuid4(),
        user_id=uuid.UUID(user_id),
        symbol=symbol,
        proposal=proposal if proposal is not None else {},
        reasoning=reasoning if reasoning is not None else {},
        fill_avg_price=fill_avg_price,
        user_responded_at=entered_at,
        triggered_at=entered_at,
        closed_at=closed_at,
    )


# ─────────────────────────────────────────────────────────────────────
# get_funnel_counts
# ─────────────────────────────────────────────────────────────────────


def _funnel_row(*, decision_id: str, symbol: str, final_action: str, counts: dict[str, int],
                 rejection_reason: str | None = None, selected_occ: str | None = None,
                 triggered_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=decision_id,
        symbol=symbol,
        triggered_at=triggered_at or datetime.now(UTC),
        final_action=final_action,
        reasoning={
            "contract_funnel": {
                "counts": counts,
                "rejection_reason": rejection_reason,
                "selected_occ": selected_occ,
            }
        },
    )


async def test_get_funnel_counts_reuses_the_real_aggregator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the handler goes through the SAME
    ``build_funnel_report_from_rows`` reducer funnel_service.py's own
    /api/v1/insights/funnel endpoint uses — the stage labels, the
    first-zero-stage rejection logic, and the "held" vs "bought" outcome
    all come from that shared function, not a re-derivation here."""
    row = _funnel_row(
        decision_id="d-1", symbol="NVDA", final_action="HOLD",
        counts={"total": 40, "contract_type": 22, "dte_window": 12, "delta_band": 0},
        rejection_reason="no_delta_in_band",
    )
    session = _FakeSession(execute_rows=[row])
    _patch_session(monkeypatch, session)

    result = await get_funnel_counts({"underlying": "nvda"}, _Ctx(user_id=OWNER))

    assert result["underlying"] == "NVDA"
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["decision_id"] == "d-1"
    assert run["rejection_reason"] == "no_delta_in_band"
    assert run["rejection_stage"] == "delta_band"
    assert run["outcome"] == "held"
    stage_by_key = {s["stage"]: s for s in run["stages"]}
    assert stage_by_key["delta_band"]["survivors"] == 0
    assert stage_by_key["contract_type"]["survivors"] == 22


async def test_get_funnel_counts_missing_underlying_never_raises() -> None:
    result = await get_funnel_counts({}, _Ctx(user_id=OWNER))
    assert result["runs"] == []


async def test_get_funnel_counts_malformed_user_id_short_circuits_before_any_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user_id that isn't a UUID must never reach the database — proves
    the tenant filter, not just an empty result, is what stops it."""
    _patch_session(monkeypatch, _NeverQuerySession())

    result = await get_funnel_counts({"underlying": "NVDA"}, _Ctx(user_id="not-a-uuid"))

    assert result == {"underlying": "NVDA", "runs": []}


async def test_get_funnel_counts_garbage_limit_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(execute_rows=[])
    _patch_session(monkeypatch, session)
    result = await get_funnel_counts(
        {"underlying": "NVDA", "limit": "not-a-number"}, _Ctx(user_id=OWNER)
    )
    assert result["runs"] == []


# ─────────────────────────────────────────────────────────────────────
# get_option_snapshot
# ─────────────────────────────────────────────────────────────────────


def _quote(
    occ: str, *, strike: float, delta: float | None = 0.45, iv: float | None = 0.32,
    bid: float | None = 2.10, ask: float | None = 2.25, oi: int | None = 500,
    volume: int | None = 3, contract_type: str = "call",
    expiry: date = date(2030, 1, 17),
) -> ContractQuote:
    return ContractQuote(
        occ_symbol=occ, contract_type=contract_type, strike=strike, expiry=expiry,
        bid=bid, ask=ask, open_interest=oi, volume=volume, delta=delta,
        implied_volatility=iv,
    )


async def test_get_option_snapshot_reuses_fetch_option_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuses ``engine.options.contracts.fetch_option_candidates`` — the
    function that merges the correct chain-snapshot client with open
    interest from the metadata endpoint (OPTIONS_PLAYBOOK.md §5's
    client-mixup bug). A raw ``TradingClient.get_option_contracts()`` call
    alone could never produce a delta/IV/bid/ask here at all."""
    quotes = (_quote("NVDA300117C00250000", strike=250.0, oi=900), _quote("NVDA300117C00260000", strike=260.0, oi=100))

    async def _fake_fetch(*_a: object, **_kw: object) -> tuple[ContractQuote, ...]:
        return quotes

    monkeypatch.setattr("engine.options.contracts.fetch_option_candidates", _fake_fetch)

    result = await get_option_snapshot({"underlying": "NVDA"}, _Ctx(user_id=OWNER))

    assert result["found"] is True
    assert result["total_candidates"] == 2
    # Ranked by open interest descending — the higher-OI contract first.
    assert result["contracts"][0]["occ_symbol"] == "NVDA300117C00250000"
    assert result["contracts"][0]["delta"] == 0.45
    assert result["contracts"][0]["volume_last_trade_size"] == 3


async def test_get_option_snapshot_specific_occ_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    quotes = (_quote("NVDA300117C00250000", strike=250.0),)

    async def _fake_fetch(*_a: object, **_kw: object) -> tuple[ContractQuote, ...]:
        return quotes

    monkeypatch.setattr("engine.options.contracts.fetch_option_candidates", _fake_fetch)

    result = await get_option_snapshot(
        {"underlying": "NVDA", "occ_symbol": "nvda300117c00250000"}, _Ctx(user_id=OWNER)
    )
    assert result["found"] is True
    assert result["contract"]["occ_symbol"] == "NVDA300117C00250000"


async def test_get_option_snapshot_unknown_occ_symbol_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch(*_a: object, **_kw: object) -> tuple[ContractQuote, ...]:
        return (_quote("NVDA300117C00250000", strike=250.0),)

    monkeypatch.setattr("engine.options.contracts.fetch_option_candidates", _fake_fetch)

    result = await get_option_snapshot(
        {"underlying": "NVDA", "occ_symbol": "NVDA300117C00999000"}, _Ctx(user_id=OWNER)
    )
    assert result["found"] is False


async def test_get_option_snapshot_missing_credentials_is_honest_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    result = await get_option_snapshot({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    assert result == {"underlying": "NVDA", "found": False, "error": "no_data_credentials"}


async def test_get_option_snapshot_missing_underlying_never_raises() -> None:
    result = await get_option_snapshot({}, _Ctx(user_id=OWNER))
    assert result["found"] is False


async def test_get_option_snapshot_chain_fetch_failure_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raising_fetch(*_a: object, **_kw: object) -> tuple[ContractQuote, ...]:
        raise RuntimeError("alpaca 500")

    monkeypatch.setattr("engine.options.contracts.fetch_option_candidates", _raising_fetch)

    result = await get_option_snapshot({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    assert result["found"] is False


# ─────────────────────────────────────────────────────────────────────
# get_underlying_bars
# ─────────────────────────────────────────────────────────────────────


def _bar(day: date, close: float, volume: float = 1_000_000.0) -> DailyBar:
    return DailyBar(day=day, open=close, high=close, low=close, close=close, volume=volume)


def _patch_bars_provider(monkeypatch: pytest.MonkeyPatch, bars: list[DailyBar]) -> None:
    class _FakeProvider:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        async def daily_bars(self, *_a: object, **_kw: object) -> list[DailyBar]:
            return bars

    monkeypatch.setattr("engine.features.bars.AlpacaDailyBarsProvider", _FakeProvider)


async def test_get_underlying_bars_reuses_alpaca_daily_bars_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuses ``engine.features.bars.AlpacaDailyBarsProvider`` — the exact
    same IEX daily-bars fetch ``RealFeatureProvider`` uses for every
    analyst, not a second bars implementation."""
    bars = [_bar(date(2026, 8, 27), 100.0), _bar(date(2026, 8, 28), 110.0)]
    _patch_bars_provider(monkeypatch, bars)

    result = await get_underlying_bars({"underlying": "nvda"}, _Ctx(user_id=OWNER))

    assert result["found"] is True
    assert result["underlying"] == "NVDA"
    assert result["last_close"] == 110.0
    assert result["pct_change_over_window"] == 10.0
    assert len(result["bars"]) == 2


async def test_get_underlying_bars_no_bars_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bars_provider(monkeypatch, [])
    result = await get_underlying_bars({"underlying": "ZZZZ"}, _Ctx(user_id=OWNER))
    assert result == {"underlying": "ZZZZ", "found": False, "bars": []}


async def test_get_underlying_bars_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    result = await get_underlying_bars({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    assert result == {"underlying": "NVDA", "found": False, "error": "no_data_credentials"}


async def test_get_underlying_bars_lookback_days_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeProvider:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        async def daily_bars(self, symbol: str, *, lookback_days: int = 30) -> list[DailyBar]:
            captured["lookback_days"] = lookback_days
            return [_bar(date(2026, 8, 28), 100.0)]

    monkeypatch.setattr("engine.features.bars.AlpacaDailyBarsProvider", _FakeProvider)

    await get_underlying_bars({"underlying": "NVDA", "lookback_days": 99999}, _Ctx(user_id=OWNER))
    assert captured["lookback_days"] == 90  # _MAX_BARS_LOOKBACK_DAYS


async def test_get_underlying_bars_garbage_lookback_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bars_provider(monkeypatch, [_bar(date(2026, 8, 28), 100.0)])
    result = await get_underlying_bars(
        {"underlying": "NVDA", "lookback_days": "not-a-number"}, _Ctx(user_id=OWNER)
    )
    assert result["found"] is True


# ─────────────────────────────────────────────────────────────────────
# get_iv_rank — the novel one, no real IV-history source exists
# ─────────────────────────────────────────────────────────────────────


def _patch_iv_chain(monkeypatch: pytest.MonkeyPatch, *, atm_iv: float, bars_close: float = 100.0) -> None:
    _patch_bars_provider(monkeypatch, [_bar(date(2026, 8, 28), bars_close)])

    async def _fake_fetch(*_a: object, **_kw: object) -> tuple[ContractQuote, ...]:
        return (_quote("NVDA300117C00100000", strike=bars_close, iv=atm_iv),)

    monkeypatch.setattr("engine.options.contracts.fetch_option_candidates", _fake_fetch)


async def test_get_iv_rank_insufficient_history_returns_none_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single most important property of this handler: with too few
    observed samples it must say so, never emit a number computed from
    noise."""
    _patch_iv_chain(monkeypatch, atm_iv=0.30)

    result = await get_iv_rank({"underlying": "NVDA"}, _Ctx(user_id=OWNER))

    assert result["iv_rank"] is None
    assert result["reason"] == "insufficient_history"
    assert result["samples"] == 1
    assert result["atm_iv"] == 0.30


async def test_get_iv_rank_computes_rank_from_observed_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeds ``_IV_HISTORY`` directly (the module's own persistence
    mechanism) rather than mocking the wall clock across several calendar
    days — this exercises the real min/max rank formula against a known
    history plus one live sample."""
    readonly._IV_HISTORY["NVDA"] = [
        (date(2026, 8, 20), 0.20),
        (date(2026, 8, 21), 0.25),
        (date(2026, 8, 22), 0.30),
        (date(2026, 8, 23), 0.35),
    ]
    _patch_iv_chain(monkeypatch, atm_iv=0.40)

    result = await get_iv_rank({"underlying": "NVDA"}, _Ctx(user_id=OWNER))

    assert result["samples"] == 5
    # min observed = 0.20, max = today's new 0.40 -> today sits at the top.
    assert result["iv_rank"] == 100.0
    assert "process-local" in result["lookback_note"]


async def test_get_iv_rank_same_day_call_does_not_double_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls on the same day must count as one sample — otherwise
    rapid re-checks within a session would inflate 'history' with
    duplicates of the same market condition."""
    _patch_iv_chain(monkeypatch, atm_iv=0.30)
    await get_iv_rank({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    await get_iv_rank({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    assert len(readonly._IV_HISTORY["NVDA"]) == 1


async def test_get_iv_rank_no_variance_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    readonly._IV_HISTORY["NVDA"] = [
        (date(2026, 8, 20), 0.30),
        (date(2026, 8, 21), 0.30),
        (date(2026, 8, 22), 0.30),
        (date(2026, 8, 23), 0.30),
    ]
    _patch_iv_chain(monkeypatch, atm_iv=0.30)

    result = await get_iv_rank({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    assert result["iv_rank"] is None
    assert result["reason"] == "no_iv_variance_observed"


async def test_get_iv_rank_no_iv_in_chain_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bars_provider(monkeypatch, [_bar(date(2026, 8, 28), 100.0)])

    async def _fake_fetch(*_a: object, **_kw: object) -> tuple[ContractQuote, ...]:
        return (_quote("NVDA300117C00100000", strike=100.0, iv=None),)

    monkeypatch.setattr("engine.options.contracts.fetch_option_candidates", _fake_fetch)

    result = await get_iv_rank({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    assert result == {"underlying": "NVDA", "iv_rank": None, "reason": "no_iv_available"}


async def test_get_iv_rank_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    result = await get_iv_rank({"underlying": "NVDA"}, _Ctx(user_id=OWNER))
    assert result == {"underlying": "NVDA", "iv_rank": None, "reason": "no_data_credentials"}


async def test_get_iv_rank_missing_underlying_never_raises() -> None:
    result = await get_iv_rank({}, _Ctx(user_id=OWNER))
    assert result["iv_rank"] is None


def test_atm_iv_picks_nearest_strike_when_price_known() -> None:
    candidates = (
        _quote("A", strike=90.0, iv=0.50),
        _quote("B", strike=100.0, iv=0.30),
        _quote("C", strike=110.0, iv=0.60),
    )
    assert _atm_iv(candidates, underlying_price=101.0) == 0.30


def test_atm_iv_falls_back_to_median_strike_without_a_price() -> None:
    candidates = (
        _quote("A", strike=90.0, iv=0.50),
        _quote("B", strike=100.0, iv=0.30),
        _quote("C", strike=110.0, iv=0.60),
    )
    assert _atm_iv(candidates, underlying_price=None) == 0.30


def test_atm_iv_returns_none_with_no_iv_anywhere() -> None:
    candidates = (_quote("A", strike=90.0, iv=None),)
    assert _atm_iv(candidates, underlying_price=90.0) is None


# ─────────────────────────────────────────────────────────────────────
# get_position_snapshot
# ─────────────────────────────────────────────────────────────────────


async def test_get_position_snapshot_reuses_the_real_ratchet_math(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the peak/trail-line numbers come from
    ``position_manager._ratchet_outcome_for`` (the pure ratchet the
    scheduled position manager itself runs), not a re-derivation: a fresh
    live P&L of +55% against a persisted peak of +42% must advance the
    peak and arm the trail at exactly ``55 * (1 - 0.30)``."""
    decision = _decision(
        proposal={
            "isOption": True,
            "occSymbol": "NVDA300117C00250000",
            "expiryDate": "2030-01-17",
            "limitPrice": 2.17,
        },
        reasoning={"option_exit": {"peak_pl_pct": 42.0, "armed": True, "trail_line_pct": 29.4}},
        fill_avg_price=2.20,
        entered_days_ago=3,
    )
    _patch_session(monkeypatch, _FakeSession(get_result=decision))

    async def _fake_pl_pct(_user_id: str) -> dict[str, float]:
        return {"NVDA300117C00250000": 55.0}

    monkeypatch.setattr(
        "app.services.orders.position_manager._option_pl_pct_by_symbol", _fake_pl_pct
    )

    result = await get_position_snapshot(
        {"decision_id": str(decision.id)}, _Ctx(user_id=str(decision.user_id))
    )

    assert result["found"] is True
    assert result["is_option"] is True
    assert result["entry_premium"] == 2.20
    assert result["current_pl_pct"] == 55.0
    assert result["peak_pl_pct"] == 55.0  # advanced past the persisted 42.0
    assert result["trail_line_pct"] == pytest.approx(55.0 * 0.7)
    assert result["armed"] is True
    assert result["days_held"] == 3
    assert result["dte"] > 0


async def test_get_position_snapshot_tenant_scoping_blocks_a_different_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real proof, not an assertion the code 'looks' scoped: the SAME
    decision row exists at the SAME id, but a caller whose ctx.user_id is
    NOT the row's owner must get an identical 'not found' — never the
    other tenant's position data."""
    decision = _decision(user_id=OWNER, proposal={"isOption": True, "occSymbol": "X"})
    _patch_session(monkeypatch, _FakeSession(get_result=decision))

    result = await get_position_snapshot(
        {"decision_id": str(decision.id)}, _Ctx(user_id=OTHER_USER)
    )

    assert result == {"decision_id": str(decision.id), "found": False}


async def test_get_position_snapshot_missing_decision_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session(monkeypatch, _FakeSession(get_result=None))
    result = await get_position_snapshot({"decision_id": str(uuid.uuid4())}, _Ctx(user_id=OWNER))
    assert result["found"] is False


async def test_get_position_snapshot_malformed_decision_id_never_raises() -> None:
    result = await get_position_snapshot({"decision_id": "not-a-uuid"}, _Ctx(user_id=OWNER))
    assert result == {"decision_id": "not-a-uuid", "found": False}


async def test_get_position_snapshot_ctx_without_user_id_never_raises() -> None:
    result = await get_position_snapshot({"decision_id": str(uuid.uuid4())}, object())
    assert result["found"] is False


async def test_get_position_snapshot_non_option_reports_is_option_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = _decision(proposal={"isOption": False})
    _patch_session(monkeypatch, _FakeSession(get_result=decision))
    result = await get_position_snapshot(
        {"decision_id": str(decision.id)}, _Ctx(user_id=str(decision.user_id))
    )
    assert result["found"] is True
    assert result["is_option"] is False


# ─────────────────────────────────────────────────────────────────────
# get_entry_thesis
# ─────────────────────────────────────────────────────────────────────


async def test_get_entry_thesis_reads_proposal_not_reasoning_drafter_rationale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin for a real find while building this: an approved,
    filled decision's ``reasoning.drafter_rationale`` is ALWAYS absent
    (nodes/drafter.py only ever sets it on a HOLD) — the real thesis for
    an open position lives on ``proposal.rationale``/``bullCase``/
    ``bearCase`` (the camelCase ApprovalProposalDto persisted for any
    approved row). Reverting to read ``reasoning.drafter_rationale``
    first would make this return the wrong (empty) thesis for every real
    open position."""
    decision = _decision(
        proposal={
            "rationale": "NVDA breaks 190 within 3 weeks on volume expansion.",
            "bullCase": "Momentum plus a catalyst.",
            "bearCase": "Could stall at resistance.",
        },
        reasoning={"drafter_rationale": "this must not win"},
    )
    _patch_session(monkeypatch, _FakeSession(get_result=decision))

    result = await get_entry_thesis(
        {"decision_id": str(decision.id)}, _Ctx(user_id=str(decision.user_id))
    )

    assert result["thesis"] == "NVDA breaks 190 within 3 weeks on volume expansion."
    assert result["bull_case"] == "Momentum plus a catalyst."
    assert result["bear_case"] == "Could stall at resistance."


async def test_get_entry_thesis_tenant_scoping_blocks_a_different_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = _decision(user_id=OWNER, proposal={"rationale": "secret thesis"})
    _patch_session(monkeypatch, _FakeSession(get_result=decision))

    result = await get_entry_thesis({"decision_id": str(decision.id)}, _Ctx(user_id=OTHER_USER))

    assert result == {"decision_id": str(decision.id), "found": False}


async def test_get_entry_thesis_parses_a_within_n_weeks_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_at = datetime.now(UTC) - timedelta(days=1)
    decision = _decision(
        proposal={"rationale": "NVDA breaks 190 within 3 weeks on volume expansion."},
        entered_days_ago=1,
    )
    decision.user_responded_at = entered_at
    decision.triggered_at = entered_at
    _patch_session(monkeypatch, _FakeSession(get_result=decision))

    result = await get_entry_thesis(
        {"decision_id": str(decision.id)}, _Ctx(user_id=str(decision.user_id))
    )

    expected = (entered_at.date() + timedelta(weeks=3)).isoformat()
    assert result["parsed_deadline"] == expected
    assert result["deadline_passed"] is False


async def test_get_entry_thesis_unparseable_thesis_returns_null_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = _decision(proposal={"rationale": "NVDA looks strong here."})
    _patch_session(monkeypatch, _FakeSession(get_result=decision))

    result = await get_entry_thesis(
        {"decision_id": str(decision.id)}, _Ctx(user_id=str(decision.user_id))
    )
    assert result["parsed_deadline"] is None
    assert result["deadline_passed"] is False


async def test_get_entry_thesis_deadline_passed_is_true_once_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = _decision(
        proposal={"rationale": "NVDA breaks 190 within 1 day on volume."},
        entered_days_ago=10,
    )
    _patch_session(monkeypatch, _FakeSession(get_result=decision))

    result = await get_entry_thesis(
        {"decision_id": str(decision.id)}, _Ctx(user_id=str(decision.user_id))
    )
    assert result["deadline_passed"] is True


async def test_get_entry_thesis_missing_decision_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session(monkeypatch, _FakeSession(get_result=None))
    result = await get_entry_thesis({"decision_id": str(uuid.uuid4())}, _Ctx(user_id=OWNER))
    assert result["found"] is False


async def test_get_entry_thesis_malformed_decision_id_never_raises() -> None:
    result = await get_entry_thesis({"decision_id": ""}, _Ctx(user_id=OWNER))
    assert result["found"] is False


def test_parse_thesis_deadline_direct() -> None:
    anchor = date(2026, 8, 31)
    assert _parse_thesis_deadline("Breaks out in 10 days", anchor=anchor) == anchor + timedelta(days=10)
    assert _parse_thesis_deadline("No timeframe here", anchor=anchor) is None
    assert _parse_thesis_deadline("", anchor=anchor) is None


# ─────────────────────────────────────────────────────────────────────
# registry.py
# ─────────────────────────────────────────────────────────────────────


_READ_ONLY_HANDLER_NAMES = {
    "get_funnel_counts",
    "get_option_snapshot",
    "get_underlying_bars",
    "get_iv_rank",
    "get_position_snapshot",
    "get_entry_thesis",
}
"""The subset of REGISTRY the generic 2-arg sweeps below exercise. The two
mutating tools are deliberately excluded from those sweeps: calling
open_option_trade/adjust_option_position with garbage args and no real
guard-computed payload isn't a meaningful test of anything (guard_payload
is a REQUIRED argument for them, unlike the read-only tools' optional
one — see tools/readonly.py's module docstring) — their own dedicated
"missing 1 required positional argument: 'guard_payload'" TypeError if
called this way is correct, not a bug to paper over. Their real behavior
under garbage/adversarial input is covered by test_tool_guard.py's 12-step
stack tests and the end-to-end dispatch_tool_call tests below, both of
which exercise them the way they are ACTUALLY ever called: through the
guard, never directly."""


def test_registry_has_all_eight_handlers() -> None:
    """Merge-time completeness check: REGISTRY must combine BOTH
    workstreams' entries — the six read-only tools (this file's original
    scope) AND the two mutating ones (tools/trade.py, a parallel
    workstream) — not silently drop either half."""
    assert set(REGISTRY.keys()) == _READ_ONLY_HANDLER_NAMES | {
        "open_option_trade",
        "adjust_option_position",
    }


def test_registry_names_match_the_schema_names_exactly() -> None:
    """A schema/registry name mismatch would mean the model calls a tool
    name dispatch can never find — proven here rather than assumed."""
    from trading_agents.options.tools.schemas import (
        ADJUST_OPTION_POSITION,
        OPEN_OPTION_TRADE,
    )

    schema_names = {tool["name"] for tool in READ_ONLY_TOOLS} | {
        OPEN_OPTION_TRADE["name"],
        ADJUST_OPTION_POSITION["name"],
    }
    assert schema_names == set(REGISTRY.keys())


async def test_every_read_only_handler_is_awaitable() -> None:
    for name in _READ_ONLY_HANDLER_NAMES:
        result = await REGISTRY[name]({}, _Ctx(user_id=OWNER))
        assert isinstance(result, dict), f"{name} did not return a dict"


# ─────────────────────────────────────────────────────────────────────
# Cross-cutting: malformed input/ctx must never raise, for any READ-ONLY
# handler (see _READ_ONLY_HANDLER_NAMES above for why the two mutating
# tools are out of scope for this particular sweep).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_READ_ONLY_HANDLER_NAMES))
async def test_handler_never_raises_on_garbage_args_and_ctx(name: str) -> None:
    handler = REGISTRY[name]
    garbage_args_variants: list[dict[str, Any]] = [
        {},
        {"underlying": None},
        {"decision_id": None},
        {"underlying": 12345, "decision_id": 12345, "limit": [], "occ_symbol": {}},
    ]
    for args in garbage_args_variants:
        result = await handler(args, object())  # ctx with no user_id at all
        assert isinstance(result, dict), f"{name} raised or returned non-dict for {args!r}"


@pytest.mark.parametrize("name", ["get_position_snapshot", "get_entry_thesis"])
async def test_by_id_handler_never_raises_on_a_real_unmocked_db_failure(name: str) -> None:
    """A VALID ctx.user_id + a syntactically valid but nonexistent
    decision_id, with NO session mock at all — the only way to actually
    clear ``_user_and_decision_uuid``'s early guard and exercise the
    try/except wrapped around the real database call. Whatever is (or
    isn't) reachable at the default ``DATABASE_URL`` in this environment,
    it does not authenticate as the default dev credentials, so this is a
    genuine, not simulated, backend failure — proving the handler degrades
    it to `found: False` rather than only ever exercising the early-return
    path the garbage-args sweep above covers. Revert-checked: removing the
    try/except around ``get_position_snapshot``'s body reproduces this
    exact failure as an uncaught ``asyncpg.exceptions.InvalidPasswordError``
    — see the commit history for that run's output."""
    handler = REGISTRY[name]
    result = await handler({"decision_id": str(uuid.uuid4())}, _Ctx(user_id=OWNER))
    assert result == {"decision_id": result["decision_id"], "found": False}


# ─────────────────────────────────────────────────────────────────────
# End-to-end guard+trade round trips, through the real dispatch_tool_call
# and the real combined REGISTRY (all eight tools) — proving the two
# workstreams above compose correctly together, not just in isolation.
# ─────────────────────────────────────────────────────────────────────


def _guard(**kwargs: Any) -> ToolGuard:
    kwargs.setdefault("context_provider", MockRiskContextProvider())
    kwargs.setdefault("decision_log", InMemoryDecisionLog())
    kwargs.setdefault("session_factory", None)
    kwargs.setdefault("broker_factory", lambda: FakeBroker())
    kwargs.setdefault("clock", lambda: MARKET_OPEN_NOW)
    return ToolGuard(**kwargs)


def _ctx(**kwargs: Any) -> GuardContext:
    kwargs.setdefault("user_id", str(uuid.uuid4()))
    kwargs.setdefault("council_run_id", str(uuid.uuid4()))
    kwargs.setdefault("resolved_direction", "long")
    kwargs.setdefault("resolved_conviction", 0.6)
    kwargs.setdefault("calls_this_pass", 0)
    kwargs.setdefault("caps", RiskCaps(options_disabled=False))
    return GuardContext(**kwargs)


async def _fetch_ok(*args: Any, **kwargs: Any) -> tuple[ContractQuote, ...]:
    return (
        ContractQuote(
            occ_symbol="NVDA260918C00225000",
            contract_type="call",
            strike=225.0,
            expiry=date(2026, 9, 18),
            bid=2.10,
            ask=2.20,
            open_interest=500,
            volume=20,
            delta=0.45,
            implied_volatility=0.30,
        ),
    )


async def test_open_then_scale_in_end_to_end_through_dispatch(
    monkeypatch: Any,
) -> None:
    """The full loop a real Bull-agent tool call would drive: open a
    position, then scale into the SAME contract, both through
    dispatch_tool_call and the real registry — proving guard.py and
    trade.py compose correctly, not just in isolation."""
    session_factory = _FakeSessionFactory(get_result=None)
    broker = FakeBroker(filled_qty=4, avg_fill_price=2.20)
    decision_log = InMemoryDecisionLog()
    guard = _guard(
        session_factory=session_factory, decision_log=decision_log, broker_factory=lambda: broker
    )
    ctx = _ctx()

    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)

    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        opened = await dispatch_tool_call(
            _call("open_option_trade", OPEN_ARGS),
            ctx,
            guard=guard,
            registry=options_registry.REGISTRY,
        )
    assert opened["is_error"] is False
    decision_id = opened["content"]["decision_id"]
    assert len(broker.orders) == 1

    # Now seed the fake session to return THIS decision's row for the
    # scale-in lookup (a real Postgres round trip would just re-read what
    # open_option_trade wrote; the fake session can't, so it is told).
    row = _seeded_row(
        uid=uuid.UUID(ctx.user_id), did=uuid.UUID(decision_id),
        option_exit={"adds_this_position": 0},
    )
    session_factory.get_result = row

    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        scaled = await dispatch_tool_call(
            _call(
                "adjust_option_position",
                {"decision_id": decision_id, "action": "SCALE_IN", "reason": "adding on strength"},
            ),
            ctx,
            guard=guard,
            registry=options_registry.REGISTRY,
        )

    assert scaled["is_error"] is False, scaled
    assert scaled["content"]["adds_this_position"] == 1
    assert len(broker.orders) == 2
    assert broker.orders[1].symbol == "NVDA260918C00225000"
    assert broker.orders[1].side == BrokerSide.BUY_TO_OPEN


async def test_exit_now_end_to_end_closes_the_broker_position(monkeypatch: Any) -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    position = Position(
        symbol="NVDA260918C00225000",
        qty=4,
        avg_entry_price=2.20,
        market_value=1_200.0,  # up from entry — current mark is 1200/(4*100)=3.00
        unrealized_pl=320.0,
        unrealized_pl_pct=36.4,
        multiplier=100,
        is_option=True,
    )
    session_factory = _FakeSessionFactory(
        get_result=_seeded_row(uid=uid, did=did, option_exit={"stop_loss_pct": 40.0})
    )
    broker = FakeBroker(position=position)
    guard = _guard(session_factory=session_factory, broker_factory=lambda: broker)
    ctx = _ctx(user_id=str(uid))

    # _before_adjust_option_position gates on the same master-switch/paper/
    # market-hours check open_option_trade always had (merge-time fix,
    # see fable5findings.md) — same setup as the open+scale-in test above.
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)

    out = await dispatch_tool_call(
        _call(
            "adjust_option_position",
            {"decision_id": str(did), "action": "EXIT_NOW", "reason": "banking the win"},
        ),
        ctx,
        guard=guard,
        registry=options_registry.REGISTRY,
    )

    assert out["is_error"] is False, out
    assert out["content"]["changed"] is True
    assert len(broker.orders) == 1
    assert broker.orders[0].side == BrokerSide.SELL_TO_CLOSE
    assert broker.orders[0].qty == 4
    assert broker.canceled == ["NVDA260918C00225000"]
    # closed_at/close_reason stamp went through the same fake session.
    all_sql = [str(s) for sess in session_factory.sessions for s, _p in sess.execute_log]
    assert any("closed_at" in sql for sql in all_sql)


async def test_exit_now_with_no_broker_position_is_a_safe_no_op(monkeypatch: Any) -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    session_factory = _FakeSessionFactory(
        get_result=_seeded_row(uid=uid, did=did, option_exit={})
    )
    broker = FakeBroker(position=None)
    guard = _guard(session_factory=session_factory, broker_factory=lambda: broker)
    ctx = _ctx(user_id=str(uid))

    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)

    out = await dispatch_tool_call(
        _call(
            "adjust_option_position",
            {"decision_id": str(did), "action": "EXIT_NOW", "reason": "bail"},
        ),
        ctx,
        guard=guard,
        registry=options_registry.REGISTRY,
    )
    assert out["is_error"] is False
    assert out["content"]["changed"] is False
    assert broker.orders == []


async def test_read_only_tool_allowed_through_the_guards_before_gate() -> None:
    """The integration point added at merge time: ToolGuard.before() must
    recognize all six read-only tool names (guard.py, built independently
    of readonly.py per the worktree-gap note in this session's build log,
    originally only recognized the two mutating tool names and would have
    denied every read-only call as "unknown_tool"). Exercises one read-only
    tool through the REAL dispatch_tool_call + the REAL combined REGISTRY,
    not just ToolGuard.before() in isolation, so a regression in either the
    guard's read-only branch OR the registry's wiring shows up here."""
    guard = _guard()
    ctx = _ctx()
    out = await dispatch_tool_call(
        _call("get_funnel_counts", {"underlying": "NVDA"}),
        ctx,
        guard=guard,
        registry=options_registry.REGISTRY,
    )
    assert out["is_error"] is False, out


# ─────────────────────────────────────────────────────────────────────
# Bull/Bear agents + resolution (options/agents.py, options/resolution.py,
# options/prompts.py) — docs/IMPL_OPTIONS_AGENTS.md §3-4.
#
# Three groups:
#   1. resolution.py in isolation — pure Python, no LLM, no guard.
#   2. Role-phrase registration (llm.py::_mock_response +
#      cost_ledger.py::infer_role_from_system_prompt) — neither raises on
#      a miss, so this needs an explicit test (docs/IMPL_OPTIONS_AGENTS.md
#      §3.1).
#   3. The sequencing itself: parallel argument -> resolve -> (only on
#      proceed) the Bull-only tool-calling hop, through the REAL guard and
#      REAL registry these tests reuse from above.
# ─────────────────────────────────────────────────────────────────────


def _state(**overrides: Any) -> CouncilState:
    base: dict[str, Any] = {
        "symbol": "NVDA",
        "horizon": "short",
        "context": {},
        "user_id": str(uuid.uuid4()),
        "council_run_id": str(uuid.uuid4()),
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


def _enable_auto_trade(monkeypatch: pytest.MonkeyPatch, *, need_broker_creds: bool = False) -> None:
    """Only the tests that actually drive a tool call through the REAL
    guard need this — most of this section's tests never reach
    ``guard.before`` at all (resolution never proceeded, or the fake's
    ``complete_tools`` never emitted a tool call), so they don't call this.
    """
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    if need_broker_creds:
        monkeypatch.setenv("ALPACA_API_KEY", "test-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")


def _tool_call_response(name: str, args: dict[str, Any], *, call_id: str = "call-1") -> LLMResponse:
    return LLMResponse(
        text="",
        model="test",
        tool_calls=(LLMToolCall(id=call_id, name=name, input=args),),
        stop_reason="tool_use",
    )


class _ScriptedLLM:
    """A minimal LLM double for ``options/agents.py`` tests.

    ``complete()`` (hop 1 — the argument phase) answers per-role from the
    ``bull_view``/``bear_view`` dicts, keyed off the system prompt's role
    phrase, exactly the way the REAL mock (``llm.py::_mock_response``)
    keys off it — just without needing a real ``LLM`` instance.
    ``complete_tools()`` (hop 2 — Bull's tool-calling hop, the only one
    that ever happens) pops responses off ``trade_responses`` in order;
    once exhausted it returns a plain no-tool-call text response, which is
    what ends ``run_tool_loop`` — so a test only has to script as many
    rounds as it actually cares about.
    """

    def __init__(
        self,
        *,
        bull_view: dict[str, Any],
        bear_view: dict[str, Any],
        trade_responses: list[LLMResponse] | None = None,
    ) -> None:
        self.bull_view = bull_view
        self.bear_view = bear_view
        self._trade_responses = list(trade_responses or [])
        self.complete_tools_calls: list[dict[str, Any]] = []

    async def complete(self, *, system: str, user: str, **kwargs: Any) -> LLMResponse:
        # Match the FULL role phrase, exactly like the real
        # llm.py::_mock_response / cost_ledger.py::infer_role_from_system_prompt
        # do — a bare "bull"/"bear" substring check is NOT safe here: the
        # Bear prompt's own body legitimately mentions "the Bull Agent"
        # (explaining the parallel-argument setup) well within the first
        # 120 chars, so a loose check would misidentify Bear as Bull. Own
        # bug, caught by test_no_tool_hop_when_resolution_does_not_proceed
        # and test_full_pass_bull_and_bear_agree_and_a_trade_opens both
        # failing identically the first time this fake was run for real.
        role_line = system[:120].lower()
        body = self.bull_view if "you are the options bull agent" in role_line else self.bear_view
        return LLMResponse(text=json.dumps(body), model="test")

    async def complete_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        self.complete_tools_calls.append({"system": system, "tools": tools})
        if self._trade_responses:
            return self._trade_responses.pop(0)
        return LLMResponse(text="Standing down; no trade this pass.", model="test")


# ─────────────────────────────────────────────────────────────────────
# resolution.py — pure, no LLM, no guard.
# ─────────────────────────────────────────────────────────────────────


def test_conviction_is_the_min_not_the_mean() -> None:
    """docs/IMPL_OPTIONS_AGENTS.md §4: `min()`, not the average. Revert
    check: swap resolve()'s `min(bull.conviction, bear.conviction)` for
    `(bull.conviction + bear.conviction) / 2` and this fails (0.7 != 0.5)."""
    bull = AgentView(role="bull", direction="long", conviction=0.9, thesis="NVDA breaks 190 within 3 weeks.")
    bear = AgentView(role="bear", direction="long", conviction=0.5, thesis="NVDA holds support within 3 weeks.")
    result = resolve(bull, bear)
    assert result.proceed
    assert result.direction == "long"
    assert result.conviction == 0.5
    assert result.reason == "agreed"


def test_agents_disagreeing_means_no_trade() -> None:
    bull = AgentView(role="bull", direction="long", conviction=0.6, thesis="NVDA breaks 190 within 3 weeks.")
    bear = AgentView(role="bear", direction="short", conviction=0.6, thesis="NVDA breaks down within 3 weeks.")
    result = resolve(bull, bear)
    assert not result.proceed
    assert result.direction is None
    assert result.reason == "agents_disagree"


def test_either_agent_abstaining_means_no_trade() -> None:
    bull = AgentView(role="bull", direction=None, conviction=0.0, thesis="No edge this pass.")
    bear = AgentView(role="bear", direction="long", conviction=0.6, thesis="NVDA breaks 190 within 3 weeks.")
    result = resolve(bull, bear)
    assert not result.proceed
    assert result.direction is None
    assert result.reason == "abstained"


def test_conviction_divergence_means_no_trade() -> None:
    bull = AgentView(role="bull", direction="long", conviction=0.9, thesis="NVDA breaks 190 within 3 weeks.")
    bear = AgentView(role="bear", direction="long", conviction=0.4, thesis="NVDA holds support within 3 weeks.")
    result = resolve(bull, bear)
    assert not result.proceed
    assert result.direction is None
    assert result.reason == "conviction_divergence"


def test_options_prompts_strategy_list_matches_registry() -> None:
    """Guards against the CLAUDE.md §4.4 drift: the strategy id list
    embedded in both prompts must track STRATEGY_REGISTRY, not a
    hand-copied snapshot of it."""
    for strategy_id in STRATEGY_REGISTRY:
        assert strategy_id in OPTIONS_BULL
        assert strategy_id in OPTIONS_BEAR


# ─────────────────────────────────────────────────────────────────────
# Role-phrase registration — llm.py::_mock_response +
# cost_ledger.py::infer_role_from_system_prompt. Neither raises on a
# miss, so this is the only thing that would catch a regression.
# ─────────────────────────────────────────────────────────────────────


async def test_bull_role_resolves_in_mock_and_cost_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = LLM(api_key=None)
    assert llm.mock is True

    resp = await llm.complete(system=OPTIONS_BULL, user="Ticker: NVDA\nHorizon: short\n")
    body = json.loads(resp.text)
    assert "direction" in body, f"got the generic fallback mock shape: {body!r}"
    assert body["direction"] in ("long", "short")

    assert infer_role_from_system_prompt(OPTIONS_BULL) == "options_bull"


async def test_bear_role_resolves_in_mock_and_cost_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = LLM(api_key=None)
    assert llm.mock is True

    resp = await llm.complete(system=OPTIONS_BEAR, user="Ticker: NVDA\nHorizon: short\n")
    body = json.loads(resp.text)
    assert "direction" in body, f"got the generic fallback mock shape: {body!r}"
    assert body["direction"] in ("long", "short")

    assert infer_role_from_system_prompt(OPTIONS_BEAR) == "options_bear"


# ─────────────────────────────────────────────────────────────────────
# Concurrency — wall-clock, not call count (docs/IMPL_OPTIONS_AGENTS.md
# §3.3 / PLAN doc §11.6: a sequential `await run_bull(); await run_bear()`
# would still pass a "both got called" assertion while doubling latency).
# ─────────────────────────────────────────────────────────────────────


async def test_agents_run_concurrently() -> None:
    delay = 0.2
    body = {
        "direction": "long", "strategy": "momentum", "conviction": 0.6,
        "thesis": "NVDA breaks 190 within 3 weeks on volume expansion.",
    }

    class _SlowLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, *, system: str, user: str, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            await asyncio.sleep(delay)
            return LLMResponse(text=json.dumps(body), model="test")

    fake = _SlowLLM()
    start = time.monotonic()
    bull, bear = await run_bull_and_bear(_state(), fake)
    elapsed = time.monotonic() - start

    assert fake.calls == 2
    assert bull.direction == "long"
    assert bear.direction == "long"
    # Sequential execution would take ~2x delay; concurrent stays near 1x.
    assert elapsed < delay * 1.6, f"expected concurrent execution, took {elapsed:.3f}s for 2x{delay}s calls"


# ─────────────────────────────────────────────────────────────────────
# The sequencing: parallel argument -> resolve -> (only on proceed) the
# Bull-only tool-calling hop, through the REAL guard + REAL registry.
# ─────────────────────────────────────────────────────────────────────


async def test_only_bull_gets_the_trade_tool() -> None:
    fake = _ScriptedLLM(
        bull_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.6,
            "thesis": "NVDA breaks 190 within 3 weeks.",
        },
        bear_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.55,
            "thesis": "NVDA holds support within 3 weeks.",
        },
    )
    result = await run_options_agents(_state(), fake, guard=_guard(), caps=RiskCaps(options_disabled=False))

    assert result.resolution.proceed
    assert len(fake.complete_tools_calls) == 1, "Bear must never get a tool-calling hop at all"
    call = fake.complete_tools_calls[0]
    assert call["system"][:120].lower().startswith("you are the options bull agent")
    assert {t["name"] for t in call["tools"]} >= {"open_option_trade"}


async def test_no_tool_hop_when_resolution_does_not_proceed() -> None:
    """A HOLD (disagreement here) must never even attempt the tool-calling
    hop — zero extra LLM calls for a pass that was never going to trade
    (docs/PLAN_OPTIONS_AGENTS.md §8's latency table)."""
    fake = _ScriptedLLM(
        bull_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.6,
            "thesis": "NVDA breaks 190 within 3 weeks.",
        },
        bear_view={
            "direction": "short", "strategy": "momentum", "conviction": 0.6,
            "thesis": "NVDA breaks down within 3 weeks.",
        },
    )
    result = await run_options_agents(_state(), fake, guard=_guard(), caps=RiskCaps(options_disabled=False))

    assert not result.resolution.proceed
    assert result.resolution.reason == "agents_disagree"
    assert result.trade_response is None
    assert result.tool_transcript == ()
    assert fake.complete_tools_calls == []


async def test_trade_hop_guard_context_carries_the_resolved_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the SEQUENCING: the tool-calling hop's GuardContext carries
    resolution.py's RESOLVED direction, not Bull's own un-resolved (or a
    missing) one. Made observable by having Bull's hop-2 tool call itself
    CONTRADICT the resolution: the guard's own (separately tested)
    ``direction_contradicts_resolution`` denial only fires when
    ``ctx.resolved_direction`` is actually wired to what resolve()
    decided.
    """
    _enable_auto_trade(monkeypatch)
    contradicting_args = {**OPEN_ARGS, "direction": "short"}
    fake = _ScriptedLLM(
        bull_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.6,
            "thesis": "NVDA breaks 190 within 3 weeks.",
        },
        bear_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.55,
            "thesis": "NVDA holds support within 3 weeks.",
        },
        trade_responses=[_tool_call_response("open_option_trade", contradicting_args)],
    )
    result = await run_options_agents(_state(), fake, guard=_guard(), caps=RiskCaps(options_disabled=False))

    assert result.resolution.proceed
    assert result.resolution.direction == "long"
    trade_calls = [c for c in result.tool_transcript if c["tool"] == "open_option_trade"]
    assert len(trade_calls) == 1
    assert trade_calls[0]["output"]["is_error"] is True
    assert trade_calls[0]["output"]["content"]["denied"] == "direction_contradicts_resolution"


async def test_second_open_attempt_in_same_pass_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that calls ``open_option_trade`` TWICE in the same
    tool-calling hop must not open two positions. ``tools/guard.py``'s own
    ``one_open_per_pass`` rule only bites here if ``run_options_agents``
    actually threads an incrementing call count through a FRESH
    ``GuardContext`` across rounds — a ``ctx`` built once and reused
    unchanged would let both attempts see ``calls_this_pass=0`` and both
    succeed. Revert check: hardcode ``calls_this_pass=0`` in
    ``run_options_agents``'s ``_dispatch`` closure and this fails (both
    opens report ``is_error is False``)."""
    _enable_auto_trade(monkeypatch, need_broker_creds=True)
    fake = _ScriptedLLM(
        bull_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.6,
            "thesis": "NVDA breaks 190 within 3 weeks.",
        },
        bear_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.55,
            "thesis": "NVDA holds support within 3 weeks.",
        },
        trade_responses=[
            _tool_call_response("open_option_trade", dict(OPEN_ARGS), call_id="c1"),
            _tool_call_response("open_option_trade", dict(OPEN_ARGS), call_id="c2"),
        ],
    )
    guard = _guard()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        result = await run_options_agents(
            _state(), fake, guard=guard, caps=RiskCaps(options_disabled=False), max_rounds=3
        )

    opens = [c for c in result.tool_transcript if c["tool"] == "open_option_trade"]
    assert len(opens) == 2
    assert opens[0]["output"]["is_error"] is False
    assert opens[1]["output"]["is_error"] is True
    assert opens[1]["output"]["content"]["denied"] == "one_open_per_pass"


async def test_full_pass_bull_and_bear_agree_and_a_trade_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole two-hop sequence, end to end: parallel argument -> resolve
    -> only then does Bull's tool-calling hop run, through the REAL guard
    and REAL registry, landing a real (fake-broker) fill — proving the
    pieces this file owns (agents.py, resolution.py, prompts.py) compose
    correctly with the already-shipped guard/trade tools, not just in
    isolation."""
    _enable_auto_trade(monkeypatch, need_broker_creds=True)
    fake = _ScriptedLLM(
        bull_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.6,
            "thesis": "NVDA breaks 190 within 3 weeks on volume expansion.",
        },
        bear_view={
            "direction": "long", "strategy": "momentum", "conviction": 0.55,
            "thesis": "NVDA holds support within 3 weeks; liquidity checks out.",
        },
        trade_responses=[_tool_call_response("open_option_trade", dict(OPEN_ARGS))],
    )
    guard = _guard()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        result = await run_options_agents(_state(), fake, guard=guard, caps=RiskCaps(options_disabled=False))

    assert result.resolution.proceed
    assert result.resolution.reason == "agreed"
    assert result.resolution.conviction == pytest.approx(0.55)  # min(0.6, 0.55), not the mean

    opens = [c for c in result.tool_transcript if c["tool"] == "open_option_trade"]
    assert len(opens) == 1
    assert opens[0]["output"]["is_error"] is False
    assert opens[0]["output"]["content"]["occ_symbol"] == "NVDA260918C00225000"
    assert opens[0]["output"]["content"]["qty"] >= 1


# ─────────────────────────────────────────────────────────────────────
# The escalation loop (options/escalation.py) — docs/IMPL_OPTIONS_AGENTS.md
# §5 / docs/PLAN_OPTIONS_AGENTS.md §5. Four groups:
#   1. Role-phrase registration, same reasoning as Bull/Bear above.
#   2. The pure material-change trigger, one test per named trigger plus a
#      quiet tick that must NOT fire.
#   3. Rate limits: auto-trade/paper/market gates, cooldown, daily cap,
#      and the fleet-tick budget.
#   4. End-to-end through `maybe_escalate` — the fail-safe (error -> no
#      order, protection unchanged), MOCK mode (same), and a real
#      TIGHTEN_STOP round trip proving the escalation's own bookkeeping
#      write does not clobber a concurrent guard write to the same key.
# ─────────────────────────────────────────────────────────────────────


def _permissive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clears every gate `_mutation_gate_reason` checks EXCEPT market
    hours (the caller still picks `now`) — used by every trigger/rate-
    limit test that isn't itself testing one of these three gates."""
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)


def _ratchet(*, pl: float | None, peak: float | None) -> Any:
    """Real `option_ratchet_signal` output at RiskCaps()'s default knobs
    (arm 35%, giveback 30%, hard TP 150%, stop 50%) — using the actual
    ratchet math rather than hand-built RatchetOutcome instances keeps
    every fixture below a combination the real system could actually
    produce."""
    return option_ratchet_signal(
        unrealized_pl_pct=pl, peak_pl_pct=peak, arm_pct=35.0, giveback_frac=0.30,
        hard_take_profit_pct=150.0, stop_loss_pct=50.0,
    )


# ── 1. Role-phrase registration ─────────────────────────────────────


async def test_escalation_role_resolves_in_mock_and_cost_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = LLM(api_key=None)
    assert llm.mock is True

    resp = await llm.complete(system=OPTIONS_ESCALATION, user="decision_id: x\n")
    body = json.loads(resp.text)
    assert "action" in body, f"got the generic fallback mock shape: {body!r}"

    assert infer_role_from_system_prompt(OPTIONS_ESCALATION) == "options_escalation"


# ── 2. The material-change trigger ──────────────────────────────────


def test_ratchet_armed_trigger_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crossing +35% for the first time both arms the ratchet AND is the
    single named trigger docs §5.1 calls "the ratchet just armed".
    Revert-checked: inverting `not was_armed_before` to `was_armed_before`
    in `_detect_material_change` made this test fail (`should_escalate is
    False`, `material_change is None`) — restored after confirming red."""
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=40.0, peak=None)
    assert outcome.armed  # sanity: 40 >= the 35% arm threshold

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=None,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is True
    assert trigger.material_change == "ratchet_armed"


def test_quiet_tick_does_not_escalate(monkeypatch: pytest.MonkeyPatch) -> None:
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=10.0, peak=10.0)
    assert not outcome.armed

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=None,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is False
    assert trigger.material_change is None
    assert trigger.reason == "no_material_change"


def test_peak_advanced_trigger_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already armed (so `ratchet_armed` does NOT re-fire); peak has
    advanced >=15pp since the persisted `last_escalated_peak_pct`."""
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=55.0, peak=40.0)
    assert outcome.armed
    assert outcome.peak_pl_pct == 55.0

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=True, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=40.0,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is True
    assert trigger.material_change == "peak_advanced"


def test_peak_advanced_below_threshold_does_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=50.0, peak=40.0)  # advanced 10pp, below the 15pp trigger
    assert outcome.armed

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=True, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=40.0,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is False
    assert trigger.reason == "no_material_change"


def test_near_trail_line_trigger_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Peak 100 (persisted, unchanged this tick), 30% giveback -> trail
    line 70. Current premium 75 is a HOLD (above the trail line) but
    within 10pp of it."""
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=75.0, peak=100.0)
    assert outcome.action == "HOLD"
    assert outcome.trail_line_pct == pytest.approx(70.0)
    assert not outcome.peak_advanced  # 75 < persisted peak 100

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=True, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=100.0,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is True
    assert trigger.material_change == "near_trail_line"


def test_dte_low_trigger_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fires even on an otherwise-quiet, never-armed position."""
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=5.0, peak=None)
    assert not outcome.armed

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=5,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=None,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is True
    assert trigger.material_change == "dte_low"

    # One DTE above the threshold: the same quiet position must NOT fire.
    trigger_above = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=6,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=None,
        now=MARKET_OPEN_NOW,
    )
    assert trigger_above.should_escalate is False


# ── 3. Rate limits ───────────────────────────────────────────────────


def test_auto_trade_disabled_blocks_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_TRADE_ENABLED", raising=False)
    outcome = _ratchet(pl=40.0, peak=None)

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=None,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is False
    assert trigger.material_change == "ratchet_armed"  # the change WAS real
    assert trigger.reason == "auto_trade_disabled"


def test_live_mode_blocks_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("TRADING_MODE", "live")
    outcome = _ratchet(pl=40.0, peak=None)

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=None,
        now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is False
    assert trigger.reason == "live_mode_refused"


def test_market_closed_blocks_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=40.0, peak=None)

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None, escalations_today=0, last_escalated_peak_pct=None,
        now=MARKET_CLOSED_NOW,
    )
    assert trigger.should_escalate is False
    assert trigger.reason == "market_closed"


def test_cooldown_blocks_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=40.0, peak=None)
    last_escalation_at = MARKET_OPEN_NOW - timedelta(seconds=DEFAULT_COOLDOWN_S - 100)

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=last_escalation_at, escalations_today=1,
        last_escalated_peak_pct=None, now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is False
    assert trigger.reason == "cooldown_active"

    # Just past the cooldown window: must be allowed again.
    trigger_after = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=MARKET_OPEN_NOW - timedelta(seconds=DEFAULT_COOLDOWN_S + 1),
        escalations_today=1, last_escalated_peak_pct=None, now=MARKET_OPEN_NOW,
    )
    assert trigger_after.should_escalate is True


def test_daily_cap_blocks_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=40.0, peak=None)

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None, escalations_today=DEFAULT_MAX_PER_DAY,
        last_escalated_peak_pct=None, now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is False
    assert trigger.reason == "daily_cap_reached"

    trigger_below = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None, escalations_today=DEFAULT_MAX_PER_DAY - 1,
        last_escalated_peak_pct=None, now=MARKET_OPEN_NOW,
    )
    assert trigger_below.should_escalate is True


async def test_maybe_escalate_reads_cooldown_from_env_when_not_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``escalation_env_config()`` must actually be consulted by
    ``maybe_escalate`` — a literal ``DEFAULT_COOLDOWN_S`` baked into its
    signature would make ``OPTIONS_ESCALATION_COOLDOWN_S`` a documented
    no-op. 1000s has elapsed since the last escalation — past the
    DEFAULT 900s cooldown, so this would be ALLOWED if the default were
    used; with the env override raising it to 10000s it must stay
    blocked. Revert-checked: temporarily hardcoded
    `cooldown_s = DEFAULT_COOLDOWN_S` at the top of `maybe_escalate`,
    ignoring both the parameter and the env read — this test then failed
    (`should_escalate` came back True) — restored after confirming red."""
    _permissive_env(monkeypatch)
    monkeypatch.setenv("OPTIONS_ESCALATION_COOLDOWN_S", "10000")
    outcome = _ratchet(pl=40.0, peak=None)
    decision = _decision(
        proposal={"occSymbol": "NVDA260918C00225000", "isOption": True},
        reasoning={
            "option_exit": {
                "last_escalation_at": (MARKET_OPEN_NOW - timedelta(seconds=1000)).isoformat(),
            }
        },
    )

    trigger = await maybe_escalate(
        decision=decision, ratchet_outcome=outcome, dte=30, now=MARKET_OPEN_NOW,
        budget=EscalationBudget(), llm=_ScriptedLLM(bull_view={}, bear_view={}), guard=_guard(),
        caps=RiskCaps(options_disabled=False), session_factory=None,
    )
    assert trigger.should_escalate is False
    assert trigger.reason == "cooldown_active"


def test_escalation_budget_allows_one_then_denies() -> None:
    budget = EscalationBudget()
    assert budget.try_consume() is True
    assert budget.try_consume() is False
    assert budget.remaining == 0


async def test_fleet_tick_budget_exhausted_denies_a_second_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME budget instance, shared across two DIFFERENT positions in
    one simulated fleet tick (mirroring `reconciler_fleet.py` threading
    one `EscalationBudget` across every user's `manage_positions_for_user`
    call) — the second material change in the tick must be denied purely
    on the budget, with its own trigger still correctly detected."""
    _permissive_env(monkeypatch)
    budget = EscalationBudget()
    outcome = _ratchet(pl=40.0, peak=None)

    first = _decision(proposal={"occSymbol": "AAA260918C00100000", "isOption": True})
    second = _decision(proposal={"occSymbol": "BBB260918C00100000", "isOption": True})

    trigger_one = await maybe_escalate(
        decision=first, ratchet_outcome=outcome, dte=30, now=MARKET_OPEN_NOW,
        budget=budget, llm=_ScriptedLLM(bull_view={}, bear_view={}), guard=_guard(),
        caps=RiskCaps(options_disabled=False), session_factory=None,
    )
    trigger_two = await maybe_escalate(
        decision=second, ratchet_outcome=outcome, dte=30, now=MARKET_OPEN_NOW,
        budget=budget, llm=_ScriptedLLM(bull_view={}, bear_view={}), guard=_guard(),
        caps=RiskCaps(options_disabled=False), session_factory=None,
    )

    assert trigger_one.should_escalate is True
    assert trigger_two.should_escalate is False
    assert trigger_two.material_change == "ratchet_armed"  # real change, just no budget left
    assert trigger_two.reason == "fleet_tick_budget_exhausted"


# ── 4. End-to-end through maybe_escalate ─────────────────────────────


class _RaisingLLM:
    """Simulates an Anthropic outage / timeout: `complete_tools` raises
    before ever producing a response, so `run_tool_loop` cannot possibly
    have seen a tool call."""

    async def complete_tools(self, **_kwargs: Any) -> LLMResponse:
        raise RuntimeError("simulated Anthropic outage")


def _option_exit_writes(session_factory: _FakeSessionFactory) -> list[dict[str, Any]]:
    return [
        json.loads(params["payload"])
        for sess in session_factory.sessions
        for stmt, params in sess.execute_log
        if "'{option_exit}'" in str(stmt)
    ]


async def test_run_escalation_errored_on_llm_exception() -> None:
    """One layer below `maybe_escalate` — `run_escalation` itself must
    convert an LLM exception into `errored=True` with an EMPTY transcript,
    never let it propagate."""
    brief = PositionBrief(
        decision_id=str(uuid.uuid4()), underlying="NVDA", entry_premium=2.20,
        current_pl_pct=40.0, peak_pl_pct=40.0, trail_line_pct=None, armed=True,
        dte=30, days_held=2, thesis="NVDA breaks 190 within 3 weeks.",
        deadline=None, deadline_passed=False, trigger="ratchet_armed",
    )
    outcome = await run_escalation(
        brief=brief, user_id=str(uuid.uuid4()), now=MARKET_OPEN_NOW,
        llm=_RaisingLLM(), guard=_guard(), caps=RiskCaps(options_disabled=False),
    )
    assert outcome.errored is True
    assert outcome.tool_transcript == ()


async def test_run_escalation_mock_mode_emits_no_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = LLM(api_key=None)
    assert llm.mock is True

    brief = PositionBrief(
        decision_id=str(uuid.uuid4()), underlying="NVDA", entry_premium=2.20,
        current_pl_pct=40.0, peak_pl_pct=40.0, trail_line_pct=None, armed=True,
        dte=30, days_held=2, thesis="NVDA breaks 190 within 3 weeks.",
        deadline=None, deadline_passed=False, trigger="ratchet_armed",
    )
    outcome = await run_escalation(
        brief=brief, user_id=str(uuid.uuid4()), now=MARKET_OPEN_NOW,
        llm=llm, guard=_guard(), caps=RiskCaps(options_disabled=False),
    )
    assert outcome.errored is False
    assert outcome.tool_transcript == ()


async def test_escalation_failure_holds_and_places_no_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fail-safe test (docs §5.3): an LLM failure must never move a
    position. Revert-checked: temporarily removed the try/except around
    `run_tool_loop` in `escalation.py::run_escalation` — this test then
    raised `RuntimeError: simulated Anthropic outage` instead of
    completing, confirming it actually exercises the fail-safe path.
    Restored after confirming red, then green again."""
    _permissive_env(monkeypatch)
    uid, did = uuid.uuid4(), uuid.uuid4()
    option_exit_before = {
        "stop_loss_pct": 45.0, "take_profit_pct": 80.0, "adds_this_position": 0,
    }
    row = _seeded_row(uid=uid, did=did, option_exit=dict(option_exit_before))
    session_factory = _FakeSessionFactory(get_result=row)
    broker = FakeBroker()
    guard = _guard(session_factory=session_factory, broker_factory=lambda: broker)

    decision = _decision(
        user_id=str(uid), decision_id=str(did),
        proposal={"occSymbol": "NVDA260918C00225000", "isOption": True},
        reasoning={"option_exit": dict(option_exit_before)},
    )
    outcome = _ratchet(pl=40.0, peak=None)

    trigger = await maybe_escalate(
        decision=decision, ratchet_outcome=outcome, dte=30, now=MARKET_OPEN_NOW,
        budget=EscalationBudget(), llm=_RaisingLLM(), guard=guard,
        caps=RiskCaps(options_disabled=False), session_factory=session_factory,
    )

    assert trigger.should_escalate is True  # a real material change WAS present
    assert broker.orders == []  # nothing was ever dispatched to the broker

    writes = _option_exit_writes(session_factory)
    assert writes, "expected the escalation bookkeeping write to still happen"
    assert writes[-1]["stop_loss_pct"] == 45.0
    assert writes[-1]["take_profit_pct"] == 80.0
    assert writes[-1]["adds_this_position"] == 0


async def test_mock_mode_never_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors `test_llm_tools.py::test_mock_never_emits_tool_use`'s
    reasoning, through the REAL escalation path rather than
    `complete_tools()` in isolation — MOCK mode must be safe to run this
    specific path against in CI."""
    _permissive_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = LLM(api_key=None)
    assert llm.mock is True

    uid, did = uuid.uuid4(), uuid.uuid4()
    option_exit_before = {
        "stop_loss_pct": 45.0, "take_profit_pct": 80.0, "adds_this_position": 0,
    }
    row = _seeded_row(uid=uid, did=did, option_exit=dict(option_exit_before))
    session_factory = _FakeSessionFactory(get_result=row)
    broker = FakeBroker()
    guard = _guard(session_factory=session_factory, broker_factory=lambda: broker)

    decision = _decision(
        user_id=str(uid), decision_id=str(did),
        proposal={"occSymbol": "NVDA260918C00225000", "isOption": True},
        reasoning={"option_exit": dict(option_exit_before)},
    )
    outcome = _ratchet(pl=40.0, peak=None)
    assert outcome.armed  # sanity: this WOULD be a "ratchet_armed" material change

    trigger = await maybe_escalate(
        decision=decision, ratchet_outcome=outcome, dte=30, now=MARKET_OPEN_NOW,
        budget=EscalationBudget(), llm=llm, guard=guard,
        caps=RiskCaps(options_disabled=False), session_factory=session_factory,
    )

    assert trigger.should_escalate is True
    assert broker.orders == []  # MOCK never emits a tool_use block

    writes = _option_exit_writes(session_factory)
    assert writes[-1]["stop_loss_pct"] == 45.0
    assert writes[-1]["take_profit_pct"] == 80.0
    assert writes[-1]["adds_this_position"] == 0


class _StatefulRow:
    """Unlike `_FakeSessionFactory`'s fixed `get_result`, this row's
    `.reasoning` actually mutates across writes — needed to prove
    `_persist_escalation_attempt` merges over whatever the GUARD's own
    concurrent write (TIGHTEN_STOP, via `persist_option_state`) already
    committed, rather than clobbering it from a stale in-memory
    snapshot. See `escalation.py::_persist_escalation_attempt`'s
    docstring for the race this guards against.

    Carries every field ``guard.py::_load_open_option_decision`` actually
    reads (``user_id``/``closed_at``/``proposal``/``symbol``/``reasoning``/
    ``fill_qty``) — this row is read through the REAL guard, not a stub of
    it, so it needs the real shape.
    """

    def __init__(
        self, *, user_id: uuid.UUID, proposal: dict[str, Any], reasoning: dict[str, Any],
        symbol: str = "NVDA", fill_qty: int | None = 4, closed_at: datetime | None = None,
    ) -> None:
        self.user_id = user_id
        self.proposal = proposal
        self.reasoning = reasoning
        self.symbol = symbol
        self.fill_qty = fill_qty
        self.closed_at = closed_at


class _StatefulSession:
    def __init__(self, row: _StatefulRow) -> None:
        self._row = row

    async def get(self, _model: Any, _pk: Any) -> _StatefulRow:
        return self._row

    async def execute(self, _stmt: Any, params: Any = None) -> None:
        if params and "payload" in params:
            # Mirrors _option_exit_merge_stmt's real jsonb_set semantics:
            # a whole-VALUE replace of the `option_exit` key only.
            self._row.reasoning = {
                **self._row.reasoning, "option_exit": json.loads(params["payload"]),
            }

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _StatefulSession:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _StatefulSessionFactory:
    def __init__(self, row: _StatefulRow) -> None:
        self._row = row

    def __call__(self) -> _StatefulSession:
        return _StatefulSession(self._row)


async def test_maybe_escalate_tighten_stop_does_not_clobber_a_concurrent_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the REAL guard + REAL registry: the model calls
    TIGHTEN_STOP, the guard allows it (30 < the current 45) and persists
    the new stop via `tools/guard.py::persist_option_state` — THEN
    `maybe_escalate`'s own bookkeeping write must not revert that.

    Revert-checked: temporarily made `_persist_escalation_attempt` merge
    from `current = {}` (simulating "ignore whatever is actually in the
    DB right now") instead of the freshly-read row — this test then
    failed (`stop_loss_pct` back to 45.0 instead of the just-tightened
    30.0), confirming it actually exercises the fresh-read-before-merge
    fix. Restored after confirming red, then green again.
    """
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)

    uid, did = uuid.uuid4(), uuid.uuid4()
    option_exit_before = {
        "stop_loss_pct": 45.0, "take_profit_pct": 80.0, "adds_this_position": 0,
    }
    row = _StatefulRow(
        user_id=uid,
        proposal={"occSymbol": "NVDA260918C00225000", "isOption": True},
        reasoning={
            "option_exit": dict(option_exit_before),
            "contract_funnel": {"stage": "kept"},  # must survive untouched too
        },
    )
    session_factory = _StatefulSessionFactory(row)
    guard = _guard(session_factory=session_factory, clock=lambda: MARKET_OPEN_NOW)

    decision = _decision(
        user_id=str(uid), decision_id=str(did),
        proposal={"occSymbol": "NVDA260918C00225000", "isOption": True},
        reasoning={"option_exit": dict(option_exit_before)},
    )
    outcome = _ratchet(pl=40.0, peak=None)
    fake = _ScriptedLLM(
        bull_view={}, bear_view={},
        trade_responses=[_tool_call_response(
            "adjust_option_position",
            {
                "decision_id": str(did), "action": "TIGHTEN_STOP",
                "value": 30.0, "reason": "vol dropped",
            },
        )],
    )

    trigger = await maybe_escalate(
        decision=decision, ratchet_outcome=outcome, dte=30, now=MARKET_OPEN_NOW,
        budget=EscalationBudget(), llm=fake, guard=guard,
        caps=RiskCaps(options_disabled=False), session_factory=session_factory,
    )

    assert trigger.should_escalate is True
    # The guard's OWN write landed...
    assert row.reasoning["option_exit"]["stop_loss_pct"] == 30.0
    # ...and the escalation's bookkeeping write did NOT revert it, and did
    # not disturb a sibling key elsewhere in `reasoning`.
    assert row.reasoning["option_exit"]["take_profit_pct"] == 80.0
    assert row.reasoning["option_exit"]["adds_this_position"] == 0
    assert "last_escalation_at" in row.reasoning["option_exit"]
    assert row.reasoning["contract_funnel"] == {"stage": "kept"}


async def test_escalation_persists_bookkeeping_across_a_rollover_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """`escalations_today` resets when `escalations_date` is not "today" —
    a stale, malformed, or yesterday's counter must never suppress a
    fresh day's escalations."""
    _permissive_env(monkeypatch)
    outcome = _ratchet(pl=40.0, peak=None)
    yesterday = (MARKET_OPEN_NOW - timedelta(days=1)).date()

    state = load_escalation_state(
        {"escalations_today": DEFAULT_MAX_PER_DAY, "escalations_date": yesterday.isoformat()}
    )
    assert state.escalations_today == DEFAULT_MAX_PER_DAY

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=outcome, was_armed_before=False, dte=30,
        last_escalation_at=None,
        escalations_today=0,  # what the caller passes AFTER applying the rollover
        last_escalated_peak_pct=None, now=MARKET_OPEN_NOW,
    )
    assert trigger.should_escalate is True


def test_load_escalation_state_defaults_are_safe_on_malformed_input() -> None:
    """Garbage/missing values degrade to "never escalated" rather than
    raising — the same defensive contract every other reader of this
    JSONB blob in this codebase already follows."""
    state = load_escalation_state(
        {"last_escalation_at": "not-a-date", "escalations_today": "nonsense",
         "last_escalated_peak_pct": "nonsense", "armed": "true"}
    )
    assert state.last_escalation_at is None
    assert state.escalations_today == 0
    assert state.last_escalated_peak_pct is None
    assert state.was_armed is True  # "true" (a non-empty str) IS truthy — bool("true") is True


def test_build_position_brief_reads_entry_premium_and_thesis() -> None:
    decision = _decision(
        proposal={
            "occSymbol": "NVDA260918C00225000", "isOption": True,
            "rationale": "NVDA breaks 190 within 3 weeks on volume expansion.",
        },
        fill_avg_price=2.20,
    )
    # `_decision`'s own `entered_days_ago` is relative to the REAL
    # wall-clock `datetime.now(UTC)`, not the fixed `now` this test passes
    # to `build_position_brief` — set `entered_at` explicitly relative to
    # THAT `now` instead, or `days_held` would drift with the real date.
    decision.user_responded_at = MARKET_OPEN_NOW - timedelta(days=2)
    decision.triggered_at = decision.user_responded_at
    outcome = _ratchet(pl=40.0, peak=None)

    brief = build_position_brief(
        decision, ratchet_outcome=outcome, dte=12, trigger="ratchet_armed", now=MARKET_OPEN_NOW,
    )
    assert brief.entry_premium == 2.20
    assert brief.days_held == 2
    assert brief.thesis == "NVDA breaks 190 within 3 weeks on volume expansion."
    assert brief.deadline is not None
    assert brief.trigger == "ratchet_armed"
    assert brief.peak_pl_pct == outcome.peak_pl_pct

    rendered = _render_escalation_brief(brief)
    assert str(decision.id) in rendered
    assert "ratchet_armed" in rendered
