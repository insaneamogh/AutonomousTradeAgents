"""Tests for ``tools.guard.ToolGuard`` — the deterministic gate in front of
``open_option_trade`` / ``adjust_option_position``.

No live DB, no live Alpaca — this repo has no live-DB test harness at all
(see ``apps/api/app/services/orders/position_manager.py``'s
``_option_exit_peak_update_stmt`` docstring for the same statement). Real,
already-tested collaborators stand in wherever possible
(``MockRiskContextProvider`` from ``engine.risk``, ``InMemoryDecisionLog``
from ``trading_agents.memory``); a small ``FakeBroker`` and
``_FakeSessionFactory`` stand in for Alpaca and Postgres.
``fetch_option_candidates``/``select_contract``/``evaluate`` are patched at
their import site INSIDE ``guard.py`` — mirrors
``apps/agents/tests/test_risk_officer_options.py``'s own
``patch("trading_agents.nodes.risk_officer.evaluate", ...)`` convention.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import patch

import pytest

from broker.types import Order, OrderStatus
from broker.types import Side as BrokerSide
from engine.options import ContractQuote, ContractSelectionResult
from engine.risk import MockRiskContextProvider, RiskCaps
from engine.risk.types import OptionLegDetails, PortfolioPosition, RiskDecision
from trading_agents.memory import InMemoryDecisionLog
from trading_agents.options.tools import registry as options_registry
from trading_agents.options.tools.guard import (
    SYMBOL_RE,
    GuardContext,
    GuardVerdict,
    ToolGuard,
    _option_exit_merge_stmt,
    _parses_timeframe,
    _tool_log_append_stmt,
    dispatch_tool_call,
)
from trading_agents.options.tools.trade import adjust_option_position, open_option_trade

# A Wednesday, solidly inside the regular session (9:30-16:00 ET), no
# holiday nearby (Labor Day 2026 is Sep 7). 15:00 UTC == 11:00 ET.
MARKET_OPEN_NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
# A Saturday.
MARKET_CLOSED_NOW = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)

OPEN_ARGS: dict[str, Any] = {
    "underlying": "NVDA",
    "direction": "long",
    "strategy": "momentum",
    "conviction": 0.6,
    "thesis": "NVDA breaks 190 within 3 weeks on volume expansion.",
    "take_profit_pct": 80.0,
    "stop_loss_pct": 40.0,
}


def _quote(
    occ: str = "NVDA260918C00225000",
    *,
    strike: float = 225.0,
    ask: float | None = 2.20,
    bid: float | None = 2.10,
    delta: float | None = 0.45,
    iv: float | None = 0.30,
    oi: int | None = 500,
    volume: int | None = 20,
    expiry: date | None = None,
    contract_type: str = "call",
) -> ContractQuote:
    return ContractQuote(
        occ_symbol=occ,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=strike,
        expiry=expiry or date(2026, 9, 18),
        bid=bid,
        ask=ask,
        open_interest=oi,
        volume=volume,
        delta=delta,
        implied_volatility=iv,
    )


async def _fetch_ok(*args: Any, **kwargs: Any) -> tuple[ContractQuote, ...]:
    return (_quote(),)


@dataclass(frozen=True)
class _FakeToolCall:
    """Duck-types ``trading_agents.llm.ToolCall`` (``.id``/``.name``/
    ``.input``) without importing it — ``guard.dispatch_tool_call`` only
    ever reads these three attributes."""

    id: str
    name: str
    input: dict[str, Any]


def _call(name: str, args: dict[str, Any]) -> _FakeToolCall:
    return _FakeToolCall(id=str(uuid.uuid4()), name=name, input=args)


class FakeBroker:
    def __init__(
        self, *, filled_qty: int = 1, avg_fill_price: float = 2.20, position: Any = None
    ) -> None:
        self.orders: list[Any] = []
        self.canceled: list[str] = []
        self._n = 0
        self._filled_qty = filled_qty
        self._avg_fill_price = avg_fill_price
        self._position = position

    async def place_order(self, request: Any) -> Order:
        self._n += 1
        self.orders.append(request)
        return Order(
            broker_order_id=f"order-{self._n}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            filled_qty=self._filled_qty,
            avg_fill_price=self._avg_fill_price,
            status=OrderStatus.FILLED if self._filled_qty else OrderStatus.ACCEPTED,
            submitted_at=datetime.now(UTC),
        )

    async def get_position(self, symbol: str) -> Any:
        return self._position

    async def cancel_open_orders(self, symbol: str) -> int:
        self.canceled.append(symbol)
        return 0


class _FakeAsyncSession:
    def __init__(self, get_result: Any, execute_log: list[Any]) -> None:
        self._get_result = get_result
        self.execute_log = execute_log
        self.committed = False

    async def get(self, model: Any, pk: Any) -> Any:
        return self._get_result

    async def execute(self, stmt: Any, params: Any = None) -> None:
        self.execute_log.append((stmt, params))

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSessionFactory:
    """Stands in for ``async_sessionmaker`` — ``ToolGuard`` calls it like
    ``session_factory()`` to get a fresh async-context-managed session per
    call, exactly like every real caller in this codebase does."""

    def __init__(self, get_result: Any = None) -> None:
        self.get_result = get_result
        self.sessions: list[_FakeAsyncSession] = []

    def __call__(self) -> _FakeAsyncSession:
        session = _FakeAsyncSession(self.get_result, [])
        self.sessions.append(session)
        return session


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
    # RiskCaps() bare defaults to options_disabled=True (fail-closed; only
    # RiskCaps.from_env() flips it via ALLOW_OPTIONS=1) — every test in
    # this file exercises the options path, so the fixture opts in
    # explicitly rather than relying on an env var these tests don't set.
    kwargs.setdefault("caps", RiskCaps(options_disabled=False))
    return GuardContext(**kwargs)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("USE_POSTGRES", raising=False)


# ─────────────────────────────────────────────────────────────────────
# The tool schemas are frozen — never accept a contract/strike/qty.
# ─────────────────────────────────────────────────────────────────────


def test_agent_cannot_supply_a_contract_or_qty() -> None:
    from trading_agents.options.tools.schemas import OPEN_OPTION_TRADE

    props = OPEN_OPTION_TRADE["input_schema"]["properties"]
    for forbidden in ("occ_symbol", "occSymbol", "qty", "strike", "expiry", "expiry_date"):
        assert forbidden not in props, f"{forbidden!r} must never be agent-suppliable"


async def test_guard_ignores_a_smuggled_qty_and_contract() -> None:
    """Even a completion that smuggles extra keys the schema never defined
    must not influence what gets traded — the guard derives the contract
    and qty itself, unconditionally."""
    args = {**OPEN_ARGS, "occ_symbol": "EVIL260101C00001000", "qty": 999_999}
    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before("open_option_trade", args, ctx)
    assert verdict.allow, verdict.reason
    assert verdict.payload is not None
    assert verdict.payload["option"].occ_symbol == "NVDA260918C00225000"
    assert verdict.payload["qty"] != 999_999


# ─────────────────────────────────────────────────────────────────────
# open_option_trade: the 12-step stack, in order
# ─────────────────────────────────────────────────────────────────────


async def test_open_trade_happy_path_reaches_broker_and_persists_decision() -> None:
    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.allow, verdict.reason
    assert verdict.payload is not None

    result = await open_option_trade(OPEN_ARGS, ctx, verdict.payload)
    assert result["occ_symbol"] == "NVDA260918C00225000"
    assert result["decision_id"] == ctx.council_run_id
    assert result["qty"] >= 1


async def test_open_trade_places_a_limit_buy_to_open_never_a_market_order() -> None:
    broker = FakeBroker(filled_qty=4, avg_fill_price=2.20)
    guard = _guard(broker_factory=lambda: broker)
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.allow, verdict.reason
    await open_option_trade(OPEN_ARGS, ctx, verdict.payload)

    assert len(broker.orders) == 1
    order = broker.orders[0]
    assert order.side == BrokerSide.BUY_TO_OPEN
    assert order.order_type.value == "LIMIT"
    assert order.symbol == "NVDA260918C00225000"


async def test_open_trade_runs_the_full_risk_engine() -> None:
    """docs/IMPL_OPTIONS_AGENTS.md §7: skip evaluate() in before() to make
    this fail. Verified by ACTUALLY reverting evaluate() to a stub and
    confirming this test fails (see this session's report)."""
    calls: list[Any] = []

    def _spy_evaluate(proposal: Any, context: Any, caps: Any, **kwargs: Any) -> RiskDecision:
        calls.append(proposal)
        return RiskDecision(approved=True, reason="stub-approved", checks_passed=("stub",))

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok), patch(
        "trading_agents.options.tools.guard.evaluate", _spy_evaluate
    ):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)

    assert verdict.allow, verdict.reason
    assert len(calls) == 1
    proposal = calls[0]
    assert proposal.is_option is True
    # PER-CONTRACT PREMIUM, never the underlying share price — OPTIONS_PLAYBOOK.md §5.2.
    assert proposal.last_price == 2.20


async def test_risk_veto_returns_is_error_not_an_exception() -> None:
    """docs/IMPL_OPTIONS_AGENTS.md §7: raise instead of returning is_error
    to make this fail — verified by ACTUALLY reverting dispatch_tool_call's
    try/except to a bare call and confirming this test fails."""

    def _veto_evaluate(proposal: Any, context: Any, caps: Any, **kwargs: Any) -> RiskDecision:
        return RiskDecision(approved=False, reason="too rich", veto_rule="max_premium_pct")

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok), patch(
        "trading_agents.options.tools.guard.evaluate", _veto_evaluate
    ):
        out = await dispatch_tool_call(
            _call("open_option_trade", OPEN_ARGS),
            ctx,
            guard=guard,
            registry={"open_option_trade": open_option_trade},
        )
    assert out == {"is_error": True, "content": {"denied": "max_premium_pct"}}


async def test_dispatch_never_raises_when_the_handler_raises() -> None:
    async def _boom(args: Any, ctx: Any, payload: Any) -> dict[str, Any]:
        raise RuntimeError("broker exploded")

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        out = await dispatch_tool_call(
            _call("open_option_trade", OPEN_ARGS),
            ctx,
            guard=guard,
            registry={"open_option_trade": _boom},
        )
    assert out == {"is_error": True, "content": {"denied": "tool_failed"}}


async def test_unknown_tool_denies_and_does_not_raise() -> None:
    guard = _guard()
    ctx = _ctx()
    out = await dispatch_tool_call(
        _call("delete_everything", {}), ctx, guard=guard, registry=options_registry.REGISTRY
    )
    assert out == {"is_error": True, "content": {"denied": "unknown_tool"}}


async def test_disabled_without_auto_trade_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_TRADE_ENABLED", raising=False)
    guard = _guard()
    ctx = _ctx()
    verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict == GuardVerdict(False, "auto_trade_disabled")


async def test_never_trades_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    guard = _guard()
    ctx = _ctx()
    verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.allow is False
    assert verdict.reason == "live_mode_refused"


async def test_never_trades_when_live_trading_enabled_even_in_paper_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    guard = _guard()
    ctx = _ctx()
    verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "live_mode_refused"


async def test_market_closed_denied() -> None:
    guard = _guard(clock=lambda: MARKET_CLOSED_NOW)
    ctx = _ctx()
    verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "market_closed"


async def test_one_open_per_pass_denies_a_second_call() -> None:
    guard = _guard()
    ctx = _ctx(calls_this_pass=1)
    verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "one_open_per_pass"


async def test_malformed_symbol_denied() -> None:
    guard = _guard()
    ctx = _ctx()
    args = {**OPEN_ARGS, "underlying": "NVDA260918C00225000"}
    verdict = await guard.before("open_option_trade", args, ctx)
    assert verdict.reason == "malformed_symbol"


def test_symbol_re_accepts_plain_tickers_and_share_classes() -> None:
    for good in ("NVDA", "SPY", "A", "BRK.B"):
        assert SYMBOL_RE.match(good), good
    for bad in ("nvda", "NVDA260918C00225000", "", "TOOLONGTICKER"):
        assert not SYMBOL_RE.match(bad), bad


async def test_unknown_strategy_denied() -> None:
    guard = _guard()
    ctx = _ctx()
    args = {**OPEN_ARGS, "strategy": "not_a_real_strategy"}
    verdict = await guard.before("open_option_trade", args, ctx)
    assert verdict.reason == "unknown_strategy"


async def test_direction_contradicts_resolution_denied() -> None:
    guard = _guard()
    ctx = _ctx(resolved_direction="short")
    verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)  # OPEN_ARGS says "long"
    assert verdict.reason == "direction_contradicts_resolution"


async def test_missing_resolution_denies_direction() -> None:
    guard = _guard()
    ctx = _ctx(resolved_direction=None)
    verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "direction_contradicts_resolution"


async def test_thesis_without_timeframe_denied() -> None:
    guard = _guard()
    ctx = _ctx()
    args = {**OPEN_ARGS, "thesis": "NVDA looks strong."}
    verdict = await guard.before("open_option_trade", args, ctx)
    assert verdict.reason == "thesis_without_timeframe"


@pytest.mark.parametrize(
    "thesis",
    [
        "NVDA breaks 190 within 3 weeks on volume expansion.",
        "Expect a move higher over the next 10 days.",
        "This should resolve by Friday.",
        "Target achieved within 2 months.",
        "Catalyst expected by 2026-09-18.",
    ],
)
def test_thesis_timeframe_variants_parse(thesis: str) -> None:
    assert _parses_timeframe(thesis) is True


def test_thesis_without_any_timeframe_does_not_parse() -> None:
    assert _parses_timeframe("NVDA looks strong.") is False
    assert _parses_timeframe("") is False


async def test_guard_clamps_conviction_to_the_resolved_value() -> None:
    guard = _guard()
    # min_council_confidence floored at 0 for this test: it isolates the
    # CLAMP behavior specifically, independent of the (correct, and
    # separately covered) confidence-floor veto that 0.3 would otherwise
    # also legitimately trip at RiskCaps' 0.50 default.
    ctx = _ctx(resolved_conviction=0.3, caps=RiskCaps(options_disabled=False, min_council_confidence=0.0))
    args = {**OPEN_ARGS, "conviction": 0.95}
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before("open_option_trade", args, ctx)
    assert verdict.allow, verdict.reason
    assert verdict.payload["conviction"] == 0.3


async def test_no_candidates_denied_with_named_reason() -> None:
    async def _empty(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
        return ()

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _empty):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "no_candidates"


async def test_no_delta_in_band_denied() -> None:
    async def _bad_delta(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
        return (_quote(delta=0.05),)

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _bad_delta):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "no_delta_in_band"


async def test_size_rounds_to_zero_denied() -> None:
    async def _expensive(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
        return (_quote(ask=50.0, bid=49.0),)  # $5,000/contract vs a $1,000 (1%) budget

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _expensive):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "size_rounds_to_zero"


async def test_naked_short_still_forbidden() -> None:
    """A hand-crafted sell_to_open leg must be denied before evaluate()
    even runs — belt-and-suspenders alongside engine.risk's own
    naked_short_forbidden rule.

    evaluate() is ALSO patched to unconditionally approve here, precisely
    so this test isolates guard.py's OWN ``_ALLOWED_ACTIONS`` check rather
    than accidentally passing because the deeper risk-engine rule caught
    it instead (verified: the first version of this test passed even with
    ``_ALLOWED_ACTIONS`` widened to include "sell_to_open", because
    evaluate()'s own naked_short_forbidden rule fired regardless — see
    this session's report). Revert-check: add "sell_to_open" to
    guard._ALLOWED_ACTIONS and confirm THIS version fails."""
    bad_option = OptionLegDetails(
        underlying_symbol="NVDA",
        occ_symbol="NVDA260918C00225000",
        contract_type="call",
        strike=225.0,
        expiry=date(2026, 9, 18),
        multiplier=100,
        action="sell_to_open",  # type: ignore[arg-type]  # must never happen
        open_interest=500,
        volume=20,
        bid=2.10,
        ask=2.20,
        implied_volatility=0.30,
    )

    def _fake_select_contract(inputs: Any) -> ContractSelectionResult:
        return ContractSelectionResult(selected=bad_option, rejection_reason=None, funnel_counts={})

    def _rubber_stamp_evaluate(proposal: Any, context: Any, caps: Any, **kwargs: Any) -> RiskDecision:
        return RiskDecision(approved=True, reason="rubber-stamped for this test", checks_passed=())

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok), patch(
        "trading_agents.options.tools.guard.select_contract", _fake_select_contract
    ), patch("trading_agents.options.tools.guard.evaluate", _rubber_stamp_evaluate):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.reason == "naked_short_forbidden"


# ─────────────────────────────────────────────────────────────────────
# adjust_option_position: the ratchet invariant
# ─────────────────────────────────────────────────────────────────────


def _seeded_row(
    *, uid: uuid.UUID, did: uuid.UUID, option_exit: dict[str, Any] | None = None
) -> Any:
    from engine.db.models import AgentDecision

    return AgentDecision(
        id=did,
        user_id=uid,
        symbol="NVDA",
        horizon="short",
        final_action="BUY",
        proposal={"occSymbol": "NVDA260918C00225000", "isOption": True},
        reasoning={"option_exit": option_exit if option_exit is not None else {}},
    )


async def test_agent_cannot_widen_a_stop() -> None:
    """THE single most important test in this repo (docs/IMPL_OPTIONS_AGENTS.md
    §7). A request to move the stop to an EQUAL or LARGER value than the
    position's current stop_loss_pct must be denied, and the stored
    protection must be untouched. See guard.py's module docstring for why
    "larger == denied" (not the plan docs' literal "must increase")."""
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(
        uid=uid, did=did, option_exit={"stop_loss_pct": 45.0, "adds_this_position": 0}
    )
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    for attempted in (45.0, 46.0, 50.0):
        verdict = await guard.before(
            "adjust_option_position",
            {"decision_id": str(did), "action": "TIGHTEN_STOP", "value": attempted, "reason": "x"},
            ctx,
        )
        assert verdict.allow is False, f"value={attempted} must be denied"
        assert verdict.reason == "cannot_loosen_protection"


async def test_tighten_stop_allows_a_strictly_smaller_value() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(
        uid=uid, did=did, option_exit={"stop_loss_pct": 45.0, "adds_this_position": 0}
    )
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "TIGHTEN_STOP", "value": 30.0, "reason": "vol dropped"},
        ctx,
    )
    assert verdict.allow, verdict.reason
    assert verdict.payload["option_state"]["stop_loss_pct"] == 30.0


async def test_tighten_stop_clamps_below_the_floor() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(
        uid=uid, did=did, option_exit={"stop_loss_pct": 45.0, "adds_this_position": 0}
    )
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "TIGHTEN_STOP", "value": 5.0, "reason": "x"},
        ctx,
    )
    assert verdict.allow, verdict.reason
    assert verdict.payload["option_state"]["stop_loss_pct"] == 25.0  # clamped to the band floor


async def test_raise_take_profit_denies_a_smaller_or_equal_value() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(
        uid=uid, did=did, option_exit={"take_profit_pct": 80.0, "adds_this_position": 0}
    )
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    for attempted in (80.0, 60.0):
        verdict = await guard.before(
            "adjust_option_position",
            {"decision_id": str(did), "action": "RAISE_TAKE_PROFIT", "value": attempted, "reason": "x"},
            ctx,
        )
        assert verdict.allow is False
        assert verdict.reason == "cannot_loosen_protection"


async def test_raise_take_profit_allows_a_larger_value() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(
        uid=uid, did=did, option_exit={"take_profit_pct": 80.0, "adds_this_position": 0}
    )
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "RAISE_TAKE_PROFIT", "value": 120.0, "reason": "let it run"},
        ctx,
    )
    assert verdict.allow, verdict.reason
    assert verdict.payload["option_state"]["take_profit_pct"] == 120.0


async def test_exit_now_always_allowed_regardless_of_state() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(uid=uid, did=did, option_exit={"adds_this_position": 2})
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "EXIT_NOW", "reason": "bail"},
        ctx,
    )
    assert verdict.allow is True


async def test_hold_always_allowed_and_changes_nothing() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(uid=uid, did=did, option_exit={"stop_loss_pct": 45.0})
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "HOLD", "reason": "waiting"},
        ctx,
    )
    assert verdict.allow is True
    result = await adjust_option_position(
        {"decision_id": str(did), "action": "HOLD", "reason": "waiting"}, ctx, verdict.payload
    )
    assert result["changed"] is False


async def test_decision_not_found_denied() -> None:
    session_factory = _FakeSessionFactory(get_result=None)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx()
    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(uuid.uuid4()), "action": "HOLD", "reason": "x"},
        ctx,
    )
    assert verdict.reason == "decision_not_found"


async def test_cross_tenant_decision_is_not_found() -> None:
    """Ownership check mirrors position_manager.close_position_now:
    another user's row must read identically to "does not exist"."""
    owner, attacker = uuid.uuid4(), uuid.uuid4()
    did = uuid.uuid4()
    row = _seeded_row(uid=owner, did=did, option_exit={})
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(attacker))
    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "HOLD", "reason": "x"},
        ctx,
    )
    assert verdict.reason == "decision_not_found"


async def test_scale_in_capped_at_two_adds() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(uid=uid, did=did, option_exit={"adds_this_position": 2})
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "SCALE_IN", "reason": "add"},
        ctx,
    )
    assert verdict.reason == "scale_in_cap_reached"


async def test_scale_in_counts_against_total_premium() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(uid=uid, did=did, option_exit={"adds_this_position": 0})
    session_factory = _FakeSessionFactory(get_result=row)
    existing_position = PortfolioPosition(
        symbol="NVDA260918C00225000",
        qty=1,
        avg_entry_price=47.0,
        market_value=4_700.0,  # 4.7% of $100k equity, close to the 5% cap
        is_option=True,
        multiplier=100,
    )
    context_provider = MockRiskContextProvider(
        account_equity=100_000.0, open_positions=(existing_position,)
    )
    guard = _guard(session_factory=session_factory, context_provider=context_provider)
    ctx = _ctx(user_id=str(uid))

    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before(
            "adjust_option_position",
            {"decision_id": str(did), "action": "SCALE_IN", "reason": "add"},
            ctx,
        )
    assert verdict.allow is False
    assert verdict.reason == "max_total_premium_pct"


async def test_scale_in_approved_when_room_remains() -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(uid=uid, did=did, option_exit={"adds_this_position": 0})
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)  # default MockRiskContextProvider, no positions
    ctx = _ctx(user_id=str(uid))

    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before(
            "adjust_option_position",
            {"decision_id": str(did), "action": "SCALE_IN", "reason": "add"},
            ctx,
        )
    assert verdict.allow, verdict.reason
    assert verdict.payload["option_state"]["adds_this_position"] == 1


# ─────────────────────────────────────────────────────────────────────
# after(): audit trail + narrow-never-widen
# ─────────────────────────────────────────────────────────────────────


def test_tool_log_append_stmt_never_a_whole_column_overwrite() -> None:
    """docs/IMPL_OPTIONS_AGENTS.md §7's test_tool_log_preserves_other_reasoning_keys.
    Same verification method as position_manager.py's
    _option_exit_peak_update_stmt: assert the STATEMENT SHAPE (this repo
    has no live-DB test harness to round-trip jsonb_set against). Revert-
    check: rewrite this function as `SET reasoning = CAST(:new_row AS
    jsonb)` and confirm the '{tool_log}' path literal disappears — done
    for real in this session (see the report)."""
    stmt, _params = _tool_log_append_stmt(
        decision_id=uuid.uuid4(),
        row={"tool": "open_option_trade", "allow": True, "reason": None, "latency_ms": 12.3, "args": {}},
    )
    sql = str(stmt)
    assert "jsonb_set(" in sql
    assert "'{tool_log}'" in sql
    assert "COALESCE(reasoning, '{}'::jsonb)" in sql
    assert "COALESCE(reasoning->'tool_log', '[]'::jsonb)" in sql
    assert "option_exit" not in sql
    assert "contract_funnel" not in sql


def test_option_exit_merge_stmt_scoped_to_its_own_key() -> None:
    stmt, _params = _option_exit_merge_stmt(
        decision_id=uuid.uuid4(), state={"stop_loss_pct": 30.0}
    )
    sql = str(stmt)
    assert "jsonb_set(" in sql
    assert "'{option_exit}'" in sql
    assert "tool_log" not in sql


async def test_after_persists_tool_log_via_jsonb_set_append() -> None:
    session_factory = _FakeSessionFactory(get_result=None)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx()

    await guard.after(
        "open_option_trade",
        OPEN_ARGS,
        {"decision_id": ctx.council_run_id, "user_id": ctx.user_id, "occ_symbol": "X"},
        ctx,
    )

    # Two writes go through the (fake) session factory on a successful
    # open: the tool_log append, and the approval_mode='auto' stamp (see
    # test_after_stamps_approval_mode_auto_on_a_successful_open below for
    # that one specifically) — each gets its own session, matching every
    # other call site in this codebase's "async with session_factory() as
    # session" per-statement pattern.
    tool_log_stmts = [
        (stmt, params)
        for sess in session_factory.sessions
        for stmt, params in sess.execute_log
        if "'{tool_log}'" in str(stmt)
    ]
    assert len(tool_log_stmts) == 1
    _stmt, params = tool_log_stmts[0]
    new_rows = json.loads(params["new_row"])
    assert new_rows[0]["tool"] == "open_option_trade"
    assert new_rows[0]["allow"] is True
    assert all(sess.committed for sess in session_factory.sessions)


async def test_after_denies_and_empties_payload_on_cross_tenant_result() -> None:
    guard = _guard()
    ctx = _ctx(user_id="user-a")
    verdict = await guard.after(
        "open_option_trade", OPEN_ARGS, {"user_id": "user-b", "decision_id": "x"}, ctx
    )
    assert verdict.allow is False
    assert verdict.reason == "user_scope_violation"
    assert verdict.payload == {}


async def test_after_stamps_approval_mode_auto_on_a_successful_open() -> None:
    session_factory = _FakeSessionFactory(get_result=None)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx()
    did = str(uuid.uuid4())

    await guard.after(
        "open_option_trade", OPEN_ARGS, {"decision_id": did, "user_id": ctx.user_id}, ctx
    )

    # Two statements executed against the (single, reused) session: the
    # tool_log append, and the approval_mode/user_response stamp.
    all_sql = [str(s) for s, _p in session_factory.sessions[-1].execute_log] + [
        str(s) for sess in session_factory.sessions for s, _p in sess.execute_log
    ]
    assert any("approval_mode" in sql for sql in all_sql)


async def test_dispatch_tool_call_end_to_end_open_and_audit() -> None:
    session_factory = _FakeSessionFactory(get_result=None)
    broker = FakeBroker(filled_qty=4, avg_fill_price=2.20)
    decision_log = InMemoryDecisionLog()
    guard = _guard(
        session_factory=session_factory, decision_log=decision_log, broker_factory=lambda: broker
    )
    ctx = _ctx()

    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        out = await dispatch_tool_call(
            _call("open_option_trade", OPEN_ARGS),
            ctx,
            guard=guard,
            registry=options_registry.REGISTRY,
        )

    assert out["is_error"] is False
    assert out["content"]["occ_symbol"] == "NVDA260918C00225000"
    assert len(broker.orders) == 1
    assert broker.orders[0].side == BrokerSide.BUY_TO_OPEN
    assert len(session_factory.sessions) >= 1
