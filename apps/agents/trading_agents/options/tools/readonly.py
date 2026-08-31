"""The six read-only tool handlers — ``docs/IMPL_OPTIONS_AGENTS.md`` §1.

Every handler has the shape ``(args: dict, ctx, guard_payload=None) -> dict``
— ``guard.dispatch_tool_call`` (``tools/guard.py``) calls EVERY registered
handler, mutating or read-only, with three positional arguments uniformly
(``handler(call.input, ctx, verdict.payload or {})``); the third parameter
is unused here (these tools have nothing for ``ToolGuard.before()`` to hand
down — see its read-only branch) but must be accepted so one dispatch call
site works for all eight registered tools without a special case. It
defaults to ``None`` so every test in this file that calls a handler
directly with just ``(args, ctx)`` — written before this module was wired
into the guard's dispatch — keeps working unchanged. ``ctx`` stays typed
``Any`` rather than imported from ``tools.guard.GuardContext``: these
handlers only ever touch ``ctx.user_id``, and duck-typing that one
attribute keeps this module independently testable against a plain fake
context, not just the concrete production one.

Every handler is defensive end to end: malformed input, a missing row, or
a broker/data failure all degrade to an honest empty/``None``-filled
result rather than raising. ``guard.dispatch_tool_call`` also never lets a
handler's exception escape — but a read-only tool that itself raises would
still be a bug worth avoiding directly, not just one worth catching one
layer up.

Tenant scoping follows ``app.services.council.ghost_service._tenant_filters``
— the same helper the existing ledger/funnel endpoints use — for the three
handlers that read this user's OWN rows (``get_funnel_counts`` via a
WHERE-clause filter; ``get_position_snapshot``/``get_entry_thesis`` via an
explicit ownership check after a by-id lookup, mirroring
``app.services.orders.position_manager.close_position_now``, since
``session.get(Model, id)`` has no WHERE clause to attach a tenant filter
to). ``get_option_snapshot``, ``get_underlying_bars`` and ``get_iv_rank``
read shared MARKET data (the same option chain / bars every tenant would
see), fetched off the process-wide ``ALPACA_API_KEY``/``ALPACA_SECRET_KEY``
data credentials — exactly how ``RealFeatureProvider`` and
``trading_agents.nodes.drafter._fetch_option_candidates`` already fetch
this data today. There is no per-tenant row to scope on for those three;
``ctx`` is still accepted (and unused) so every handler matches the same
dispatch signature.
"""

from __future__ import annotations

import logging
import os
import re
import uuid as _uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

logger = logging.getLogger("agents.options.tools.readonly")

_MAX_FUNNEL_RUNS = 5
_MAX_SNAPSHOT_CONTRACTS = 15
_DEFAULT_BARS_LOOKBACK_DAYS = 30
_MAX_BARS_LOOKBACK_DAYS = 90
_MAX_BARS_RETURNED = 30
_MIN_IV_SAMPLES_FOR_RANK = 5
_MAX_IV_HISTORY = 252


def _user_and_decision_uuid(user_id: str, decision_id: str) -> tuple[_uuid.UUID, _uuid.UUID] | None:
    """Both parsed, or ``None`` — never raises. A malformed id of either
    kind must read as "not found", not crash the tool call."""
    try:
        return _uuid.UUID(user_id), _uuid.UUID(decision_id)
    except (ValueError, TypeError, AttributeError):
        return None


def _alpaca_data_credentials() -> tuple[str, str] | None:
    """The shared market-data keys, or ``None`` when unset — same env vars
    and same "just skip" contract ``_fetch_option_candidates`` uses."""
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        return None
    return api_key, secret_key


# ─────────────────────────────────────────────────────────────────────
# get_funnel_counts
# ─────────────────────────────────────────────────────────────────────


async def get_funnel_counts(args: dict[str, Any], ctx: Any, guard_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Most recent contract-funnel run(s) for one underlying, tenant-scoped.

    Reuses ``app.services.council.funnel_service.build_funnel_report_from_rows``
    for the aggregation (which stage keys mean "absent" vs "zero", which
    stage names the rejection) rather than re-deriving those tolerance
    rules — see that module's own docstring. This handler only adds the
    per-symbol filter and caps the row count to a handful of RECENT single
    runs; the aggregate whole-window report is the API's own job, not this
    tool's.
    """
    underlying = str(args.get("underlying", "")).strip().upper()
    try:
        limit = max(1, min(int(args.get("limit", 1)), _MAX_FUNNEL_RUNS))
    except (TypeError, ValueError):
        limit = 1

    if not underlying:
        return {"underlying": underlying, "runs": [], "error": "missing_underlying"}

    try:
        from sqlalchemy import select

        from app.services.council.funnel_service import build_funnel_report_from_rows
        from app.services.council.ghost_service import _NoSuchTenant, _tenant_filters
        from engine.db import async_session_factory
        from engine.db.models import AgentDecision

        user_id = str(getattr(ctx, "user_id", ""))
        try:
            tenant = _tenant_filters(user_id)
        except _NoSuchTenant:
            return {"underlying": underlying, "runs": []}

        session_factory = async_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(
                    AgentDecision.id,
                    AgentDecision.symbol,
                    AgentDecision.triggered_at,
                    AgentDecision.final_action,
                    AgentDecision.reasoning,
                )
                .where(
                    AgentDecision.symbol == underlying,
                    AgentDecision.reasoning.is_not(None),
                    *tenant,
                )
                .order_by(AgentDecision.triggered_at.desc())
                .limit(limit)
            )
            rows = result.all()

        mapped = [
            {
                "id": row.id,
                "symbol": row.symbol,
                "triggered_at": row.triggered_at,
                "final_action": row.final_action,
                "reasoning": row.reasoning,
            }
            for row in rows
        ]
        # window_days is an unused label here (filtering already happened
        # above, by symbol + limit, not by a date cutoff) — the pure
        # reducer just carries it through to FunnelReport.window_days,
        # which this handler doesn't read back.
        report = build_funnel_report_from_rows(mapped, window_days=0, limit=limit)
        return {
            "underlying": underlying,
            "runs": [
                {
                    "decision_id": run.decision_id,
                    "triggered_at": run.triggered_at.isoformat() if run.triggered_at else None,
                    "stages": [
                        {
                            "stage": s.key,
                            "label": s.label,
                            "survivors": s.survivors,
                            "dropped": s.dropped,
                        }
                        for s in run.stages
                    ],
                    "rejection_reason": run.rejection_reason,
                    "rejection_stage": run.rejection_stage,
                    "selected_occ": run.selected_occ,
                    "outcome": run.outcome,
                }
                for run in report.recent
            ],
        }
    except Exception:
        logger.exception("get_funnel_counts failed for %s", underlying)
        return {"underlying": underlying, "runs": []}


# ─────────────────────────────────────────────────────────────────────
# get_option_snapshot
# ─────────────────────────────────────────────────────────────────────


def _spread_pct(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return round((ask - bid) / mid * 100.0, 2)


def _quote_as_dict(q: Any) -> dict[str, Any]:
    return {
        "occ_symbol": q.occ_symbol,
        "contract_type": q.contract_type,
        "strike": q.strike,
        "expiry": q.expiry.isoformat(),
        "bid": q.bid,
        "ask": q.ask,
        "spread_pct": _spread_pct(q.bid, q.ask),
        "delta": q.delta,
        "implied_volatility": q.implied_volatility,
        "open_interest": q.open_interest,
        # NOT daily volume: OPTIONS_PLAYBOOK.md §1.3 — alpaca-py's
        # OptionsSnapshot drops dailyBar, so this is the size of the last
        # print, typically 1-5 lots. Named explicitly so a reader never
        # mistakes it for real daily volume.
        "volume_last_trade_size": q.volume,
    }


async def get_option_snapshot(args: dict[str, Any], ctx: Any, guard_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Real bid/ask/greeks/IV for a contract or a chain slice.

    Reuses ``engine.options.contracts.fetch_option_candidates`` — the
    function that already merges the CORRECT chain-snapshot client
    (``broker.alpaca.list_option_chain_quotes``, which wraps
    ``OptionHistoricalDataClient.get_option_chain``) with open interest
    from ``broker.alpaca.list_option_contracts`` (``TradingClient``,
    metadata only — no greeks/IV/bid-ask). Reaching for ``TradingClient``
    alone for market data was the exact chain-fetch bug OPTIONS_PLAYBOOK.md
    §5 warns about; this handler does not re-derive which client is
    correct, it calls the function that already got it right.
    """
    del ctx  # market data, not tenant-scoped — see module docstring
    underlying = str(args.get("underlying", "")).strip().upper()
    occ_symbol = str(args.get("occ_symbol") or "").strip().upper() or None
    if not underlying:
        return {"underlying": underlying, "found": False, "error": "missing_underlying"}

    creds = _alpaca_data_credentials()
    if creds is None:
        return {"underlying": underlying, "found": False, "error": "no_data_credentials"}
    api_key, secret_key = creds

    try:
        from engine.options.contracts import fetch_option_candidates

        candidates = await fetch_option_candidates(
            underlying, api_key=api_key, secret_key=secret_key, now=datetime.now(UTC)
        )

        if occ_symbol is not None:
            match = next((q for q in candidates if q.occ_symbol.upper() == occ_symbol), None)
            if match is None:
                return {"underlying": underlying, "occ_symbol": occ_symbol, "found": False}
            return {"underlying": underlying, "found": True, "contract": _quote_as_dict(match)}

        ranked = sorted(candidates, key=lambda q: (q.open_interest or 0), reverse=True)
        top = ranked[:_MAX_SNAPSHOT_CONTRACTS]
        return {
            "underlying": underlying,
            "found": bool(top),
            "total_candidates": len(candidates),
            "contracts": [_quote_as_dict(q) for q in top],
        }
    except Exception:
        logger.exception("get_option_snapshot failed for %s", underlying)
        return {"underlying": underlying, "found": False}


# ─────────────────────────────────────────────────────────────────────
# get_underlying_bars
# ─────────────────────────────────────────────────────────────────────


async def get_underlying_bars(args: dict[str, Any], ctx: Any, guard_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recent daily closes/volume — reuses
    ``engine.features.bars.AlpacaDailyBarsProvider``, the same IEX daily
    bars fetch the live feature provider (``RealFeatureProvider``) uses
    for every analyst, not a second bars implementation.
    """
    del ctx  # market data, not tenant-scoped — see module docstring
    underlying = str(args.get("symbol") or args.get("underlying") or "").strip().upper()
    if not underlying:
        return {"underlying": underlying, "found": False, "error": "missing_underlying"}

    try:
        lookback = int(args.get("lookback_days", _DEFAULT_BARS_LOOKBACK_DAYS))
    except (TypeError, ValueError):
        lookback = _DEFAULT_BARS_LOOKBACK_DAYS
    lookback = max(1, min(lookback, _MAX_BARS_LOOKBACK_DAYS))

    creds = _alpaca_data_credentials()
    if creds is None:
        return {"underlying": underlying, "found": False, "error": "no_data_credentials"}
    api_key, secret_key = creds

    try:
        from engine.features.bars import AlpacaDailyBarsProvider

        provider = AlpacaDailyBarsProvider(api_key, secret_key)
        bars = await provider.daily_bars(underlying, lookback_days=lookback)
        if not bars:
            return {"underlying": underlying, "found": False, "bars": []}

        recent = bars[-_MAX_BARS_RETURNED:]
        first_close, last_close = recent[0].close, recent[-1].close
        pct_change = round((last_close - first_close) / first_close * 100.0, 2) if first_close else None
        return {
            "underlying": underlying,
            "found": True,
            "bars": [{"day": b.day.isoformat(), "close": b.close, "volume": b.volume} for b in recent],
            "last_close": last_close,
            "pct_change_over_window": pct_change,
        }
    except Exception:
        logger.exception("get_underlying_bars failed for %s", underlying)
        return {"underlying": underlying, "found": False}


# ─────────────────────────────────────────────────────────────────────
# get_iv_rank — no real IV-history source exists anywhere in this repo.
# ─────────────────────────────────────────────────────────────────────

# Process-local, in-memory ATM-IV samples per underlying, oldest first:
# {"NVDA": [(date(...), 0.34), ...]}. NOT persisted — a process restart
# empties it. See get_iv_rank's docstring for why this is the honest
# ceiling on what this tool can report rather than a real vendor IV rank.
_IV_HISTORY: dict[str, list[tuple[date, float]]] = {}


def _atm_iv(candidates: Sequence[Any], *, underlying_price: float | None) -> float | None:
    """IV of the candidate closest to the money, or the median-strike
    candidate's IV when the underlying price is unavailable (still "middle
    of the chain", just not verified ATM)."""
    with_iv = [q for q in candidates if q.implied_volatility is not None]
    if not with_iv:
        return None
    if underlying_price is not None:
        nearest = min(with_iv, key=lambda q: abs(q.strike - underlying_price))
        return float(nearest.implied_volatility)
    ordered = sorted(with_iv, key=lambda q: q.strike)
    return float(ordered[len(ordered) // 2].implied_volatility)


async def get_iv_rank(args: dict[str, Any], ctx: Any, guard_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """IV rank built ONLY from ATM-IV samples this same running process has
    itself observed.

    No IV-history/lookback source exists anywhere in this codebase:
    ``MinimalOptionsContextProvider.iv_rank`` (``engine/features/provider.py``)
    is hardcoded ``None``, ``docs/OPTIONS_PLAN.md`` §6 lists a real
    IV-rank/term-structure source as a follow-up, and grepping the repo for
    ``iv_rank``/``iv_percentile`` turns up nothing but this module and that
    docstring. Fabricating a plausible-sounding rank would be worse than an
    absent one — the options risk rules and the agents would have no way
    to tell a real number from a guess.

    HONEST LIMITATIONS — load-bearing for anyone consuming this:
      - The "history" is an in-memory dict, one per process, never
        persisted anywhere. A restart empties it. Depth is bounded by how
        long this runtime has been calling this tool, not a real trailing
        window (a true 252-trading-day rank would need this process to
        stay up and get called daily for roughly a year).
      - One sample per calendar day per underlying (deliberately
        deduplicated — see below), taken from the option chain's
        near-the-money contract, not a dedicated IV time series.
      - Below ``_MIN_IV_SAMPLES_FOR_RANK`` observed samples, ``iv_rank`` is
        ``None`` with a named reason, never a number computed from too
        little data.

    "IV rank" here means the classic ``(current - min) / (max - min)``
    over the observed samples (0 = lowest this runtime has seen, 100 =
    highest) — not the count-based "IV percentile" variant.
    """
    del ctx  # market data, not tenant-scoped — see module docstring
    underlying = str(args.get("underlying", "")).strip().upper()
    if not underlying:
        return {"underlying": underlying, "iv_rank": None, "reason": "missing_underlying"}

    creds = _alpaca_data_credentials()
    if creds is None:
        return {"underlying": underlying, "iv_rank": None, "reason": "no_data_credentials"}
    api_key, secret_key = creds

    try:
        from engine.features.bars import AlpacaDailyBarsProvider
        from engine.options.contracts import fetch_option_candidates

        now = datetime.now(UTC)
        bars = await AlpacaDailyBarsProvider(api_key, secret_key).daily_bars(underlying, lookback_days=5)
        underlying_price = bars[-1].close if bars else None

        candidates = await fetch_option_candidates(
            underlying, api_key=api_key, secret_key=secret_key, now=now
        )
        atm_iv = _atm_iv(candidates, underlying_price=underlying_price)
        if atm_iv is None:
            return {"underlying": underlying, "iv_rank": None, "reason": "no_iv_available"}

        history = _IV_HISTORY.setdefault(underlying, [])
        today = now.date()
        # One sample per day, not per call: several escalations/re-checks
        # on the same trading day are the same market condition, and
        # counting each separately would inflate "history" with duplicates
        # rather than genuinely new information.
        if not history or history[-1][0] != today:
            history.append((today, atm_iv))
            del history[:-_MAX_IV_HISTORY]

        samples = [iv for _, iv in history]
        if len(samples) < _MIN_IV_SAMPLES_FOR_RANK:
            return {
                "underlying": underlying,
                "atm_iv": atm_iv,
                "iv_rank": None,
                "reason": "insufficient_history",
                "samples": len(samples),
                "samples_needed": _MIN_IV_SAMPLES_FOR_RANK,
            }

        lo, hi = min(samples), max(samples)
        if hi <= lo:
            return {
                "underlying": underlying,
                "atm_iv": atm_iv,
                "iv_rank": None,
                "reason": "no_iv_variance_observed",
                "samples": len(samples),
            }

        rank = round((atm_iv - lo) / (hi - lo) * 100.0, 1)
        return {
            "underlying": underlying,
            "atm_iv": atm_iv,
            "iv_rank": rank,
            "samples": len(samples),
            "lookback_note": (
                "process-local observed history only, not a true trailing "
                "window — see get_iv_rank's docstring"
            ),
        }
    except Exception:
        logger.exception("get_iv_rank failed for %s", underlying)
        return {"underlying": underlying, "iv_rank": None, "reason": "error"}


# ─────────────────────────────────────────────────────────────────────
# get_position_snapshot
# ─────────────────────────────────────────────────────────────────────


async def get_position_snapshot(args: dict[str, Any], ctx: Any, guard_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Current state of one open option position, tenant-checked by
    comparing the loaded row's ``user_id`` against the caller's — the same
    ownership check ``position_manager.close_position_now`` uses, since a
    by-id ``session.get`` has no WHERE clause to attach ``_tenant_filters``
    to. A wrong-owner or malformed id returns the same ``found: False``
    shape a genuinely missing id would — never a different error that
    would let a caller distinguish "not yours" from "doesn't exist".

    Reuses ``position_manager._ratchet_outcome_for`` +
    ``_option_pl_pct_by_symbol`` for the peak/trail-line/current-P&L
    numbers rather than re-deriving the ratchet math, and
    ``engine.options.expiry.dte`` for days-to-expiry, matching every other
    caller of that function.
    """
    decision_id = str(args.get("decision_id", "")).strip()
    user_id = str(getattr(ctx, "user_id", ""))
    not_found = {"decision_id": decision_id, "found": False}

    ids = _user_and_decision_uuid(user_id, decision_id)
    if ids is None:
        return not_found
    uid, did = ids

    try:
        from app.services.orders.position_manager import (
            _coerce_expiry_date,
            _option_pl_pct_by_symbol,
            _ratchet_outcome_for,
        )
        from engine.db import async_session_factory
        from engine.db.models import AgentDecision
        from engine.options.expiry import dte
        from engine.risk import RiskCaps

        session_factory = async_session_factory()
        async with session_factory() as session:
            decision = await session.get(AgentDecision, did)

        if decision is None or decision.user_id != uid:
            return not_found

        proposal = decision.proposal or {}
        if not bool(proposal.get("isOption", proposal.get("is_option", False))):
            return {**not_found, "found": True, "is_option": False}

        occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
        now = datetime.now(UTC)
        caps = RiskCaps.from_env()

        option_pl_pct = await _option_pl_pct_by_symbol(user_id) if occ else {}
        outcome = _ratchet_outcome_for(decision, option_pl_pct, caps)

        # ``proposal`` is the camelCase ApprovalProposalDto for any row the
        # risk officer approved (decision_log.py's own DecisionEntry.proposal_dto
        # docstring, confirmed in memory/postgres.py's record()) — an open
        # position is always such a row. ``limit_price`` is checked too only
        # for a pre-DTO/raw-state row (the unapproved-row fallback that same
        # write path documents), matching position_manager's own dual-key
        # reads for occSymbol/isOption/expiryDate elsewhere on this dict.
        raw_limit_price = proposal.get("limitPrice", proposal.get("limit_price"))
        entry_premium = (
            float(decision.fill_avg_price)
            if decision.fill_avg_price is not None
            else (float(raw_limit_price) if raw_limit_price is not None else None)
        )
        expiry = _coerce_expiry_date(proposal.get("expiryDate", proposal.get("expiry_date")))
        entered_at = decision.user_responded_at or decision.triggered_at
        days_held = (now.date() - entered_at.date()).days if entered_at else None

        return {
            "decision_id": decision_id,
            "found": True,
            "is_option": True,
            "underlying": decision.symbol,
            "occ_symbol": occ,
            "entry_premium": entry_premium,
            "current_pl_pct": (option_pl_pct or {}).get(str(occ).upper()) if occ else None,
            "peak_pl_pct": outcome.peak_pl_pct if outcome is not None else None,
            "trail_line_pct": outcome.trail_line_pct if outcome is not None else None,
            "armed": outcome.armed if outcome is not None else None,
            "dte": dte(expiry, now) if expiry is not None else None,
            "days_held": days_held,
            "closed": decision.closed_at is not None,
        }
    except Exception:
        logger.exception("get_position_snapshot failed for decision_id=%s", decision_id)
        return not_found


# ─────────────────────────────────────────────────────────────────────
# get_entry_thesis
# ─────────────────────────────────────────────────────────────────────

# "within/in/by N <unit>" — the shape IMPL_OPTIONS_AGENTS.md §3.2's own
# prompt example uses ("NVDA breaks 190 within 3 weeks"). Deliberately
# narrow: see _parse_thesis_deadline's docstring for what this does not
# understand.
_TIMEFRAME_RE = re.compile(
    r"\b(?:within|in|by)\s+(\d+)\s*(day|days|wk|wks|week|weeks|month|months)\b",
    re.IGNORECASE,
)
_UNIT_DAYS = {
    "day": 1, "days": 1,
    "wk": 7, "wks": 7, "week": 7, "weeks": 7,
    "month": 30, "months": 30,
}


def _parse_thesis_deadline(thesis: str, *, anchor: date) -> date | None:
    """Best-effort deadline extraction, anchored at entry time.

    Honest limitation: this is a narrow regex over relative-duration
    phrasing ("within 3 weeks", "in 10 days", "by 2 months"). It does not
    understand explicit calendar dates, weekday names, or looser phrasing
    ("by the end of the month"). A thesis it cannot parse returns ``None``
    rather than a guessed date — this tool is a read surface for whatever
    thesis text already exists, not the guard's own (separate)
    thesis-without-timeframe validator, and a wrong guess here would be
    worse than an honest absence.
    """
    match = _TIMEFRAME_RE.search(thesis or "")
    if match is None:
        return None
    unit_days = _UNIT_DAYS.get(match.group(2).lower())
    if unit_days is None:
        return None
    return anchor + timedelta(days=int(match.group(1)) * unit_days)


async def get_entry_thesis(args: dict[str, Any], ctx: Any, guard_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """The original thesis + parsed deadline for one decision, tenant-
    checked the same way ``get_position_snapshot`` is (see that function's
    docstring for why an ownership comparison, not a WHERE-clause filter).

    The thesis text is read from ``proposal.rationale`` — NOT
    ``reasoning.drafter_rationale``, despite that field sounding like the
    obvious source. Verified by reading
    ``nodes/drafter.py``: ``drafter_rationale`` is only ever set on a HOLD
    (verdict HOLD, or the sizer zeroing the qty) — it is absent on every
    approved/filled decision, which is the only kind of decision an open
    position (what this tool is asked about) can be. ``reasoning.
    drafter_rationale`` is kept as a fallback only for the theoretical case
    of a pre-fill decision row with no proposal rationale at all.

    ``bull_case``/``bear_case`` are read as ``bullCase``/``bearCase`` first:
    an approved row's ``proposal`` is the camelCase ApprovalProposalDto
    (``decision_log.py``'s ``DecisionEntry.proposal_dto`` docstring,
    confirmed in ``memory/postgres.py``'s ``record()``), and an open
    position is always such a row. ``rationale`` needs no such dual read —
    the DTO uses that same key unchanged.
    """
    decision_id = str(args.get("decision_id", "")).strip()
    user_id = str(getattr(ctx, "user_id", ""))
    not_found = {"decision_id": decision_id, "found": False}

    ids = _user_and_decision_uuid(user_id, decision_id)
    if ids is None:
        return not_found
    uid, did = ids

    try:
        from engine.db import async_session_factory
        from engine.db.models import AgentDecision

        session_factory = async_session_factory()
        async with session_factory() as session:
            decision = await session.get(AgentDecision, did)

        if decision is None or decision.user_id != uid:
            return not_found

        proposal = decision.proposal or {}
        reasoning = decision.reasoning or {}
        thesis = str(
            proposal.get("rationale") or reasoning.get("drafter_rationale") or ""
        ).strip()
        bull_case = str(proposal.get("bullCase", proposal.get("bull_case")) or "").strip()
        bear_case = str(proposal.get("bearCase", proposal.get("bear_case")) or "").strip()

        entered_at = decision.user_responded_at or decision.triggered_at
        anchor = (entered_at or datetime.now(UTC)).date()
        deadline = _parse_thesis_deadline(thesis, anchor=anchor)

        return {
            "decision_id": decision_id,
            "found": True,
            "underlying": decision.symbol,
            "thesis": thesis or None,
            "bull_case": bull_case or None,
            "bear_case": bear_case or None,
            "entered_at": entered_at.isoformat() if entered_at else None,
            "parsed_deadline": deadline.isoformat() if deadline else None,
            "deadline_passed": bool(deadline is not None and datetime.now(UTC).date() > deadline),
        }
    except Exception:
        logger.exception("get_entry_thesis failed for decision_id=%s", decision_id)
        return not_found
