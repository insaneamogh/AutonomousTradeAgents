"""Execution-time correctness tests — the audit's executor findings.

  F26  the re-risk at order placement runs the SAME rule chain the council
       ran. It used to build a RiskProposal with both intraday flags
       defaulted False and no specialists at all, so pdt_block (FINRA),
       mis_square_off_block and min_specialist_avg_score could never fire
       at the moment an order actually goes out.
  F27  the confidence floor is checked against the COUNCIL's confidence,
       not conviction_level/5 — the drafter emits those separately.
  F30  two concurrent approvals produce exactly one order; the loser gets
       a named refusal, never a fabricated order id.
  F31  a live agent-mode BUY with no stop/target is refused rather than
       silently placed without broker-side protective legs.

Service-level (no HTTP), mirroring test_executor_risk_context.py.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.schemas.approvals import ApprovalProposalDto
from app.services.broker.broker_store import BrokerConnectionRecord
from app.services.council.mock_store import MockStore
from app.services.orders import executor as executor_mod
from app.services.orders.execution_claim import reset_execution_claims_for_tests
from app.services.orders.executor import RiskInputs, execute_proposal
from broker.types import Side as BrokerSide
from engine.risk import (
    DbRiskState,
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    SpecialistScore,
)

USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _reset_claims() -> None:
    reset_execution_claims_for_tests()
    yield
    reset_execution_claims_for_tests()


# ─────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
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
class _FakeOrder:
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
class _FakeBroker:
    equity: float = 100_000.0
    buying_power: float = 200_000.0
    positions: list[Any] = field(default_factory=list)
    placed: list[_FakeOrder] = field(default_factory=list)
    place_delay: float = 0.0
    options_trading_level: int | None = None

    async def get_account_equity(self) -> float:
        return self.equity

    async def get_buying_power(self) -> float:
        return self.buying_power

    async def get_options_trading_level(self) -> int | None:
        return self.options_trading_level

    async def list_positions(self) -> list[Any]:
        return list(self.positions)

    async def place_order(self, request: Any) -> _FakeOrder:
        if self.place_delay:
            await asyncio.sleep(self.place_delay)
        order = _FakeOrder(
            broker_order_id=f"alp-{len(self.placed) + 1:04d}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
        )
        self.placed.append(order)
        return order


def _conn(*, is_paper: bool = True) -> BrokerConnectionRecord:
    return BrokerConnectionRecord(
        id="conn-1",
        user_id=USER_ID,
        broker="alpaca",
        is_paper=is_paper,
        account_number="PA-TEST",
        encrypted_access_token="enc",
        encrypted_refresh_token=None,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        status="active",
        live_trading_consent=not is_paper,
    )


def _patch_broker(
    monkeypatch: pytest.MonkeyPatch,
    broker: _FakeBroker,
    *,
    is_paper: bool = True,
) -> None:
    @asynccontextmanager
    async def fake_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, _conn(is_paper=is_paper)

    monkeypatch.setattr(executor_mod, "with_broker_client", fake_cm)


def _patch_risk_inputs(monkeypatch: pytest.MonkeyPatch, inputs: RiskInputs) -> None:
    async def fake_inputs(_proposal, *, user_id: str) -> RiskInputs:
        return inputs

    monkeypatch.setattr(executor_mod, "load_risk_inputs", fake_inputs)


def _proposal(
    symbol: str = "NVDA",
    qty: int = 10,
    price: float = 100.0,
    *,
    side: str = "BUY",
    stop_loss: float | None = 90.0,
    target_price: float | None = 120.0,
    conviction: int = 4,
) -> ApprovalProposalDto:
    return ApprovalProposalDto(
        id=f"agent-test-{symbol.lower()}-{side.lower()}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type="MARKET",
        estimated_notional=qty * price,
        stop_loss=stop_loss,
        target_price=target_price,
        rationale="test",
        bull_case="test bull",
        bear_case="test bear",
        risk_level=2,
        conviction_level=conviction,
        proposed_at=datetime.now(UTC),
    )


# ─────────────────────────────────────────────────────────────────────
# F26 — the regulatory rules can actually fire at execution
# ─────────────────────────────────────────────────────────────────────


def _ctx(
    *, equity: float, day_trades: int, holding: str | None = None
) -> RiskContext:
    """Risk context. ``holding`` seeds an open long so a SELL under test is
    a position exit, not a short (which forbid_short_phase_0 vetoes first)."""
    positions = (
        (
            PortfolioPosition(
                symbol=holding,
                qty=50,
                avg_entry_price=100.0,
                market_value=5_000.0,
            ),
        )
        if holding
        else ()
    )
    return RiskContext(
        account_equity=equity,
        cash=equity,
        buying_power=equity,
        open_positions=positions,
        day_trades_last_5d=day_trades,
    )


def test_pdt_block_fires_in_executor_re_risk() -> None:
    """Sub-$25k account, 3 day-trades already used, and this SELL closes a
    same-day position → FINRA's line. Before F26 this could not be
    reached: closes_intraday_position was hard-coded False."""
    decision = executor_mod._re_run_risk(
        _proposal(side="SELL", stop_loss=None, target_price=None),
        _ctx(equity=20_000.0, day_trades=3, holding="NVDA"),
        RiskCaps(),
        RiskInputs(council_confidence=0.9, closes_intraday_position=True),
    )
    assert decision.approved is False
    assert decision.veto_rule == "pdt_block"


def test_pdt_block_does_not_fire_without_the_intraday_flag() -> None:
    """Same account state, an ordinary swing exit → not a day trade."""
    decision = executor_mod._re_run_risk(
        _proposal(side="SELL", stop_loss=None, target_price=None),
        _ctx(equity=20_000.0, day_trades=3, holding="NVDA"),
        RiskCaps(),
        RiskInputs(council_confidence=0.9, closes_intraday_position=False),
    )
    assert decision.approved is True


def test_pdt_block_reaches_the_broker_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the veto stops the order, not just the rule in isolation."""

    async def run() -> None:
        monkeypatch.setenv("TRADING_MODE", "live")
        broker = _FakeBroker(
            equity=20_000.0,
            positions=[
                _FakePosition(
                    symbol="NVDA",
                    qty=50,
                    avg_entry_price=100.0,
                    market_value=5_000.0,
                )
            ],
        )
        _patch_broker(monkeypatch, broker)
        _patch_risk_inputs(
            monkeypatch,
            RiskInputs(council_confidence=0.9, closes_intraday_position=True),
        )

        async def _state(_user_id: str, _equity: float | None) -> DbRiskState:
            return DbRiskState(day_trades_last_5d=4)

        monkeypatch.setattr(executor_mod, "_load_db_state_or_fail", _state)

        store = MockStore()
        dto = _proposal(side="SELL", stop_loss=None, target_price=None)
        await store.append_pending(dto)

        result = await execute_proposal(
            user_id=USER_ID, proposal_id=dto.id, store=store
        )
        assert result.risk_blocked is True
        assert result.risk_veto_rule == "pdt_block"
        assert broker.placed == []

    asyncio.run(run())


def test_specialist_average_floor_fires_at_execution() -> None:
    """An empty specialist list made this rule a permanent no-op."""
    weak = (
        SpecialistScore(name="technical", score=30.0, confidence=0.6),
        SpecialistScore(name="fundamental", score=35.0, confidence=0.5),
    )
    decision = executor_mod._re_run_risk(
        _proposal(),
        _ctx(equity=100_000.0, day_trades=0),
        RiskCaps(),
        RiskInputs(council_confidence=0.9, specialists=weak),
    )
    assert decision.approved is False
    assert decision.veto_rule == "min_specialist_avg_score"


# ─────────────────────────────────────────────────────────────────────
# F27 — conviction is not confidence
# ─────────────────────────────────────────────────────────────────────


def test_low_council_confidence_blocks_even_with_high_conviction() -> None:
    """conviction_level=5 → 1.0 under the old substitution, which sailed
    past the 0.50 floor the council itself would have failed."""
    decision = executor_mod._re_run_risk(
        _proposal(conviction=5),
        _ctx(equity=100_000.0, day_trades=0),
        RiskCaps(),
        RiskInputs(council_confidence=0.31),
    )
    assert decision.approved is False
    assert decision.veto_rule == "min_council_confidence"


def test_absent_confidence_self_gates_instead_of_scoring_conviction() -> None:
    """The AMZN 2026-08-31 bug, at the unit level.

    An unrecorded council confidence must make ``min_council_confidence``
    SELF-GATE OUT, not fall back to ``conviction_level / 5``. Conviction 2
    scored 0.40 under that substitution and was refused at click time by a
    floor (0.42 aggressive / 0.50 conservative) the council had already
    cleared on the real number — live, AMZN was drafted at 0.54.

    Revert check: restore the ``else proposal.conviction_level / 5.0``
    fallback in ``_re_run_risk`` and this fails with
    veto_rule='min_council_confidence'.
    """
    decision = executor_mod._re_run_risk(
        _proposal(conviction=2),
        _ctx(equity=100_000.0, day_trades=0),
        RiskCaps(),
        RiskInputs(council_confidence=None),
    )
    assert decision.approved is True, decision.reason
    assert decision.veto_rule is None
    # A check that did not run must not be reported as one that passed.
    assert "min_council_confidence" not in decision.checks_passed


def test_recorded_confidence_still_runs_the_floor() -> None:
    """Self-gating is for the ABSENT case only. A recorded below-floor
    confidence must still veto here — the re-check stays a real gate."""
    decision = executor_mod._re_run_risk(
        _proposal(conviction=5),
        _ctx(equity=100_000.0, day_trades=0),
        RiskCaps(),
        RiskInputs(council_confidence=0.31),
    )
    assert decision.approved is False
    assert decision.veto_rule == "min_council_confidence"
    assert "0.31" in decision.reason


def test_dto_confidence_is_used_when_no_decision_row_exists() -> None:
    """USE_POSTGRES=0, or a row that cannot be found, must not lose the
    council's number: the DTO carries it too."""
    proposal = _proposal(conviction=2)
    proposal = proposal.model_copy(update={"council_confidence": 0.30})
    decision = executor_mod._re_run_risk(
        proposal,
        _ctx(equity=100_000.0, day_trades=0),
        RiskCaps(),
        RiskInputs(council_confidence=None),
    )
    assert decision.approved is False
    assert decision.veto_rule == "min_council_confidence"


def test_council_and_executor_agree_on_the_same_proposal() -> None:
    """The whole bug in one assertion: whatever the council decided about
    the confidence floor, the approval-time re-check must decide the same.

    Runs the REAL serialisation boundary the live bug hid behind —
    ``runtime._to_proposal_dto`` → ``ApprovalProposalDto`` → ``_re_run_risk``
    — with AMZN's live numbers (confidence 0.54, conviction 2, 18 shares).

    Revert check: drop ``councilConfidence`` from ``_to_proposal_dto`` and
    this fails — the executor refuses a trade the council approved.
    """
    from trading_agents.runtime import _to_proposal_dto

    caps = RiskCaps(min_council_confidence=0.42)
    state = {
        "symbol": "AMZN",
        "context": {"last_price": 266.39, "asset": {}},
        "proposal": {
            "side": "BUY",
            "qty": 18,
            "estimated_notional": 4795.02,
            "confidence": 0.54,
            "conviction_level": 2,
            "risk_level": 3,
            "stop_loss": 252.44,
            "target_price": 301.26,
            "rationale": "r",
            "bull_case": "b",
            "bear_case": "x",
        },
    }

    # 1. The council gate, on the drafter's own dict.
    from engine.risk import RiskProposal, evaluate
    from engine.risk import Side as _S

    council = evaluate(
        RiskProposal(
            symbol="AMZN", side=_S("BUY"), qty=18,
            estimated_notional=4795.02, last_price=266.39,
            confidence=0.54, stop_price=252.44,
        ),
        _ctx(equity=100_000.0, day_trades=0),
        caps,
    )
    assert council.approved is True, council.reason
    assert "min_council_confidence" in council.checks_passed

    # 2. Serialise exactly as the council does when it writes the row, and
    #    parse it back exactly as the approvals API does.
    dto_dict = _to_proposal_dto(state)
    assert dto_dict is not None
    assert dto_dict["councilConfidence"] == pytest.approx(0.54), (
        "the council's confidence must survive onto the persisted proposal — "
        "without it the executor cannot re-check the same number"
    )
    dto = ApprovalProposalDto.model_validate(dto_dict)
    assert dto.council_confidence == pytest.approx(0.54)

    # 3. The approval-time gate must reach the SAME verdict.
    replay = executor_mod._re_run_risk(
        dto, _ctx(equity=100_000.0, day_trades=0), caps, RiskInputs(),
    )
    assert replay.approved is council.approved, (
        f"council approved={council.approved} but executor "
        f"approved={replay.approved} ({replay.veto_rule}: {replay.reason})"
    )


async def test_load_risk_inputs_degrades_without_a_decision_row() -> None:
    """No Postgres → no row → conservative defaults, never an exception."""
    inputs = await executor_mod.load_risk_inputs(_proposal(), user_id=USER_ID)
    assert inputs.council_confidence is None
    assert inputs.specialists == ()
    assert inputs.closes_intraday_position is False
    assert inputs.is_intraday is False


# ─────────────────────────────────────────────────────────────────────
# F30 — double-approve race
# ─────────────────────────────────────────────────────────────────────


def test_concurrent_approvals_place_exactly_one_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both callers find the proposal pending (store.decide only runs after
    placement). Exactly one may reach the broker; the loser is refused by
    name instead of receiving a fabricated order id."""

    async def run() -> None:
        monkeypatch.setenv("TRADING_MODE", "live")
        # A slow broker widens the window both callers race through.
        broker = _FakeBroker(place_delay=0.05)
        _patch_broker(monkeypatch, broker)
        _patch_risk_inputs(monkeypatch, RiskInputs(council_confidence=0.9))

        store = MockStore()
        dto = _proposal()
        await store.append_pending(dto)

        first, second = await asyncio.gather(
            execute_proposal(user_id=USER_ID, proposal_id=dto.id, store=store),
            execute_proposal(user_id=USER_ID, proposal_id=dto.id, store=store),
            return_exceptions=True,
        )

        results = [r for r in (first, second) if not isinstance(r, Exception)]
        assert len(results) == 2, (first, second)

        winners = [r for r in results if r.order is not None]
        losers = [r for r in results if r.order is None]

        assert len(broker.placed) == 1, "a second order reached the broker"
        assert len(winners) == 1
        assert len(losers) == 1
        assert losers[0].risk_blocked is True
        assert losers[0].risk_veto_rule == "concurrent_execution_claim"
        # The winner's order id is a real identifier, not a random UUID.
        assert winners[0].order.client_order_id == f"agent-exec-{dto.id}"

    asyncio.run(run())


def test_claim_is_released_when_the_broker_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient broker failure must stay retryable — the claim can't
    wedge the proposal permanently."""

    async def run() -> None:
        monkeypatch.setenv("TRADING_MODE", "live")
        broker = _FakeBroker()
        _patch_broker(monkeypatch, broker)
        _patch_risk_inputs(monkeypatch, RiskInputs(council_confidence=0.9))

        async def boom(_request: Any) -> _FakeOrder:
            raise RuntimeError("alpaca 503")

        monkeypatch.setattr(broker, "place_order", boom)

        store = MockStore()
        dto = _proposal()
        await store.append_pending(dto)

        with pytest.raises(RuntimeError):
            await execute_proposal(user_id=USER_ID, proposal_id=dto.id, store=store)

        # Retry with a working broker succeeds — the claim was released.
        monkeypatch.undo()
        _patch_broker(monkeypatch, broker)
        _patch_risk_inputs(monkeypatch, RiskInputs(council_confidence=0.9))
        monkeypatch.setenv("TRADING_MODE", "live")
        result = await execute_proposal(
            user_id=USER_ID, proposal_id=dto.id, store=store
        )
        assert result.order is not None
        assert len(broker.placed) == 1

    asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# F31 — no unprotected live entries
# ─────────────────────────────────────────────────────────────────────


def test_live_agent_buy_without_bracket_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
        broker = _FakeBroker()
        _patch_broker(monkeypatch, broker, is_paper=False)
        _patch_risk_inputs(monkeypatch, RiskInputs(council_confidence=0.9))

        store = MockStore()
        dto = _proposal(stop_loss=None, target_price=None)
        await store.append_pending(dto)

        result = await execute_proposal(
            user_id=USER_ID, proposal_id=dto.id, store=store, exit_mode="agent"
        )
        assert result.risk_blocked is True
        assert result.risk_veto_rule == "bracket_legs_required"
        assert broker.placed == []

    asyncio.run(run())


def _option_proposal(
    *,
    occ_symbol: str = "AAPL260901C00250000",
    underlying: str = "AAPL",
    qty: int = 1,
    limit_price: float = 2.50,
    conviction: int = 4,
) -> ApprovalProposalDto:
    # ``symbol`` is the UNDERLYING and ``occ_symbol`` is the contract — the
    # convention the council actually produces (runtime._to_proposal_dto).
    # This fixture used to set both to the OCC string, which made the two
    # indistinguishable and is why nothing caught the executor placing an
    # EQUITY order on the underlying at the option's premium price.
    #
    # Far enough out that this stays inside the [7, 60] DTE window no
    # matter which day this test actually runs on.
    expiry = (datetime.now(UTC) + timedelta(days=45)).date()
    return ApprovalProposalDto(
        id=f"agent-test-{occ_symbol.lower()}",
        symbol=underlying,
        side="BUY",
        is_option=True,
        option_action="buy_to_open",
        occ_symbol=occ_symbol,
        strike=250.0,
        expiry_date=expiry,
        contract_type="call",
        multiplier=100,
        open_interest=500,
        volume=100,
        bid=2.45,
        ask=2.55,
        implied_volatility=0.28,
        days_to_earnings=None,
        qty=qty,
        order_type="LIMIT",
        limit_price=limit_price,
        estimated_notional=qty * limit_price * 100,
        rationale="test",
        bull_case="test bull",
        bear_case="test bear",
        risk_level=2,
        conviction_level=conviction,
        proposed_at=datetime.now(UTC),
    )


def test_live_agent_options_buy_without_bracket_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options mirror of test_live_agent_buy_without_bracket_is_refused
    above: Alpaca cannot bracket a single-leg option order at all — no
    broker-side stop/target is ever possible for one — so the "unprotected
    live order is refused" gate (built for the short-selling feature) must
    NOT apply to options. Unfixed, this branch would refuse every live
    options order, always, unconditionally, since an option proposal never
    carries stop_loss/target_price in the first place.
    """

    async def run() -> None:
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
        monkeypatch.setenv("ALLOW_OPTIONS", "1")
        broker = _FakeBroker(equity=100_000.0, options_trading_level=3)
        _patch_broker(monkeypatch, broker, is_paper=False)
        _patch_risk_inputs(monkeypatch, RiskInputs(council_confidence=0.9))

        store = MockStore()
        dto = _option_proposal()
        await store.append_pending(dto)

        result = await execute_proposal(
            user_id=USER_ID, proposal_id=dto.id, store=store, exit_mode="agent"
        )

        assert result.risk_blocked is False
        assert result.risk_veto_rule != "bracket_legs_required"
        assert len(broker.placed) == 1
        placed = broker.placed[0]
        assert placed.side == BrokerSide.BUY_TO_OPEN
        # THE SEAM. The council writes symbol=underlying / occ_symbol=contract;
        # the broker must be addressed with the CONTRACT. Sending the
        # underlying here places an equity order at the option's premium
        # price — and the OptionBracketNotSupportedError guard silently
        # no-ops too, because OccSymbol.try_parse("AAPL") is None. No test
        # spanned this before; the fixture set both fields to the same
        # string so the two were indistinguishable.
        assert placed.symbol == dto.occ_symbol
        assert placed.symbol != dto.symbol
        assert "options_agent_managed_exit_no_broker_bracket" in result.informational_flags

    asyncio.run(run())


def test_live_manual_exit_without_bracket_still_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit_mode='manual' means the user owns the close — no legs expected."""

    async def run() -> None:
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
        broker = _FakeBroker()
        _patch_broker(monkeypatch, broker, is_paper=False)
        _patch_risk_inputs(monkeypatch, RiskInputs(council_confidence=0.9))

        store = MockStore()
        dto = _proposal(symbol="MSFT", stop_loss=None, target_price=None)
        await store.append_pending(dto)

        result = await execute_proposal(
            user_id=USER_ID, proposal_id=dto.id, store=store, exit_mode="manual"
        )
        assert result.risk_blocked is False
        assert len(broker.placed) == 1

    asyncio.run(run())


def test_paper_agent_buy_without_bracket_still_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paper keeps the warning-only behavior so demos stay smooth."""

    async def run() -> None:
        monkeypatch.setenv("TRADING_MODE", "live")
        broker = _FakeBroker()
        _patch_broker(monkeypatch, broker, is_paper=True)
        _patch_risk_inputs(monkeypatch, RiskInputs(council_confidence=0.9))

        store = MockStore()
        dto = _proposal(symbol="AAPL", stop_loss=None, target_price=None)
        await store.append_pending(dto)

        result = await execute_proposal(
            user_id=USER_ID, proposal_id=dto.id, store=store, exit_mode="agent"
        )
        assert result.risk_blocked is False
        assert len(broker.placed) == 1

    asyncio.run(run())
