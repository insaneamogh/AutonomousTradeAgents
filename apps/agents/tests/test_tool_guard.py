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
from unittest.mock import AsyncMock, patch

import pytest

from broker.types import Order, OrderStatus, Position
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
    persist_placed_order,
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
    """A chain with real DEPTH, not a single contract.

    `select_contract` refuses a chain whose liquidity stage yields fewer
    than `_MIN_LIQUID_CHAIN_DEPTH` survivors (see its docstring: the
    2026-09-01 CME position, where 1 of 29 survived and the resulting mark
    gapped 26 points between prints, so the stop could not function). These
    guard tests exercise the GUARD, not chain depth, so the fixture models
    a normal chain — the target contract plus liquid siblings at adjacent
    strikes. `_tie_break` still returns the 225 strike (closest to the
    delta band's centre), which every assertion below already expects."""
    return (
        _quote(),
        *(
            _quote(occ=f"NVDA260918C0{225 + i}000", strike=225.0 + i)
            for i in range(1, 6)
        ),
    )


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
    def __init__(
        self,
        get_result: Any,
        execute_log: list[Any],
        execute_results: list[Any] | None = None,
    ) -> None:
        self._get_result = get_result
        self.execute_log = execute_log
        self.committed = False
        # Queue of return values for successive .execute() calls — e.g. a
        # scalar-lookup result feeding persist_placed_order's broker
        # connection query. `None` (the default) preserves this fake's
        # original behavior of returning None from every execute(), which
        # every caller that only inspects execute_log (never the return
        # value) already relies on.
        self._execute_results = (
            list(execute_results) if execute_results is not None else None
        )

    async def get(self, model: Any, pk: Any) -> Any:
        return self._get_result

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.execute_log.append((stmt, params))
        if self._execute_results:
            return self._execute_results.pop(0)
        return None

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

    def __init__(self, get_result: Any = None, execute_results: list[Any] | None = None) -> None:
        self.get_result = get_result
        self.sessions: list[_FakeAsyncSession] = []
        self._execute_results = execute_results

    def __call__(self) -> _FakeAsyncSession:
        session = _FakeAsyncSession(
            self.get_result, [], execute_results=self._execute_results
        )
        self.sessions.append(session)
        return session


class _ScalarResult:
    """Bare-minimum stand-in for a SQLAlchemy ``Result`` — supports only the
    one method ``persist_placed_order``'s connection lookup calls."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


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


async def test_open_trade_persists_an_orders_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before this fix, ``open_option_trade`` placed a REAL broker order and
    wrote ONLY an ``agent_decisions`` row via ``decision_log.record`` —
    ``orders`` (the ONE table ``order_sync.py`` polls to converge fill_qty/
    status back onto the decision) stayed empty forever. That silently
    disabled every fill_qty-gated exit mechanism for the position (the
    ratchet, the mandatory DTE<=2 expiry sweep) and left it permanently
    showing as "awaiting fill" — or, combined with a separate OCC-vs-
    underlying matching bug, as broker-side "unmanaged" — regardless of how
    real and filled it actually was at the broker.

    Patches ``persist_placed_order`` at its ``trade.py`` import site (the
    same convention this file already uses for ``fetch_option_candidates``)
    rather than exercising a live DB — that function has its own dedicated
    unit tests in ``test_options_agents.py``/this file's persistence
    section; this test only pins that ``open_option_trade`` actually CALLS
    it, with the right identifiers.
    """
    fake_persist = AsyncMock()
    monkeypatch.setattr(
        "trading_agents.options.tools.trade.persist_placed_order", fake_persist
    )

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before("open_option_trade", OPEN_ARGS, ctx)
    assert verdict.allow, verdict.reason

    result = await open_option_trade(OPEN_ARGS, ctx, verdict.payload)

    fake_persist.assert_awaited_once()
    _, kwargs = fake_persist.await_args
    assert kwargs["decision_id"] == result["decision_id"]
    assert kwargs["user_id"] == ctx.user_id
    assert kwargs["client_order_id"] == f"agent-open-{ctx.council_run_id}"
    assert kwargs["underlying"] == "NVDA"
    assert kwargs["option_action"] == "buy_to_open"
    assert kwargs["multiplier"] == verdict.payload["option"].multiplier
    assert kwargs["order"].broker_order_id == result["order_id"]


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
    # `content` now also carries `contract_funnel` (a concrete contract WAS
    # selected before the risk veto fired — see the dedicated funnel tests
    # below) so it is a subset check, not the old exact-dict equality.
    assert out["is_error"] is True
    assert out["content"]["denied"] == "max_premium_pct"


async def test_a_denied_open_carries_the_contract_funnel_to_the_transcript() -> None:
    """The gap this whole change closes: `select_contract` runs on every
    attempted open (guard.py step 10), but until 2026-09-01 only the bare
    `rejection_reason` string ever escaped — the six-stage counts were
    computed and discarded. Covers BOTH shapes of denial: no contract
    survived at all, and a contract survived but was later risk-vetoed
    (`_ledger_refusal`).

    Revert-checked: reverting guard.py's `if selection.selected is None:`
    branch to `return GuardVerdict(False, selection.rejection_reason or
    "no_liquid_contract")` (no payload) makes this fail on the KeyError
    from indexing `out["content"]["contract_funnel"]`. Confirmed, then
    restored.
    """

    async def _bad_delta(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
        return (_quote(delta=0.05),)

    guard = _guard()
    ctx = _ctx()
    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _bad_delta):
        out = await dispatch_tool_call(
            _call("open_option_trade", OPEN_ARGS),
            ctx,
            guard=guard,
            registry={"open_option_trade": open_option_trade},
        )
    assert out["is_error"] is True
    assert out["content"]["denied"] == "no_delta_in_band"
    funnel = out["content"]["contract_funnel"]
    assert funnel["rejection_reason"] == "no_delta_in_band"
    assert funnel["selected_occ"] is None
    # Real per-stage counts, not an empty dict — narrowing to zero IS the story.
    assert funnel["counts"]["total"] == 1
    assert funnel["counts"]["delta_band"] == 0
    assert all(isinstance(v, int) for v in funnel["counts"].values())


async def test_a_risk_vetoed_open_ledgers_the_contract_funnel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OTHER shape: a contract survived all six stages and was then
    risk-vetoed (`_ledger_refusal`). Before this fix, `ToolGuard._ledger_
    refusal`'s own `agent_decisions` row (the Refusal Ledger's whole
    source of truth) carried `council_run_id`/`refused_by`/
    `risk_checks_passed` and NOTHING about which contracts were even
    looked at — measured live: 8/8 real VETOED options rows in the 7 days
    before this fix had no funnel data at all.

    Revert-checked: reverting `_ledger_refusal`'s `reasoning={...}` to drop
    the `contract_funnel` key makes the final assertion fail (KeyError).
    Confirmed, then restored.
    """

    def _veto_evaluate(proposal: Any, context: Any, caps: Any, **kwargs: Any) -> RiskDecision:
        return RiskDecision(approved=False, reason="too rich", veto_rule="max_premium_pct")

    decision_log = InMemoryDecisionLog()
    guard = _guard(decision_log=decision_log)
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
    assert out["content"]["contract_funnel"]["selected_occ"] == "NVDA260918C00225000"

    assert len(decision_log._rows) == 1
    row = decision_log._rows[0]
    assert row.final_action == "VETOED"
    assert row.risk_veto_rule == "max_premium_pct"
    funnel = row.reasoning["contract_funnel"]
    assert funnel["selected_occ"] == "NVDA260918C00225000"
    assert funnel["rejection_reason"] is None
    # 6 = the depth `_fetch_ok` now models (see its docstring). This test's
    # point is that the funnel is PERSISTED on a risk-vetoed row at all,
    # not the specific count.
    assert funnel["counts"]["liquidity"] == 6


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


# ─────────────────────────────────────────────────────────────────────
# adjust_option_position gets the SAME master-switch/paper/market-hours
# gate as open_option_trade -- added after review found the original
# _before_adjust_option_position had none at all, so EXIT_NOW/SCALE_IN
# (both of which reach packages/broker) could place a real order
# regardless of AUTO_TRADE_ENABLED, live/paper mode, or market hours.
# Mirrors the four tests directly above; no session_factory/seeded row
# needed since the gate now runs before any DB lookup.
# ─────────────────────────────────────────────────────────────────────


async def test_adjust_disabled_without_auto_trade_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_TRADE_ENABLED", raising=False)
    guard = _guard()
    ctx = _ctx()
    verdict = await guard.before("adjust_option_position", {"action": "EXIT_NOW"}, ctx)
    assert verdict == GuardVerdict(False, "auto_trade_disabled")


async def test_adjust_never_acts_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    guard = _guard()
    ctx = _ctx()
    verdict = await guard.before("adjust_option_position", {"action": "EXIT_NOW"}, ctx)
    assert verdict.reason == "live_mode_refused"


async def test_adjust_never_acts_when_live_trading_enabled_even_in_paper_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    guard = _guard()
    ctx = _ctx()
    verdict = await guard.before("adjust_option_position", {"action": "SCALE_IN"}, ctx)
    assert verdict.reason == "live_mode_refused"


async def test_adjust_market_closed_denied() -> None:
    guard = _guard(clock=lambda: MARKET_CLOSED_NOW)
    ctx = _ctx()
    verdict = await guard.before("adjust_option_position", {"action": "HOLD"}, ctx)
    assert verdict.reason == "market_closed"


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
        # $5,000/contract vs a $1,000 (1%) budget. Chain depth matters here
        # too — a one-contract chain is refused `illiquid_chain` before
        # sizing is ever reached, which would pass this test for the wrong
        # reason.
        return (
            _quote(ask=50.0, bid=49.0),
            *(
                _quote(occ=f"NVDA260918C0{225 + i}000", strike=225.0 + i,
                       ask=50.0, bid=49.0)
                for i in range(1, 6)
            ),
        )

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


async def test_exit_now_persists_an_orders_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same bug as open_option_trade, in the close direction: _exit_now
    placed a real SELL_TO_CLOSE at the broker but never wrote an orders
    row, so order_sync.py had nothing to converge the close's fill/status
    against."""
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(uid=uid, did=did, option_exit={"adds_this_position": 0})
    session_factory = _FakeSessionFactory(get_result=row)
    position = Position(
        symbol="NVDA260918C00225000",
        qty=2,
        avg_entry_price=2.20,
        market_value=460.0,
        unrealized_pl=20.0,
        unrealized_pl_pct=4.5,
        multiplier=100,
        is_option=True,
    )
    broker = FakeBroker(position=position)
    guard = _guard(session_factory=session_factory, broker_factory=lambda: broker)
    ctx = _ctx(user_id=str(uid))

    fake_persist = AsyncMock()
    monkeypatch.setattr(
        "trading_agents.options.tools.trade.persist_placed_order", fake_persist
    )

    verdict = await guard.before(
        "adjust_option_position",
        {"decision_id": str(did), "action": "EXIT_NOW", "reason": "bail"},
        ctx,
    )
    assert verdict.allow is True
    result = await adjust_option_position(
        {"decision_id": str(did), "action": "EXIT_NOW", "reason": "bail"}, ctx, verdict.payload
    )
    assert result["changed"] is True

    fake_persist.assert_awaited_once()
    _, kwargs = fake_persist.await_args
    assert kwargs["decision_id"] == str(did)
    assert kwargs["underlying"] == "NVDA"
    assert kwargs["option_action"] == "sell_to_close"
    assert kwargs["multiplier"] == 100


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


async def test_scale_in_persists_an_orders_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same bug again: _scale_in placed a real BUY_TO_OPEN add-on at the
    broker but never wrote an orders row for it."""
    uid, did = uuid.uuid4(), uuid.uuid4()
    row = _seeded_row(uid=uid, did=did, option_exit={"adds_this_position": 0})
    session_factory = _FakeSessionFactory(get_result=row)
    guard = _guard(session_factory=session_factory)
    ctx = _ctx(user_id=str(uid))

    fake_persist = AsyncMock()
    monkeypatch.setattr(
        "trading_agents.options.tools.trade.persist_placed_order", fake_persist
    )

    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        verdict = await guard.before(
            "adjust_option_position",
            {"decision_id": str(did), "action": "SCALE_IN", "reason": "add"},
            ctx,
        )
    assert verdict.allow, verdict.reason
    result = await adjust_option_position(
        {"decision_id": str(did), "action": "SCALE_IN", "reason": "add"}, ctx, verdict.payload
    )
    assert result["changed"] is True

    fake_persist.assert_awaited_once()
    _, kwargs = fake_persist.await_args
    assert kwargs["decision_id"] == str(did)
    assert kwargs["underlying"] == "NVDA"
    assert kwargs["option_action"] == "buy_to_open"
    assert kwargs["multiplier"] == 100


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


async def test_a_successful_open_persists_the_contract_funnel_too() -> None:
    """`nodes/drafter.py`'s legacy options path has always persisted "we
    looked at N contracts and bought this one" on the SUCCESS path, not
    just a HOLD's — `open_option_trade`'s own row (the one this tool
    actually writes; `runtime` skips its write via `decision_row_written`)
    needs the same thing, and until 2026-09-01 did not have it at all.

    Revert-checked: reverting trade.py's `entry.reasoning` to drop
    `"contract_funnel": guard_payload.get("contract_funnel")` makes the
    final assertion fail (KeyError). Confirmed, then restored.
    """
    decision_log = InMemoryDecisionLog()
    broker = FakeBroker(filled_qty=4, avg_fill_price=2.20)
    guard = _guard(decision_log=decision_log, broker_factory=lambda: broker)
    ctx = _ctx()

    with patch("trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok):
        out = await dispatch_tool_call(
            _call("open_option_trade", OPEN_ARGS),
            ctx,
            guard=guard,
            registry={"open_option_trade": open_option_trade},
        )
    assert out["is_error"] is False

    assert len(decision_log._rows) == 1
    row = decision_log._rows[0]
    assert row.final_action == "BUY"
    funnel = row.reasoning["contract_funnel"]
    assert funnel["selected_occ"] == "NVDA260918C00225000"
    assert funnel["rejection_reason"] is None
    assert funnel["counts"]
    assert all(isinstance(v, int) for v in funnel["counts"].values())


# ─────────────────────────────────────────────────────────────────────
# persist_placed_order — the orders-row write itself (see trade.py's three
# call sites for the wiring tests; these pin the INSERT's own correctness)
# ─────────────────────────────────────────────────────────────────────


def _placed_order(
    *, filled_qty: int = 0, avg_fill_price: float | None = None, qty: int = 4
) -> Order:
    return Order(
        broker_order_id="broker-order-1",
        client_order_id="agent-open-abc",
        symbol="NVDA260918C00225000",
        side=BrokerSide.BUY_TO_OPEN,
        qty=qty,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill_price,
        status=OrderStatus.ACCEPTED if filled_qty == 0 else OrderStatus.FILLED,
        submitted_at=datetime(2026, 9, 2, 15, 0, tzinfo=UTC),
    )


async def test_persist_placed_order_writes_the_row() -> None:
    """The core of the fix: given a real (fake) session that finds an
    active paper broker_connections row, the INSERT actually carries the
    right values — underlying symbol (never the OCC string), plain "BUY"
    side, the real option_action/multiplier, and the broker's own
    broker_order_id/status/filled_qty so order_sync.py has something
    accurate to converge against on the next tick."""
    conn_id = uuid.uuid4()
    session_factory = _FakeSessionFactory(execute_results=[_ScalarResult(conn_id)])
    user_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    order = _placed_order(filled_qty=0)

    await persist_placed_order(
        session_factory,
        user_id=user_id,
        decision_id=decision_id,
        client_order_id="agent-open-abc",
        underlying="NVDA",
        order=order,
        option_action="buy_to_open",
        multiplier=100,
    )

    session = session_factory.sessions[0]
    assert session.committed is True
    assert len(session.execute_log) == 2  # the connection SELECT, then the INSERT
    insert_stmt, _ = session.execute_log[1]

    from sqlalchemy.dialects import postgresql as pg_dialect

    compiled = insert_stmt.compile(dialect=pg_dialect.dialect())
    params = compiled.params
    assert params["symbol"] == "NVDA"  # the underlying, never the OCC string
    assert params["side"] == "BUY"
    assert params["client_order_id"] == "agent-open-abc"
    assert params["broker_connection_id"] == conn_id
    assert params["agent_decision_id"] == uuid.UUID(decision_id)
    assert params["broker_order_id"] == "broker-order-1"
    assert params["is_option"] is True
    assert params["is_paper"] is True
    assert params["option_action"] == "buy_to_open"
    assert params["multiplier"] == 100
    assert params["status"] == "accepted"
    assert params["filled_qty"] == 0


async def test_persist_placed_order_noop_without_session_factory() -> None:
    """Offline/dry-run mode (no Postgres) — must not raise."""
    await persist_placed_order(
        None,
        user_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        client_order_id="agent-open-abc",
        underlying="NVDA",
        order=_placed_order(),
        option_action="buy_to_open",
    )


async def test_persist_placed_order_noop_without_broker_connection() -> None:
    """No active paper alpaca broker_connections row for this user — must
    log and return, never raise (and never insert)."""
    session_factory = _FakeSessionFactory(execute_results=[_ScalarResult(None)])

    await persist_placed_order(
        session_factory,
        user_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        client_order_id="agent-open-abc",
        underlying="NVDA",
        order=_placed_order(),
        option_action="buy_to_open",
    )

    session = session_factory.sessions[0]
    assert session.committed is False
    assert len(session.execute_log) == 1  # only the connection SELECT ran


async def test_persist_placed_order_never_raises_on_a_db_failure() -> None:
    """By the time this runs, the broker order is ALREADY real. A DB hiccup
    writing the audit row must be swallowed, not propagated — the same
    contract executor.py's own persist_order_result call site documents.
    Simulated here with a session whose execute() raises outright."""

    class _ExplodingSession:
        async def __aenter__(self) -> "_ExplodingSession":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection pool exhausted")

    def _session_factory() -> _ExplodingSession:
        return _ExplodingSession()

    # Must not raise.
    await persist_placed_order(
        _session_factory,
        user_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        client_order_id="agent-open-abc",
        underlying="NVDA",
        order=_placed_order(),
        option_action="buy_to_open",
    )


async def test_persist_placed_order_sell_to_close_side() -> None:
    conn_id = uuid.uuid4()
    session_factory = _FakeSessionFactory(execute_results=[_ScalarResult(conn_id)])

    await persist_placed_order(
        session_factory,
        user_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        client_order_id="agent-exit-abc",
        underlying="NVDA",
        order=_placed_order(filled_qty=4, avg_fill_price=3.00),
        option_action="sell_to_close",
        multiplier=100,
    )

    session = session_factory.sessions[0]
    _, _ = session.execute_log[0]
    insert_stmt, _ = session.execute_log[1]

    from sqlalchemy.dialects import postgresql as pg_dialect

    params = insert_stmt.compile(dialect=pg_dialect.dialect()).params
    assert params["side"] == "SELL"
    assert params["option_action"] == "sell_to_close"
    assert params["filled_qty"] == 4


# ─────────────────────────────────────────────────────────────────────
# preflight_can_open — the zero-LLM account-level gate
# ─────────────────────────────────────────────────────────────────────


def _preflight_caps(**kw: Any) -> RiskCaps:
    kw.setdefault("options_disabled", False)
    return RiskCaps.aggressive_paper(**kw)


async def test_preflight_blocks_when_the_book_alone_already_meets_the_premium_cap() -> None:
    """The measured waste this exists for: 2026-09-01, the options book hit
    `max_total_premium_pct` at 15:00 UTC and stayed there, and every options
    pass for the next three hours still paid ~3 Sonnet calls to be told a
    portfolio-level fact that had nothing to do with the symbol. 48 runs."""
    caps = _preflight_caps()
    at_cap = PortfolioPosition(
        symbol="NVDA260918C00225000",
        qty=1,
        avg_entry_price=75.0,
        # caps.options_max_total_premium_pct is 7.5 -> 7.5% of $100k.
        market_value=caps.options_max_total_premium_pct / 100.0 * 100_000.0,
        is_option=True,
        multiplier=100,
    )
    guard = _guard(
        context_provider=MockRiskContextProvider(
            account_equity=100_000.0, open_positions=(at_cap,)
        )
    )

    verdict = await guard.preflight_can_open(user_id=str(uuid.uuid4()), caps=caps)

    assert verdict.allow is False
    assert verdict.reason == "max_total_premium_pct"


async def test_preflight_never_blocks_a_book_that_is_still_under_the_cap() -> None:
    """The load-bearing guarantee. A false negative here is a LOST TRADE —
    strictly worse than the wasted spend this is saving. A book under the
    cap must always go on to the real per-contract check."""
    caps = _preflight_caps()
    under = PortfolioPosition(
        symbol="NVDA260918C00225000",
        qty=1,
        avg_entry_price=1.0,
        # A hair under the cap — still room for a new entry.
        market_value=(caps.options_max_total_premium_pct - 0.01) / 100.0 * 100_000.0,
        is_option=True,
        multiplier=100,
    )
    guard = _guard(
        context_provider=MockRiskContextProvider(
            account_equity=100_000.0, open_positions=(under,)
        )
    )

    verdict = await guard.preflight_can_open(user_id=str(uuid.uuid4()), caps=caps)

    assert verdict.allow is True


async def test_preflight_ignores_equity_positions_when_summing_option_premium() -> None:
    """`max_total_premium_pct` sums OPTION market value only. Counting a
    large equity holding would block the options leg on an account that has
    no option exposure at all."""
    caps = _preflight_caps()
    equity_only = PortfolioPosition(
        symbol="CVX", qty=1_000, avg_entry_price=200.0,
        market_value=200_000.0, is_option=False, multiplier=1,
    )
    guard = _guard(
        context_provider=MockRiskContextProvider(
            account_equity=100_000.0, open_positions=(equity_only,)
        )
    )

    verdict = await guard.preflight_can_open(user_id=str(uuid.uuid4()), caps=caps)

    assert verdict.allow is True


async def test_preflight_blocks_below_the_broker_options_level() -> None:
    caps = _preflight_caps()
    guard = _guard(
        context_provider=MockRiskContextProvider(
            account_equity=100_000.0, options_trading_level=1
        )
    )

    verdict = await guard.preflight_can_open(user_id=str(uuid.uuid4()), caps=caps)

    assert verdict.allow is False
    assert verdict.reason == "options_level_insufficient"


async def test_preflight_blocks_when_the_market_is_closed() -> None:
    caps = _preflight_caps()
    guard = _guard(clock=lambda: datetime(2026, 9, 2, 2, 0, tzinfo=UTC))

    verdict = await guard.preflight_can_open(user_id=str(uuid.uuid4()), caps=caps)

    assert verdict.allow is False
    assert verdict.reason == "market_closed"


async def test_preflight_fails_open_when_the_context_provider_raises() -> None:
    """A pre-flight is an OPTIMISATION. A broken one must degrade to
    running the normal paid path, never to a silent HOLD that quietly stops
    the desk trading."""
    caps = _preflight_caps()

    class _Boom:
        async def fetch(self, *, user_id: str | None = None) -> Any:
            raise RuntimeError("postgres down")

    guard = _guard(context_provider=_Boom())

    verdict = await guard.preflight_can_open(user_id=str(uuid.uuid4()), caps=caps)

    assert verdict.allow is True


# ─────────────────────────────────────────────────────────────────────
# preflight_chain_is_tradeable — the funnel, before the debate is paid for
# ─────────────────────────────────────────────────────────────────────


async def _thin_chain(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
    """One liquid contract — the CME shape. Trips `illiquid_chain` in every
    conviction regime."""
    return (_quote(),)


async def _empty_chain(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
    return ()


async def _no_delta_chain(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
    """Deep and liquid, but every contract sits outside BOTH delta bands
    (union [0.25, 0.75]) — a conviction/market-shaped refusal, not a
    chain-shaped one."""
    return tuple(
        _quote(occ=f"NVDA260918C0{225 + i}000", strike=225.0 + i, delta=0.95)
        for i in range(6)
    )


async def test_chain_preflight_refuses_a_one_contract_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CME case: refused for zero model calls instead of ~3."""
    guard = _guard()
    monkeypatch.setattr(
        "trading_agents.options.tools.guard.fetch_option_candidates", _thin_chain
    )
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    verdict = await guard.preflight_chain_is_tradeable(
        underlying="CME", direction="long", caps=RiskCaps.aggressive_paper()
    )

    assert verdict.allow is False
    assert verdict.reason == "illiquid_chain"


async def test_chain_preflight_allows_a_deep_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _guard()
    monkeypatch.setattr(
        "trading_agents.options.tools.guard.fetch_option_candidates", _fetch_ok
    )
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    verdict = await guard.preflight_chain_is_tradeable(
        underlying="NVDA", direction="long", caps=RiskCaps.aggressive_paper()
    )

    assert verdict.allow is True


async def test_chain_preflight_does_not_short_circuit_a_conviction_shaped_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`no_delta_in_band` depends on conviction and on live greeks. Treating
    it as fatal here would refuse trades the paid path might have taken —
    a false negative, the one thing this must never produce."""
    guard = _guard()
    monkeypatch.setattr(
        "trading_agents.options.tools.guard.fetch_option_candidates", _no_delta_chain
    )
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    verdict = await guard.preflight_chain_is_tradeable(
        underlying="NVDA", direction="long", caps=RiskCaps.aggressive_paper()
    )

    assert verdict.allow is True


async def test_chain_preflight_fails_open_when_the_chain_fetch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*a: Any, **k: Any) -> tuple[ContractQuote, ...]:
        raise RuntimeError("alpaca down")

    guard = _guard()
    monkeypatch.setattr(
        "trading_agents.options.tools.guard.fetch_option_candidates", _boom
    )
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    verdict = await guard.preflight_chain_is_tradeable(
        underlying="NVDA", direction="long", caps=RiskCaps.aggressive_paper()
    )

    assert verdict.allow is True


async def test_chain_preflight_refuses_an_empty_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _guard()
    monkeypatch.setattr(
        "trading_agents.options.tools.guard.fetch_option_candidates", _empty_chain
    )
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    verdict = await guard.preflight_chain_is_tradeable(
        underlying="NVDA", direction="long", caps=RiskCaps.aggressive_paper()
    )

    assert verdict.allow is False
    assert verdict.reason == "no_candidates"
