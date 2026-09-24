"""End-of-day job: ghost marking runs independent of the council, and the
daily report renders the right numbers without leaking them to a push."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.council import eod_report
from app.services.council.eod_report import (
    DailyReport,
    build_daily_report,
    push_body,
    render_report,
    run_eod,
)

_USER = "00000000-0000-0000-0000-000000000001"
_DAY = date(2026, 9, 24)


def _report(**kw: Any) -> DailyReport:
    base = dict(
        day=_DAY, equity=97_012.55, day_pnl=-412.30, day_pnl_pct=-0.42,
        realized_today=-536.0, closes_by_reason={"option_stop_loss": 1, "protective_stop": 1},
        open_positions=3, unrealized=120.5, breaker_status="halted",
        decisions_today=14, llm_spend_usd=0.37, llm_calls=41,
    )
    base.update(kw)
    return DailyReport(**base)


def test_render_carries_the_numbers_and_the_breaker() -> None:
    title, body = render_report(_report())
    assert title == "Daily report 2026-09-24"
    assert "Equity $97,012.55, day -$412.30 (-0.42%)" in body
    assert "Closed 2: realized -$536.00 (option_stop_loss 1, protective_stop 1)" in body
    assert "Open positions 3, unrealized +$120.50" in body
    assert "LLM $0.37 over 41 call(s)" in body
    assert "Breaker HALTED" in body


def test_push_body_is_lock_screen_safe() -> None:
    body = push_body(_report())
    assert "$" not in body
    assert "97" not in body and "412" not in body and "536" not in body
    assert "2 closed" in body and "3 open" in body and "breaker HALTED" in body


def _session_factory(results: list[Any]) -> Any:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=results)

    class _CM:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_a: object) -> None:
            return None

    return lambda: _CM()


def _result(**kw: Any) -> MagicMock:
    r = MagicMock()
    for k, v in kw.items():
        getattr(r, k).return_value = v
    return r


async def test_build_assembles_snapshot_closes_breaker_and_spend() -> None:
    snap = SimpleNamespace(
        account_equity=Decimal("97012.55"), daily_pnl=Decimal("-412.30"),
        daily_pnl_pct=Decimal("-0.420"),
        open_positions=[{"unrealized_pl": 100.25}, {"unrealized_pl": -20.0}],
    )
    factory = _session_factory([
        _result(scalar_one_or_none=snap),
        _result(all=[("option_stop_loss", Decimal("-536.00")), (None, None)]),
        _result(scalar_one=14),
        _result(scalar_one_or_none="halted"),
        _result(one=(Decimal("0.371"), 41)),
    ])

    r = await build_daily_report(user_id=_USER, session_factory=factory, day=_DAY)

    assert r.equity == 97012.55
    assert r.day_pnl == -412.30
    assert r.realized_today == -536.0
    assert r.closes_by_reason == {"option_stop_loss": 1, "unknown": 1}
    assert r.open_positions == 2
    assert r.unrealized == 80.25
    assert r.breaker_status == "halted"
    assert r.decisions_today == 14
    assert r.llm_calls == 41


async def test_ghosts_are_marked_before_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def _ghosts(day: date) -> dict:
        order.append("ghosts")
        return {"created": 2, "updated": 5, "finalized": 1}

    async def _build(**_k: Any) -> DailyReport:
        order.append("build")
        return _report()

    delivered: list[DailyReport] = []
    monkeypatch.setattr(eod_report, "_mark_ghosts", _ghosts)
    monkeypatch.setattr(eod_report, "build_daily_report", _build)
    monkeypatch.setattr(eod_report, "_deliver", lambda _u, r: delivered.append(r))

    report = await run_eod(user_id=_USER, session_factory=None, day=_DAY)

    assert order == ["ghosts", "build"]
    assert report is not None and report.ghost == {"created": 2, "updated": 5, "finalized": 1}
    assert delivered == [report]


async def test_a_ghost_failure_does_not_stop_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_agents.jobs import ghost_eval

    async def _boom(**_k: Any) -> dict:
        raise RuntimeError("no price data")

    async def _build(**_k: Any) -> DailyReport:
        return _report()

    delivered: list[DailyReport] = []
    monkeypatch.setattr(ghost_eval, "evaluate_ghosts", _boom)
    monkeypatch.setattr(eod_report, "build_daily_report", _build)
    monkeypatch.setattr(eod_report, "_deliver", lambda _u, r: delivered.append(r))

    report = await run_eod(user_id=_USER, session_factory=None, day=_DAY)

    assert report is not None and report.ghost is None
    assert len(delivered) == 1


async def test_a_build_failure_returns_none_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ghosts(day: date) -> dict:
        return {}

    async def _build(**_k: Any) -> DailyReport:
        raise RuntimeError("db down")

    monkeypatch.setattr(eod_report, "_mark_ghosts", _ghosts)
    monkeypatch.setattr(eod_report, "build_daily_report", _build)
    monkeypatch.setattr(eod_report, "_deliver", lambda *_a: pytest.fail("nothing to deliver"))

    assert await run_eod(user_id=_USER, session_factory=None, day=_DAY) is None
