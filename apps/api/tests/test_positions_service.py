"""positions_service unit tests — the read path behind /api/v1/positions.

Route-level behavior (auth, mock-mode empty list, close error mapping) is
covered by test_positions_route.py. This file pins the P&L/mark math in
``_from_decision``/``_unmanaged`` directly — in particular the options
multiplier fix: an option's ``market_value`` is ALREADY multiplier-scaled
(a total dollar value), so reconstructing a mark from it and subtracting
against ``avg_entry_price``/``fill_avg_price`` (never multiplied) without
correcting for that puts two numbers on different units into the same
subtraction.

Also pins ``_estimate_exit_price``/``_closed_from_decision`` — the
closed-position-history DTO builder behind GET /positions/history. Neither
touches a database, matching this file's own convention (and this
package's: there is no live-DB test harness at all, so
``list_closed_positions``'s actual query path is exercised via mock-mode
route tests + a one-off live-Postgres verification script, not a fake
session here).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.orders.positions_service import (
    _broker_key_for_decision,
    _closed_from_decision,
    _estimate_exit_price,
    _from_decision,
    _unmanaged,
)


def _decision(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        id=uuid.uuid4(),
        symbol="NVDA",
        proposal={"side": "BUY"},
        fill_qty=10,
        fill_avg_price=100.0,
        exit_mode="agent",
        user_responded_at=datetime(2026, 1, 2, tzinfo=UTC),
        triggered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────
# _from_decision — managed (agent-tracked) positions
# ─────────────────────────────────────────────────────────────────────


def test_equity_unrealized_pnl_unaffected_by_absent_multiplier() -> None:
    decision = _decision(symbol="NVDA", fill_qty=10, fill_avg_price=100.0)
    marks = {"NVDA": 104.25}
    dto = _from_decision(decision, marks, {}, status="open")
    assert dto.last_price == 104.25
    # (104.25 - 100.00) * 10 = 42.50
    assert dto.unrealized_pnl == 42.50


def test_brokers_own_unrealized_pl_is_preferred_over_the_derived_one() -> None:
    """Live 2026-09-03: a short equity position (BA) derived to exactly
    $0 unrealized (the snapshot's own market_value happened to equal
    qty * entry, likely a stale/pre-market mark), while Alpaca's own
    dashboard reported a real -$22 for the same position the whole time.
    ``marks`` here is deliberately set so the OLD derivation would produce
    $0 -- proving the broker's own number wins, not that both happen to
    agree."""
    decision = _decision(
        symbol="BA",
        proposal={"side": "SELL", "direction": "short"},
        fill_qty=97,
        fill_avg_price=209.10,
    )
    marks = {"BA": 209.10}  # last == entry -> the derivation alone gives $0
    broker_pnl = {"BA": -22.0}
    dto = _from_decision(decision, marks, broker_pnl, status="open")
    assert dto.last_price == 209.10
    assert dto.unrealized_pnl == -22.0


def test_option_unrealized_pnl_scaled_by_multiplier() -> None:
    """``marks`` carries the RAW abs(market_value)/abs(qty) — for 1
    contract marked at $300 total that's 300.0, not the $3.00 per-contract
    price. A $2.50 entry -> $3.00 mark move on 1 contract must be $50.00,
    not $0.50 (multiplier forgotten) and not some four-digit figure (a
    per-contract entry subtracted against a still-inflated mark)."""
    decision = _decision(
        symbol="AAPL260828C00250000",
        proposal={"side": "BUY", "multiplier": 100},
        fill_qty=1,
        fill_avg_price=2.50,
    )
    marks = {"AAPL260828C00250000": 300.0}
    dto = _from_decision(decision, marks, {}, status="open")
    assert dto.last_price == 3.00
    assert dto.unrealized_pnl == 50.00


def test_option_short_direction_unrealized_pnl_scaled_by_multiplier() -> None:
    """A short option's unrealized P&L mirrors the long formula (gains
    when price falls) — the multiplier applies identically."""
    decision = _decision(
        symbol="AAPL260828C00250000",
        proposal={"side": "SELL", "direction": "short", "multiplier": 100},
        fill_qty=1,
        fill_avg_price=3.00,
    )
    marks = {"AAPL260828C00250000": 250.0}  # raw mv/qty for a $2.50 mark
    dto = _from_decision(decision, marks, {}, status="open")
    assert dto.last_price == 2.50
    # Short gains as price falls: (3.00 - 2.50) * 1 * 100 = 50.00
    assert dto.unrealized_pnl == 50.00


def test_a_long_put_is_not_flipped_by_its_bearish_thesis() -> None:
    """The real bug, live 2026-09-01: a long PUT (BUY to open,
    ``direction="short"`` because the THESIS is bearish) entered at
    $20.55, marked at $18.60 -- a real $195 LOSS on a contract we own --
    rendered as "+$195" because ``is_short`` was reading the thesis label
    instead of the broker side. Alpaca's own account showed this position
    as Long, with a real loss, the entire time.

    Phase A never sells an option (``RiskCaps.options_disabled``'s own
    docstring: "long calls/puts only, no spreads/assignment"), so ``side``
    is ALWAYS "BUY" here regardless of what ``direction`` says -- a
    bearish bet is a fact about the THESIS, not about which side of the
    contract we hold. Every long option's P&L must move the same
    direction as its own price, whether it's a call or a put.
    """
    decision = _decision(
        symbol="META260918P00585000",
        proposal={"side": "BUY", "direction": "short", "multiplier": 100},
        fill_qty=1,
        fill_avg_price=20.55,
    )
    marks = {"META260918P00585000": 1860.0}  # raw mv/qty for an $18.60 mark
    dto = _from_decision(decision, marks, {}, status="open")
    assert dto.direction == "short"
    assert dto.last_price == 18.60
    # A real loss: (18.60 - 20.55) * 1 * 100 = -195.00, NOT +195.00.
    assert dto.unrealized_pnl == -195.00


def test_a_long_call_with_bullish_thesis_is_unaffected() -> None:
    """The mirror case, to pin that the fix didn't just flip the bug the
    other way: a long CALL (``direction="long"``) must keep gaining when
    price rises, exactly as before."""
    decision = _decision(
        symbol="AAPL260828C00250000",
        proposal={"side": "BUY", "direction": "long", "multiplier": 100},
        fill_qty=1,
        fill_avg_price=2.50,
    )
    marks = {"AAPL260828C00250000": 300.0}
    dto = _from_decision(decision, marks, {}, status="open")
    assert dto.unrealized_pnl == 50.00


def test_open_option_position_gets_a_live_mark_via_occ_symbol_not_underlying() -> None:
    """The realistic shape: ``symbol`` is the underlying, ``proposal``
    carries ``isOption``/``occSymbol`` separately (this is what
    runtime._to_proposal_dto and tools/trade.py's _proposal_dto both
    actually write — the OTHER tests in this file happen to set ``symbol``
    to the OCC string directly, which coincidentally still matches `marks`
    even with the old, buggy `marks.get(d.symbol.upper())` lookup and so
    would NOT have caught this bug — see CLAUDE.md §4.2's own callout of
    this exact "symbol set to the OCC string" trap).

    Before the fix, this looked up ``marks.get("AAPL")`` — never found,
    because ``marks`` is keyed by whatever PositionsSnapshot.open_positions
    stored, which is the OCC contract for an option — so last_price and
    unrealized_pnl were always None for every OPEN option position no
    matter how live the mark actually was.
    """
    decision = _decision(
        symbol="AAPL",
        proposal={
            "side": "BUY",
            "isOption": True,
            "occSymbol": "AAPL260828C00250000",
            "contractType": "call",
            "strike": 250.0,
            "expiryDate": "2026-08-28",
            "multiplier": 100,
        },
        fill_qty=1,
        fill_avg_price=2.50,
    )
    marks = {"AAPL260828C00250000": 300.0}  # NOT keyed by "AAPL"
    dto = _from_decision(decision, marks, {}, status="open")
    assert dto.symbol == "AAPL"  # display field stays the underlying
    assert dto.last_price == 3.00
    assert dto.unrealized_pnl == 50.00
    assert dto.is_option is True
    assert dto.occ_symbol == "AAPL260828C00250000"
    assert dto.contract_type == "call"
    assert dto.strike == 250.0
    assert dto.expiry_date is not None and dto.expiry_date.isoformat() == "2026-08-28"
    assert dto.multiplier == 100


def test_equity_position_never_flagged_as_option() -> None:
    decision = _decision(symbol="NVDA", proposal={"side": "BUY"})
    dto = _from_decision(decision, marks={"NVDA": 104.25}, broker_pnl={}, status="open")
    assert dto.is_option is False
    assert dto.occ_symbol is None
    assert dto.contract_type is None
    assert dto.multiplier == 1


def test_pending_fill_option_reports_proposal_qty_and_no_mark() -> None:
    decision = _decision(
        symbol="AAPL260828C00250000",
        proposal={"side": "BUY", "multiplier": 100, "qty": 2},
        fill_qty=None,
        fill_avg_price=None,
    )
    marks = {"AAPL260828C00250000": 300.0}
    dto = _from_decision(decision, marks, {}, status="pending_fill")
    assert dto.last_price is None
    assert dto.unrealized_pnl is None
    assert dto.qty == 2


# ─────────────────────────────────────────────────────────────────────
# _unmanaged — broker positions with no agent decision behind them
# ─────────────────────────────────────────────────────────────────────


def test_unmanaged_option_position_scales_by_multiplier() -> None:
    broker_positions = {
        "MSFT260828P00400000": {
            "symbol": "MSFT260828P00400000",
            "qty": 2,
            "avg_entry_price": 5.00,
            "market_value": 1_200.0,  # 2 contracts * $6.00 mark * 100
            "multiplier": 100,
        }
    }
    snapshot = SimpleNamespace(captured_at=datetime(2026, 1, 5, tzinfo=UTC))
    out = _unmanaged(broker_positions, covered=set(), snapshot=snapshot)

    assert len(out) == 1
    dto = out[0]
    assert dto.last_price == 6.00
    # (6.00 - 5.00) * 2 * 100 = 200.00
    assert dto.unrealized_pnl == 200.00


def test_unmanaged_equity_position_unaffected_by_absent_multiplier() -> None:
    broker_positions = {
        "NVDA": {
            "symbol": "NVDA",
            "qty": 10,
            "avg_entry_price": 100.0,
            "market_value": 1_050.0,
        }
    }
    snapshot = SimpleNamespace(captured_at=datetime(2026, 1, 5, tzinfo=UTC))
    out = _unmanaged(broker_positions, covered=set(), snapshot=snapshot)

    dto = out[0]
    assert dto.last_price == 105.0
    assert dto.unrealized_pnl == 50.0


def test_unmanaged_brokers_own_unrealized_pl_is_preferred_over_the_derived_one() -> None:
    """Same preference as _from_decision's equivalent test: the snapshot's
    own market_value can derive to $0 (or anything else) while the broker's
    own unrealized_pl, carried on the same position dict, is the real
    number — must win."""
    broker_positions = {
        "BA": {
            "symbol": "BA",
            "qty": -97,
            "avg_entry_price": 209.10,
            "market_value": -20282.70,  # 97 * 209.10 -> derives to $0
            "unrealized_pl": -22.0,
        }
    }
    snapshot = SimpleNamespace(captured_at=datetime(2026, 1, 5, tzinfo=UTC))
    out = _unmanaged(broker_positions, covered=set(), snapshot=snapshot)

    dto = out[0]
    assert dto.last_price == 209.10
    assert dto.unrealized_pnl == -22.0


def test_unmanaged_short_option_position_scales_by_multiplier() -> None:
    broker_positions = {
        "MSFT260828P00400000": {
            "symbol": "MSFT260828P00400000",
            "qty": -2,  # short — Alpaca reports both qty and market_value negative
            "avg_entry_price": 6.00,
            "market_value": -1_000.0,  # 2 contracts * $5.00 mark * 100
            "multiplier": 100,
        }
    }
    snapshot = SimpleNamespace(captured_at=datetime(2026, 1, 5, tzinfo=UTC))
    out = _unmanaged(broker_positions, covered=set(), snapshot=snapshot)

    dto = out[0]
    assert dto.direction == "short"
    assert dto.last_price == 5.00
    # Short gains as price falls: (6.00 - 5.00) * 2 * 100 = 200.00
    assert dto.unrealized_pnl == 200.00


def test_unmanaged_option_position_reports_option_facts_not_plain_equity() -> None:
    """schemas/positions.py's is_option/occ_symbol/contract_type/strike/
    expiry_date/multiplier fields existed but nothing ever populated them —
    every position, option or not, rendered isOption=false/occSymbol=null,
    so a $2,392-notional NVDA call showed as "NVDA LONG qty 4" with no
    indication it was a 100x-levered contract rather than 4 shares."""
    broker_positions = {
        "NVDA261002C00225000": {
            "symbol": "NVDA261002C00225000",
            "qty": 4,
            "avg_entry_price": 5.95,
            "market_value": 2_220.0,
            "multiplier": 100,
            "is_option": True,
        }
    }
    snapshot = SimpleNamespace(captured_at=datetime(2026, 1, 5, tzinfo=UTC))
    out = _unmanaged(broker_positions, covered=set(), snapshot=snapshot)

    assert len(out) == 1
    dto = out[0]
    assert dto.symbol == "NVDA"  # underlying, not the OCC string
    assert dto.is_option is True
    assert dto.occ_symbol == "NVDA261002C00225000"
    assert dto.contract_type == "call"
    assert dto.strike == 225.0
    assert dto.expiry_date is not None and dto.expiry_date.isoformat() == "2026-10-02"
    assert dto.multiplier == 100


# ─────────────────────────────────────────────────────────────────────
# _broker_key_for_decision / covered-set — the "no council decision behind
# it" mislabeling bug
# ─────────────────────────────────────────────────────────────────────


def test_covered_option_position_excluded_from_unmanaged() -> None:
    """The core of the "UNMANAGED / no council decision behind it" bug:
    `covered` used to be built from OpenPositionDto.symbol (always the
    underlying) compared directly against broker-reported keys (OCC for an
    option) — so a real, decision-backed option position could NEVER be
    recognized as covered and always fell through to _unmanaged(), no
    matter how many agent_decisions rows named it."""
    decision = _decision(
        symbol="AAPL",
        proposal={"side": "BUY", "isOption": True, "occSymbol": "AAPL260828C00250000"},
    )
    covered = {_broker_key_for_decision(decision)}
    broker_positions = {
        "AAPL260828C00250000": {
            "symbol": "AAPL260828C00250000",
            "qty": 1,
            "avg_entry_price": 2.50,
            "market_value": 300.0,
            "multiplier": 100,
            "is_option": True,
        }
    }
    snapshot = SimpleNamespace(captured_at=datetime(2026, 1, 5, tzinfo=UTC))
    out = _unmanaged(broker_positions, covered, snapshot=snapshot)
    assert out == []


def test_uncovered_option_position_still_appears_unmanaged() -> None:
    """A genuinely unmanaged option (no decision at all, e.g. opened
    directly at the broker) must still surface — the fix must not make
    _unmanaged() blind to real gaps, only to false ones."""
    broker_positions = {
        "AAPL260828C00250000": {
            "symbol": "AAPL260828C00250000",
            "qty": 1,
            "avg_entry_price": 2.50,
            "market_value": 300.0,
            "multiplier": 100,
            "is_option": True,
        }
    }
    snapshot = SimpleNamespace(captured_at=datetime(2026, 1, 5, tzinfo=UTC))
    out = _unmanaged(broker_positions, covered=set(), snapshot=snapshot)
    assert len(out) == 1
    assert out[0].managed is False


def test_broker_key_for_decision_is_underlying_for_equity() -> None:
    decision = _decision(symbol="NVDA", proposal={"side": "BUY"})
    assert _broker_key_for_decision(decision) == "NVDA"


def test_broker_key_for_decision_is_occ_symbol_for_option() -> None:
    decision = _decision(
        symbol="NVDA",
        proposal={"side": "BUY", "isOption": True, "occSymbol": "nvda261002c00225000"},
    )
    assert _broker_key_for_decision(decision) == "NVDA261002C00225000"


# ─────────────────────────────────────────────────────────────────────
# _estimate_exit_price — back-solving the exit price from realized_pnl,
# the only option for an `external_broker` close (order_sync never places
# an order for one, so there is no fill price to read directly).
# ─────────────────────────────────────────────────────────────────────


def test_estimate_exit_price_long_equity() -> None:
    # Entered 100, gained $50 on 10 shares → must have exited at 105.
    assert _estimate_exit_price(
        entry=100.0, realized_pnl=50.0, qty=10, multiplier=1, entry_side="BUY",
    ) == 105.0


def test_estimate_exit_price_short_equity_gains_when_price_falls() -> None:
    # Entered 100 short, gained $50 on 10 shares → must have covered at 95
    # (a short's own mirror-image formula, not the long formula run backwards).
    assert _estimate_exit_price(
        entry=100.0, realized_pnl=50.0, qty=10, multiplier=1, entry_side="SELL",
    ) == 95.0


def test_estimate_exit_price_option_uses_multiplier() -> None:
    # Same numbers as test_option_unrealized_pnl_scaled_by_multiplier above
    # (entry 2.50, mark 3.00, 1 contract, x100 multiplier -> $50 P&L) — the
    # back-solve must recover the SAME 3.00 exit this file already pins as
    # the forward answer, or the two would be answering different questions
    # about the identical trade.
    assert _estimate_exit_price(
        entry=2.50, realized_pnl=50.0, qty=1, multiplier=100, entry_side="BUY",
    ) == 3.00


def test_estimate_exit_price_zero_qty_or_multiplier_returns_none() -> None:
    assert _estimate_exit_price(
        entry=100.0, realized_pnl=50.0, qty=0, multiplier=1, entry_side="BUY",
    ) is None
    assert _estimate_exit_price(
        entry=100.0, realized_pnl=50.0, qty=10, multiplier=0, entry_side="BUY",
    ) is None


# ─────────────────────────────────────────────────────────────────────
# _closed_from_decision — the closed-position history DTO builder
# ─────────────────────────────────────────────────────────────────────


def _closed_decision(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        id=uuid.uuid4(),
        symbol="KO",
        proposal={"side": "BUY"},
        fill_qty=55,
        fill_avg_price=89.19,
        realized_pnl=25.65,
        exit_mode="agent",
        approval_mode="ask",
        close_reason="external_broker",
        closed_at=datetime(2026, 8, 30, 6, 18, 43, tzinfo=UTC),
        user_responded_at=datetime(2026, 8, 27, 9, 27, 18, tzinfo=UTC),
        triggered_at=datetime(2026, 8, 27, 9, 21, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_closed_from_decision_prefers_the_real_close_order_fill_price() -> None:
    """A close Order row's own avg_fill_price is the broker's REAL fill —
    it must win even though the pnl-based estimate would (in a correctly
    computed row) land on the same number, because it is the more direct
    source and does not depend on this function's own back-solve agreeing
    with order_sync's forward math."""
    decision = _closed_decision(fill_avg_price=100.0, realized_pnl=50.0, fill_qty=10)
    close_order = SimpleNamespace(avg_fill_price=105.0)
    dto = _closed_from_decision(decision, close_order)
    assert dto.exit_price == 105.0
    assert dto.exit_price_source == "order_fill"


def test_closed_from_decision_falls_back_to_pnl_estimate_with_no_close_order() -> None:
    """The real shape of every closed row in production today: an
    `external_broker` close has NO Order row at all (order_sync updates
    AgentDecision directly), so this is the only path that can ever run
    for one."""
    decision = _closed_decision(fill_avg_price=100.0, realized_pnl=50.0, fill_qty=10)
    dto = _closed_from_decision(decision, None)
    assert dto.exit_price == 105.0
    assert dto.exit_price_source == "estimated_from_pnl"


def test_closed_from_decision_no_realized_pnl_leaves_exit_price_unset() -> None:
    """Defensive: a closed row with no realized_pnl and no close order
    (should not happen given order_sync always sets both together, but
    this must not crash or fabricate a number if it ever does)."""
    decision = _closed_decision(realized_pnl=None)
    dto = _closed_from_decision(decision, None)
    assert dto.exit_price is None
    assert dto.exit_price_source is None


def test_closed_from_decision_carries_lifecycle_fields() -> None:
    decision = _closed_decision(
        close_reason="external_broker", exit_mode="agent", approval_mode="ask",
    )
    dto = _closed_from_decision(decision, None)
    assert dto.symbol == "KO"
    assert dto.side == "BUY"
    assert dto.direction == "long"
    assert dto.qty == 55
    assert dto.avg_entry_price == 89.19
    assert dto.realized_pnl == 25.65
    assert dto.close_reason == "external_broker"
    assert dto.exit_mode == "agent"
    assert dto.approval_mode == "ask"
    assert dto.closed_at == decision.closed_at
    assert dto.opened_at == decision.user_responded_at


def test_closed_from_decision_short_direction() -> None:
    decision = _closed_decision(
        symbol="XOM",
        proposal={"side": "SELL", "direction": "short"},
        fill_avg_price=157.84,
        realized_pnl=-35.03,
        fill_qty=31,
    )
    dto = _closed_from_decision(decision, None)
    assert dto.side == "SELL"
    assert dto.direction == "short"
    # Short: exit = entry - delta. delta = -35.03 / 31 = -1.13 (rounded).
    # exit = 157.84 - (-1.13) = 158.97.
    assert dto.exit_price == pytest.approx(158.97, abs=0.01)


def test_closed_from_decision_option_facts() -> None:
    decision = _closed_decision(
        symbol="NVDA",
        proposal={
            "side": "BUY",
            "isOption": True,
            "occSymbol": "NVDA260918C00215000",
            "contractType": "call",
            "strike": 215.0,
            "expiryDate": "2026-09-18",
            "multiplier": 100,
        },
        fill_qty=2,
        fill_avg_price=8.70,
        realized_pnl=130.0,
    )
    dto = _closed_from_decision(decision, None)
    assert dto.symbol == "NVDA"
    assert dto.is_option is True
    assert dto.occ_symbol == "NVDA260918C00215000"
    assert dto.contract_type == "call"
    assert dto.strike == 215.0
    assert dto.expiry_date is not None and dto.expiry_date.isoformat() == "2026-09-18"
    assert dto.multiplier == 100
    # delta = 130.0 / (2 * 100) = 0.65 -> exit = 8.70 + 0.65 = 9.35
    assert dto.exit_price == pytest.approx(9.35, abs=0.01)
