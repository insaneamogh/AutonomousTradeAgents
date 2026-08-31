"""positions_service unit tests — the read path behind /api/v1/positions.

Route-level behavior (auth, mock-mode empty list, close error mapping) is
covered by test_positions_route.py. This file pins the P&L/mark math in
``_from_decision``/``_unmanaged`` directly — in particular the options
multiplier fix: an option's ``market_value`` is ALREADY multiplier-scaled
(a total dollar value), so reconstructing a mark from it and subtracting
against ``avg_entry_price``/``fill_avg_price`` (never multiplied) without
correcting for that puts two numbers on different units into the same
subtraction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.orders.positions_service import (
    _broker_key_for_decision,
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
    dto = _from_decision(decision, marks, status="open")
    assert dto.last_price == 104.25
    # (104.25 - 100.00) * 10 = 42.50
    assert dto.unrealized_pnl == 42.50


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
    dto = _from_decision(decision, marks, status="open")
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
    dto = _from_decision(decision, marks, status="open")
    assert dto.last_price == 2.50
    # Short gains as price falls: (3.00 - 2.50) * 1 * 100 = 50.00
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
    dto = _from_decision(decision, marks, status="open")
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
    dto = _from_decision(decision, marks={"NVDA": 104.25}, status="open")
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
    dto = _from_decision(decision, marks, status="pending_fill")
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
