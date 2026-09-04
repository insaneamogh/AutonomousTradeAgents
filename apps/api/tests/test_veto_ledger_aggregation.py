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

from app.routers.insights import GhostBucketDto, VetoRuleDto
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


# ── test_bucket_names_the_oldest_pending_marks_remaining_trading_days ──


def test_bucket_names_the_oldest_pending_marks_remaining_trading_days() -> None:
    """A bare pending count reads as "broken or unbuilt" with no context —
    the frontend needs to say WHEN the next mark resolves. Must pick the
    OLDEST pending row (soonest to finalize), not the newest, and must use
    the real `trading_day_offset` math `ghost_eval` itself finalizes on —
    two independent day-counters could disagree by a day around a
    weekend."""
    now = datetime(2026, 6, 10, tzinfo=UTC)  # a Wednesday
    newer_pending = _ghost(status="pending", ghost_pnl=None, horizon_days=5, reason="vetoed")
    older_pending = _ghost(status="partial", ghost_pnl=-10.0, horizon_days=5, reason="vetoed")
    finalized = _ghost(status="final", ghost_pnl=-500.0, reason="vetoed")
    rows = [
        (newer_pending, datetime(2026, 6, 9, tzinfo=UTC)),  # 1 trading day old
        (older_pending, datetime(2026, 6, 5, tzinfo=UTC)),  # Friday -> 3 trading days old by Wed
        (finalized, datetime(2026, 6, 1, tzinfo=UTC)),
    ]

    bucket = ghost_service._bucket_from_rows(rows, ("vetoed",), now=now)

    assert bucket.count == 3
    assert bucket.pending_count == 2
    assert bucket.oldest_pending_triggered_at == datetime(2026, 6, 5, tzinfo=UTC), (
        "must pick the OLDER pending row, not whichever sorts first in the rows list"
    )
    # Friday -> Wednesday is 3 elapsed trading days (Mon, Tue, Wed); a
    # 5-day horizon leaves 2.
    assert bucket.oldest_pending_remaining_trading_days == 2


def test_bucket_clamps_remaining_trading_days_at_zero_when_overdue() -> None:
    """A ghost that should have finalized (elapsed >= horizon) but hasn't
    yet — e.g. today's evaluator pass hasn't run — must read as "any day
    now" (remaining == 0), never a negative countdown."""
    now = datetime(2026, 6, 10, tzinfo=UTC)
    stuck = _ghost(status="partial", ghost_pnl=-5.0, horizon_days=1, reason="vetoed")
    rows = [(stuck, datetime(2026, 6, 1, tzinfo=UTC))]

    bucket = ghost_service._bucket_from_rows(rows, ("vetoed",), now=now)

    assert bucket.oldest_pending_remaining_trading_days == 0


def test_bucket_has_no_pending_countdown_once_everything_finalizes() -> None:
    """No pending rows -> both new fields must be None, not 0 — a `0`
    would misleadingly claim something is about to resolve when nothing
    is outstanding at all."""
    now = datetime(2026, 6, 10, tzinfo=UTC)
    finalized = _ghost(status="final", ghost_pnl=-500.0, reason="vetoed")
    rows = [(finalized, datetime(2026, 6, 1, tzinfo=UTC))]

    bucket = ghost_service._bucket_from_rows(rows, ("vetoed",), now=now)

    assert bucket.pending_count == 0
    assert bucket.oldest_pending_triggered_at is None
    assert bucket.oldest_pending_remaining_trading_days is None


def test_ghost_summary_wires_the_pending_countdown_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through `build_ghost_summary` (not just the pure
    `_bucket_from_rows` helper) — confirms the DB-facing wrapper actually
    threads a real `now` through rather than only the pure function being
    correct in isolation."""
    t = datetime.now(UTC) - timedelta(days=1)
    pending = _ghost(status="pending", ghost_pnl=None, horizon_days=5, reason="vetoed")
    session = _QueueSession([[(pending, t)]])
    _patch(monkeypatch, session)

    summary = anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID))

    assert summary.vetoed.pending_count == 1
    assert summary.vetoed.oldest_pending_triggered_at == t
    assert summary.vetoed.oldest_pending_remaining_trading_days is not None
    assert summary.vetoed.oldest_pending_remaining_trading_days >= 0


def test_ghost_bucket_dto_serializes_the_pending_countdown_camel_cased() -> None:
    """The router's DTO must round-trip both new fields to the exact
    camelCase names `packages/shared-types` and the frontend expect, and
    must serialize a `None` countdown as JSON `null` (no pending row),
    never `0` — the same honesty rule as `preventedLossUsd`."""
    t = datetime(2026, 6, 5, tzinfo=UTC)
    dto = GhostBucketDto(
        count=3,
        ghost_pnl=-10.0,
        pending_count=2,
        oldest_pending_triggered_at=t.isoformat(),
        oldest_pending_remaining_trading_days=2,
    )
    dumped = dto.model_dump(by_alias=True)
    assert dumped["oldestPendingTriggeredAt"] == t.isoformat()
    assert dumped["oldestPendingRemainingTradingDays"] == 2

    empty_dto = GhostBucketDto(count=0, ghost_pnl=0.0, pending_count=0)
    empty_dumped = empty_dto.model_dump(by_alias=True)
    assert empty_dumped["oldestPendingTriggeredAt"] is None
    assert empty_dumped["oldestPendingRemainingTradingDays"] is None


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


def test_ledger_reports_the_real_live_risk_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test for the 2026-09-01 fix: `VetoLedger.risk_profile` did
    not exist at all, so the mobile client's "under the X% caps" disclosure
    caption always fell back to a generic placeholder. Hits the real
    `build_veto_ledger` (not a mocked hook) so this can't recur silently —
    reuses `RiskCaps.from_env()`'s own profile-name resolution, so this
    test would also catch the two ever disagreeing in the future."""
    session = _QueueSession([[], []])
    _patch(monkeypatch, session)

    monkeypatch.setenv("RISK_PROFILE", "aggressive_paper")
    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))
    assert ledger.risk_profile == "aggressive_paper"

    session2 = _QueueSession([[], []])
    _patch(monkeypatch, session2)
    monkeypatch.delenv("RISK_PROFILE", raising=False)
    ledger2 = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))
    assert ledger2.risk_profile == "conservative"


def test_ledger_reports_risk_profile_even_for_an_unknown_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The early-return path (no matching tenant) must populate
    `risk_profile` too — it's a required field, not optional."""
    monkeypatch.setenv("RISK_PROFILE", "aggressive_paper")
    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id="not-a-real-user"))
    assert ledger.risk_profile == "aggressive_paper"
    assert ledger.total_vetoes == 0


# ── marked (partial-inclusive) per-rule numbers ────────────────────────


def test_partial_marks_populate_marked_pnl_without_touching_ghost_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live 2026-09-04 case. A ghost finalizes only after
    `horizon_days` TRADING days, so on submission day all 101 marked
    refusals were still `partial` and every per-rule "would have" cell
    rendered "pending" — while the table already held real
    marked-to-market counterfactuals priced from live Alpaca option quotes.

    `ghost_pnl` stays FINALS ONLY (that is the number the product stands
    behind); `marked_pnl` carries the provisional one alongside it. Break
    this by restoring `status == "final"` on the marked branch."""
    dec = _decision(proposal={"estimatedNotional": 5000.0})
    partial = _ghost(status="partial", ghost_pnl=-250.0)
    session = _QueueSession([[(dec, partial)], []])
    _patch(monkeypatch, session)

    row = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID)).rules[0]

    assert row.ghost_pnl is None, "a partial must never become the settled number"
    assert row.prevented_loss_usd is None
    assert row.marked_pnl == pytest.approx(-250.0)
    assert row.marked_count == 1


def test_marked_split_reports_both_sides_not_just_the_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule can block real losses AND real gains. Netting them is what
    made the tiles read as broken: 2026-09-04's vetoed bucket held $30,788
    avoided and $32,967 blocked, netting +$2,179, and `max(0, -2179)`
    floored the tile to "$—" — rendering "our vetoes cost us money"
    identically to "no data"."""
    winner = _decision(proposal={"estimatedNotional": 1000.0})
    loser = _decision(proposal={"estimatedNotional": 1000.0})
    session = _QueueSession(
        [
            [
                (winner, _ghost(status="partial", ghost_pnl=900.0)),
                (loser, _ghost(status="partial", ghost_pnl=-400.0)),
            ],
            [],
        ]
    )
    _patch(monkeypatch, session)

    row = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID)).rules[0]

    assert row.marked_pnl == pytest.approx(500.0)      # the net, which hides both
    assert row.loss_avoided_usd == pytest.approx(-400.0)
    assert row.upside_blocked_usd == pytest.approx(900.0)
    assert row.marked_count == 2


def test_a_rule_with_no_marks_at_all_stays_null_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pending` with no mark is absent data. It must stay null so the UI
    keeps rendering the literal word "pending" rather than a $0 that reads
    as a measured result."""
    dec = _decision(proposal={"estimatedNotional": 5000.0})
    session = _QueueSession([[(dec, _ghost(status="pending", ghost_pnl=None))], []])
    _patch(monkeypatch, session)

    row = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID)).rules[0]

    assert row.marked_pnl is None
    assert row.marked_count is None
    assert row.loss_avoided_usd is None
    assert row.upside_blocked_usd is None
