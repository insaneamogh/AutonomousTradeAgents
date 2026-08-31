"""IMPL_REFUSAL_LEDGER.md §6 revert-check matrix — the aggregation half.

`build_veto_ledger` / `build_ghost_summary` / `build_veto_exemplar` are
Postgres-only (they open a real `AsyncSession`), so — same convention as
`test_tenant_isolation.py` — these tests fake the session rather than
requiring a live database: a queue of canned per-`execute()` results,
fed in the exact order each builder issues its queries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from app.routers.insights import VetoRuleDto
from app.services.council import ghost_service

USER_ID = "11111111-1111-1111-1111-111111111111"


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _Result:
        return self

    def one(self) -> Any:
        return self._rows[0]


class _QueueSession:
    """Fake AsyncSession: pops the next canned result on each `execute()`.

    `build_veto_ledger` opens its own session for the main veto query, then
    calls `_trim_rows`, which opens a SECOND session for the reasoning
    query — both resolve to this SAME instance because
    `async_session_factory` is patched to always hand back `lambda: self`.
    """

    def __init__(self, results: list[list[Any]]) -> None:
        self._results = list(results)
        self.statements: list[Any] = []

    async def __aenter__(self) -> _QueueSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        rows = self._results.pop(0) if self._results else []
        return _Result(rows)


def _patch(monkeypatch: pytest.MonkeyPatch, session: _QueueSession) -> None:
    monkeypatch.setattr(ghost_service, "async_session_factory", lambda: lambda: session)


def _decision(
    *,
    rule: str | None = "max_premium_pct",
    proposal: dict[str, Any] | None = None,
    triggered_at: datetime | None = None,
    symbol: str = "NVDA",
    bull_case: str = "",
    bear_case: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        symbol=symbol,
        risk_veto_rule=rule,
        proposal=proposal if proposal is not None else {},
        triggered_at=triggered_at or datetime.now(UTC),
        bull_case=bull_case,
        bear_case=bear_case,
    )


def _ghost(
    *,
    status: str = "final",
    ghost_pnl: float | None = -100.0,
    reason: str = "vetoed",
    side: str = "BUY",
    qty: int = 10,
    entry_price: float = 100.0,
    last_price: float | None = 95.0,
    horizon_days: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        ghost_pnl=ghost_pnl,
        reason=reason,
        side=side,
        qty=qty,
        entry_price=entry_price,
        last_price=last_price,
        horizon_days=horizon_days,
    )


# ── test_pending_ghosts_excluded_from_prevented_loss ───────────────────


def test_pending_ghosts_excluded_from_prevented_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `pending`/`partial` ghost must never contribute to `ghostPnl` /
    `preventedLossUsd` — only `status == "final"` counts (the honesty
    rule, §4.1). A `pending` row has no mark yet (`ghost_pnl` is None, so
    it's trivially excluded); the load-bearing case is `partial`, which
    CAN carry a real interim `ghost_pnl` and must be excluded on STATUS
    alone. Break this by summing every ghost regardless of status."""
    dec = _decision(proposal={"estimatedNotional": 5000.0})
    pending_ghost = _ghost(status="pending", ghost_pnl=None)
    session = _QueueSession([[(dec, pending_ghost)], []])
    _patch(monkeypatch, session)

    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))

    assert ledger.total_vetoes == 1
    row = ledger.rules[0]
    assert row.ghost_pnl is None
    assert row.prevented_loss_usd is None, "a pending ghost must render as unknown, not $0/realized"

    partial_dec = _decision(proposal={"estimatedNotional": 5000.0})
    partial_ghost = _ghost(status="partial", ghost_pnl=-250.0)
    session2 = _QueueSession([[(partial_dec, partial_ghost)], []])
    _patch(monkeypatch, session2)

    ledger2 = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))
    assert ledger2.rules[0].prevented_loss_usd is None, (
        "a partial ghost's interim mark must not be counted as prevented loss either"
    )


# ── test_trims_not_counted_as_vetoes ────────────────────────────────────


def test_trims_not_counted_as_vetoes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`total_vetoes` must count only rows with a `risk_veto_rule` — trims
    (approved-but-shrunk rows, surfaced separately via `total_trims`) must
    never inflate it. Break this by returning
    `total_vetoes + total_trims`."""
    dec = _decision(rule="single_name_concentration", proposal={"estimatedNotional": 4900.0})
    ghost = _ghost(status="final", ghost_pnl=-50.0)
    veto_rows = [(dec, ghost)]
    trim_reasonings = [
        {"risk_trim_rules": ["max_premium_pct_trim"]},
        {"risk_trim_rules": ["max_premium_pct_trim"]},
        {"risk_trim_rules": ["max_position_pct_trim"]},
    ]
    session = _QueueSession([veto_rows, trim_reasonings])
    _patch(monkeypatch, session)

    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))

    assert ledger.total_vetoes == 1, "trims must not be folded into the veto count"
    assert ledger.total_trims == 3
    assert {t.rule: t.count for t in ledger.trims} == {
        "max_premium_pct_trim": 2,
        "max_position_pct_trim": 1,
    }


# ── test_veto_row_without_notional_does_not_crash ──────────────────────


@pytest.mark.parametrize(
    "proposal",
    [
        {},
        {"side": "BUY", "qty": 10},
        None,
    ],
)
def test_veto_row_without_notional_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, proposal: dict[str, Any] | None
) -> None:
    """Neither `estimatedNotional` nor `estimated_notional` present (or no
    proposal at all) must degrade to `blockedNotional == 0`, never raise."""
    dec = _decision(proposal=proposal)
    ghost = _ghost(status="final", ghost_pnl=-10.0)
    session = _QueueSession([[(dec, ghost)], []])
    _patch(monkeypatch, session)

    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))

    assert ledger.rules[0].blocked_notional == 0.0


def test_veto_row_notional_is_read_from_either_key_casing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write side persists a vetoed proposal's raw (pre-DTO) dict under
    `estimated_notional`; the camelCase DTO shape uses `estimatedNotional`.
    Both must be summed correctly — this is the exact bug IMPL_REFUSAL_
    LEDGER.md §0 traced the live "$0 blocked" dashboard to."""
    snake_case_row = _decision(proposal={"estimated_notional": 4922.08})
    camel_case_row = _decision(proposal={"estimatedNotional": 4627.08})
    ghost = _ghost(status="final", ghost_pnl=-10.0)
    session = _QueueSession([[(snake_case_row, ghost), (camel_case_row, ghost)], []])
    _patch(monkeypatch, session)

    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))

    assert ledger.rules[0].blocked_notional == pytest.approx(4922.08 + 4627.08)


# ── test_null_prevented_loss_renders_pending_not_zero ──────────────────


def test_null_prevented_loss_renders_pending_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """No finalized ghost at all under a rule -> `preventedLossUsd` must
    serialize as JSON `null`, never `0`. `0` claims a measurement was
    made; `null` admits none has finalized yet."""
    dec = _decision(proposal={"estimatedNotional": 1000.0})
    session = _QueueSession([[(dec, None)], []])
    _patch(monkeypatch, session)

    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))
    row = ledger.rules[0]
    assert row.prevented_loss_usd is None

    dto = VetoRuleDto(
        rule=row.rule,
        count=row.count,
        blocked_notional=row.blocked_notional,
        ghost_pnl=row.ghost_pnl,
        prevented_loss_usd=row.prevented_loss_usd,
        last_at=None,
    )
    dumped = dto.model_dump(by_alias=True)
    assert dumped["preventedLossUsd"] is None, "must serialize as null, not 0"
    assert dumped["preventedLossUsd"] != "0"


# ── test_missed_upside_is_shown_not_hidden ──────────────────────────────


def test_missed_upside_is_shown_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DECLINED pick that would have made money is `missed_usd` — it
    must be reported, not zeroed out or folded into `saved_usd`."""
    dec = _decision()
    declined_winner = _ghost(status="final", ghost_pnl=300.0, reason="declined")
    session = _QueueSession([[(declined_winner, dec.triggered_at)]])
    _patch(monkeypatch, session)

    summary = anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID))

    assert summary.missed_usd == 300.0
    assert summary.saved_usd == 0.0


def test_saved_and_missed_both_populate_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vetoed pick that WOULD have lost money -> saved_usd; a declined
    pick that WOULD have gained -> missed_usd. Neither should suppress the
    other when both exist in the same window."""
    t = datetime.now(UTC)
    vetoed_loser = _ghost(status="final", ghost_pnl=-400.0, reason="vetoed")
    declined_winner = _ghost(status="final", ghost_pnl=250.0, reason="declined")
    session = _QueueSession([[(vetoed_loser, t), (declined_winner, t)]])
    _patch(monkeypatch, session)

    summary = anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID))

    assert summary.saved_usd == 400.0
    assert summary.missed_usd == 250.0


# ── test_exemplar_picks_the_largest_finalized_ghost ────────────────────


def test_exemplar_picks_the_largest_finalized_ghost(monkeypatch: pytest.MonkeyPatch) -> None:
    """The story trade is the LARGEST abs(ghostPnl), not the most recent
    one -- a small, brand-new refusal must not outrank a big older one."""
    now = datetime.now(UTC)
    old_but_big = _decision(
        rule="max_premium_pct", triggered_at=now - timedelta(days=3), symbol="NVDA"
    )
    old_ghost = _ghost(status="final", ghost_pnl=-1476.0)
    recent_but_small = _decision(
        rule="max_premium_pct", triggered_at=now - timedelta(hours=1), symbol="AAPL"
    )
    recent_ghost = _ghost(status="final", ghost_pnl=-12.0)
    session = _QueueSession([[(old_but_big, old_ghost), (recent_but_small, recent_ghost)]])
    _patch(monkeypatch, session)

    exemplar = anyio.run(
        lambda: ghost_service.build_veto_exemplar("max_premium_pct", user_id=USER_ID)
    )

    assert exemplar is not None
    assert exemplar.symbol == "NVDA", "must pick the largest |ghostPnl|, not the most recent row"
    assert exemplar.prevented_loss_usd == 1476.0


def test_exemplar_ignores_unfinalized_ghosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """None -> the caller renders 'pending', not a 404-shaped empty rule."""
    session = _QueueSession([[]])
    _patch(monkeypatch, session)

    exemplar = anyio.run(
        lambda: ghost_service.build_veto_exemplar("max_premium_pct", user_id=USER_ID)
    )
    assert exemplar is None


# ── test_ledger_scopes_to_the_window ────────────────────────────────────


def test_ledger_scopes_to_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The emitted SQL's cutoff must actually move with `window_days` --
    break this by hardcoding the cutoff or dropping the predicate."""

    def _cutoff_param(window_days: int) -> datetime:
        session = _QueueSession([[], []])
        _patch(monkeypatch, session)
        anyio.run(lambda: ghost_service.build_veto_ledger(window_days, user_id=USER_ID))
        stmt = session.statements[0]
        compiled = stmt.compile()
        cutoff_values = [
            v for v in compiled.params.values() if isinstance(v, datetime)
        ]
        assert cutoff_values, "expected a bound datetime cutoff parameter"
        return min(cutoff_values)

    short_window = _cutoff_param(1)
    long_window = _cutoff_param(300)

    assert long_window < short_window, "a wider window must reach further back"
    assert (short_window - long_window) > timedelta(days=250)
