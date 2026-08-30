"""Auto-approver gate matrix — see docs/PLAN_AUTO_APPROVE.md §4.

Every test starts from a fully "everything passes" baseline (operator env
on, paper mode, per-connection consent granted, market open, one fresh
eligible pending proposal, budget/breaker both clear) via
``_install_happy_path``, then breaks exactly ONE thing and asserts the
sweep refuses (returns 0, touches neither ``execute_proposal`` nor the
audit stamp). Per CLAUDE.md §4.1, each of these was hand-verified during
development by reverting the corresponding gate in ``auto_approver.py``
and confirming the matching test actually fails before restoring the fix —
a test that cannot be made to fail proves nothing.

No real Postgres/broker involved: every I/O seam (``_resolve_paper_connection``,
``is_us_market_open``, ``get_store``, ``_auto_approvals_today``,
``load_db_risk_state``, ``execute_proposal``, ``_stamp_auto_approval``) is
monkeypatched at the ``auto_approver`` module level, mirroring the seam-
patching style already used in ``test_reconciler_fleet_poller.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.orders import auto_approver as aa

# ─────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _FakeProposal:
    id: str
    symbol: str = "NVDA"
    proposed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _FakeConn:
    id: str = "conn-1"
    auto_approve_consent: bool = True


@dataclass
class _FakeStore:
    pending: list[Any]

    async def list_pending(self, user_id: str) -> list[Any]:
        return list(self.pending)


@dataclass
class _FakeExecuteResult:
    risk_blocked: bool = False
    risk_veto_rule: str | None = None
    order: Any = None


@dataclass
class _FakeDbState:
    drawdown_halted: bool = False


class _Sentinel:
    """Stands in for ``session_factory``. Never actually called in these
    tests — every helper that would use it is monkeypatched away."""


# ─────────────────────────────────────────────────────────────────────
# Happy-path installer
# ─────────────────────────────────────────────────────────────────────


def _install_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pending: list[Any] | None = None,
    consent: bool = True,
    market_open: bool = True,
    today_count: int = 0,
    drawdown_halted: bool = False,
    execute_result: Any = None,
    execute_side_effect: Exception | None = None,
) -> list[tuple[str, str]]:
    """Wire every gate to PASS. Returns ``calls``: a log of
    ``(proposal_id, tag)`` appended by the ``execute_proposal`` and
    ``_stamp_auto_approval`` fakes — ``tag`` is ``exit_mode`` for an
    execute call, or the literal string ``"STAMPED"`` for a stamp call.
    """
    calls: list[tuple[str, str]] = []

    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("AUTO_APPROVE_MAX_AGE_MIN", raising=False)
    monkeypatch.delenv("AUTO_APPROVE_MAX_PER_DAY", raising=False)

    async def fake_resolve_conn(user_id: str) -> _FakeConn | None:
        return _FakeConn(auto_approve_consent=consent)

    monkeypatch.setattr(aa, "_resolve_paper_connection", fake_resolve_conn)
    monkeypatch.setattr(aa, "is_us_market_open", lambda now: market_open)

    proposals = pending if pending is not None else [_FakeProposal(id="p-1")]
    monkeypatch.setattr(aa, "get_store", lambda: _FakeStore(pending=proposals))

    async def fake_today(session_factory: Any, user_id: str) -> int:
        return today_count

    monkeypatch.setattr(aa, "_auto_approvals_today", fake_today)

    async def fake_db_state(session_factory: Any, *, user_id: str) -> _FakeDbState:
        return _FakeDbState(drawdown_halted=drawdown_halted)

    monkeypatch.setattr(aa, "load_db_risk_state", fake_db_state)

    result = execute_result if execute_result is not None else _FakeExecuteResult()

    async def fake_execute(
        *, user_id: str, proposal_id: str, risk_caps: Any = None, exit_mode: str
    ) -> _FakeExecuteResult:
        calls.append((proposal_id, exit_mode))
        if execute_side_effect is not None:
            raise execute_side_effect
        return result

    monkeypatch.setattr(aa, "execute_proposal", fake_execute)

    async def fake_stamp(session_factory: Any, *, user_id: str, proposal_id: str) -> None:
        calls.append((proposal_id, "STAMPED"))

    monkeypatch.setattr(aa, "_stamp_auto_approval", fake_stamp)

    return calls


# ─────────────────────────────────────────────────────────────────────
# Gate 1 — operator kill switch
# ─────────────────────────────────────────────────────────────────────


async def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: default AUTO_APPROVE_ENABLED to on."""
    calls = _install_happy_path(monkeypatch)
    monkeypatch.delenv("AUTO_APPROVE_ENABLED", raising=False)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


# ─────────────────────────────────────────────────────────────────────
# Gate 2 — HARD-CODED paper-only (the single most important test here)
# ─────────────────────────────────────────────────────────────────────


async def test_never_auto_approves_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single most important test in this plan. Break: make gate 2
    respect some env flag instead of being unconditional — even with the
    operator switch ON, TRADING_MODE=live must refuse regardless."""
    calls = _install_happy_path(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "live")
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


async def test_never_auto_approves_when_live_trading_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other half of gate 2's AND: LIVE_TRADING_ENABLED=1 refuses even if
    TRADING_MODE still reads paper."""
    calls = _install_happy_path(monkeypatch)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


# ─────────────────────────────────────────────────────────────────────
# Gate 2b — per-connection auto-approve consent (Part 2's two-key gate)
# ─────────────────────────────────────────────────────────────────────


async def test_requires_per_connection_auto_approve_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator env alone is not enough — the account owner's own
    in-app toggle must also be on. Break: drop the consent check."""
    calls = _install_happy_path(monkeypatch, consent=False)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


async def test_requires_the_operator_env_even_with_consent_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: consent alone, without AUTO_APPROVE_ENABLED, is
    not enough — proves the two keys are a real AND, not an OR."""
    calls = _install_happy_path(monkeypatch, consent=True)
    monkeypatch.delenv("AUTO_APPROVE_ENABLED", raising=False)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


async def test_no_connection_at_all_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """No active paper connection resolves → refuse, don't crash on None."""
    calls = _install_happy_path(monkeypatch)

    async def no_conn(user_id: str) -> None:
        return None

    monkeypatch.setattr(aa, "_resolve_paper_connection", no_conn)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


# ─────────────────────────────────────────────────────────────────────
# Gate 3 — US market hours
# ─────────────────────────────────────────────────────────────────────


async def test_does_not_approve_outside_market_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: drop the is_us_market_open check."""
    calls = _install_happy_path(monkeypatch, market_open=False)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


# ─────────────────────────────────────────────────────────────────────
# Gate 4 — proposal freshness
# ─────────────────────────────────────────────────────────────────────


async def test_skips_a_stale_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: drop the age check."""
    stale = _FakeProposal(
        id="p-stale", proposed_at=datetime.now(UTC) - timedelta(minutes=61)
    )
    calls = _install_happy_path(monkeypatch, pending=[stale])
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


async def test_a_fresh_proposal_within_the_age_window_is_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _FakeProposal(
        id="p-fresh", proposed_at=datetime.now(UTC) - timedelta(minutes=59)
    )
    calls = _install_happy_path(monkeypatch, pending=[fresh])
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 1
    assert ("p-fresh", "agent") in calls


# ─────────────────────────────────────────────────────────────────────
# Gate 5 — daily budget
# ─────────────────────────────────────────────────────────────────────


async def test_stops_at_the_daily_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: remove the per-day count. Default budget is 5."""
    calls = _install_happy_path(monkeypatch, today_count=5)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


async def test_proceeds_when_under_the_daily_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_happy_path(monkeypatch, today_count=4)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 1
    assert calls  # execute_proposal + the stamp both fired


# ─────────────────────────────────────────────────────────────────────
# Gate 6 — per-tick cap of ONE
# ─────────────────────────────────────────────────────────────────────


async def test_at_most_one_per_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: remove the per-tick cap (e.g. loop over every eligible
    proposal instead of picking exactly one)."""
    many = [_FakeProposal(id=f"p-{i}") for i in range(5)]
    calls = _install_happy_path(monkeypatch, pending=many)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 1
    executed = {pid for pid, tag in calls if tag == "agent"}
    assert len(executed) == 1


# ─────────────────────────────────────────────────────────────────────
# Gate 7 — drawdown circuit breaker
# ─────────────────────────────────────────────────────────────────────


async def test_refuses_when_the_breaker_is_tripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: drop the breaker check."""
    calls = _install_happy_path(monkeypatch, drawdown_halted=True)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    assert calls == []


# ─────────────────────────────────────────────────────────────────────
# Execution semantics — risk block, exit mode, audit stamp, resilience
# ─────────────────────────────────────────────────────────────────────


async def test_risk_blocked_leaves_the_proposal_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: mark it declined/approved on a risk block instead of leaving
    it pending."""
    blocked = _FakeExecuteResult(risk_blocked=True, risk_veto_rule="max_position_pct_trim")
    calls = _install_happy_path(monkeypatch, execute_result=blocked)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0
    # An attempt WAS made...
    assert ("p-1", "agent") in calls
    # ...but a blocked attempt must never be stamped autonomous.
    assert ("p-1", "STAMPED") not in calls


async def test_stamps_approval_mode_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: leave approval_mode as 'ask' (don't call the stamp)."""
    calls = _install_happy_path(monkeypatch)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 1
    assert ("p-1", "STAMPED") in calls


async def test_uses_agent_exit_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: pass exit_mode='manual' — an auto-opened manual position is
    orphaned, owned by nobody."""
    calls = _install_happy_path(monkeypatch)
    await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert ("p-1", "agent") in calls


async def test_a_broker_failure_does_not_kill_the_fleet_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break: let the exception escape (remove the try/except around the
    execute_proposal call)."""
    _install_happy_path(monkeypatch, execute_side_effect=RuntimeError("broker down"))
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0


async def test_a_connection_lookup_failure_is_also_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate 2b's own I/O (the connection lookup) must be inside the same
    try/except as execution, not just the caller's. Break: narrow the
    try/except back to start at gate 5 (right before the daily-budget
    check) instead of gate 2b, and this raises straight out of
    ``auto_approve_for_user`` instead of returning 0 — the function's own
    docstring claim of "never raises" would then only hold because
    ``ReconcilerFleet.tick()`` happens to wrap the call too, not because
    this function actually keeps its own promise."""
    _install_happy_path(monkeypatch)

    async def fake_resolve_conn_raises(user_id: str) -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(aa, "_resolve_paper_connection", fake_resolve_conn_raises)
    n = await aa.auto_approve_for_user(user_id="u1", session_factory=_Sentinel())
    assert n == 0


# ─────────────────────────────────────────────────────────────────────
# Schema fit
# ─────────────────────────────────────────────────────────────────────


def test_auto_fits_approval_mode_column() -> None:
    """agent_decisions.approval_mode is String(10) — "auto" must fit
    without truncation. Catches this here, not in production."""
    from engine.db.models import AgentDecision

    col = AgentDecision.__table__.columns["approval_mode"]
    assert len("auto") <= col.type.length
