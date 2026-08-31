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

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from test_tool_guard import (
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
from engine.options.selection import ContractQuote
from engine.risk import MockRiskContextProvider, RiskCaps
from trading_agents.memory import InMemoryDecisionLog
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


async def test_exit_now_end_to_end_closes_the_broker_position() -> None:
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


async def test_exit_now_with_no_broker_position_is_a_safe_no_op() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    session_factory = _FakeSessionFactory(
        get_result=_seeded_row(uid=uid, did=did, option_exit={})
    )
    broker = FakeBroker(position=None)
    guard = _guard(session_factory=session_factory, broker_factory=lambda: broker)
    ctx = _ctx(user_id=str(uid))

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
