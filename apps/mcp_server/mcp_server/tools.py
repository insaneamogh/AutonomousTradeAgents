"""The six MCP tools — thin, read/propose-only adapters over the council
and service layer. Every one of these is a plain, undecorated ``async
def`` so it stays directly unit-testable with no MCP transport/client
involved (mirrors this codebase's router-thin/service-thick convention —
see ``apps/api/app/routers/agent.py``'s ``_execute_council``). Only
``mcp_server.server`` imports the MCP SDK and registers these by
reference.

WILL NEVER BUILD, here or anywhere in this package: ``place_order``,
``approve_proposal``, ``execute_trade``, ``cancel_order``,
``close_position`` — anything that mutates broker or portfolio state. No
tool, code path, or flag in this server may reach
``packages/engine/risk`` -> ``packages/broker``. Every tool below either
reads existing state, or runs the deterministic council read-only (a
council pass writes an audit row — never an order). This is the actual
hackathon differentiator against wrapping Alpaca's own execution-capable
MCP tools directly into an LLM's tool loop, which would hand order
placement to a model and violate this codebase's one architectural rule:
agents propose, deterministic code disposes.
"""

from __future__ import annotations

from typing import Any, Literal, cast, get_args

from app.services.council.decisions_list import list_decisions
from app.services.council.ghost_service import build_veto_ledger
from app.services.council.scanner_status import build_scanner_status_report
from app.services.orders.positions_service import list_open_positions
from app.services.watchlist.watchlist_store import SYMBOL_RE, get_watchlist_store
from engine.env import env_flag
from mcp_server.context import DEMO_USER_ID
from trading_agents.features import resolve_feature_provider
from trading_agents.memory import get_confidence_store, get_decision_log
from trading_agents.runtime import run_council

# ─────────────────────────────────────────────────────────────────────
# Tool 1 — run_council_pass
# ─────────────────────────────────────────────────────────────────────

Horizon = Literal["intraday", "short", "mid", "long"]
_VALID_HORIZONS: tuple[str, ...] = get_args(Horizon)


async def run_council_pass(symbol: str, horizon: str = "short") -> dict[str, Any]:
    """Run the deterministic agent council for one symbol and return its
    full rationale (regime, analyst scores, selected strategy + rationale,
    risk verdict, and the proposal itself when risk approved it).

    Read-only / propose-only — this NEVER executes, approves, or
    auto-submits anything. It writes exactly one audit row via the
    decision log, exactly like a real council pass does today, and
    nothing else: it deliberately skips the two extra side effects
    ``apps/api/app/routers/agent.py::_execute_council`` layers on top of
    this same ``run_council()`` call for the mobile app —
    ``store.append_pending()`` (an in-memory pending-list shim) and
    ``schedule_proposal_pending_notification()`` (push fan-out). Firing a
    push notification because an MCP tool call happened is a side effect
    this design explicitly avoids.

    Raises ``ValueError`` for a malformed symbol or an unrecognized
    horizon rather than silently normalizing either — a raised tool
    error here is a Claude-visible, correctable mistake; silently
    coercing a typo'd horizon to "short" would hide it instead.
    """
    normalized_symbol = symbol.strip().upper()
    # Same pattern apps/api/app/schemas/agent.py::AgentRunRequest validates
    # against, and for the same reason documented there: `symbol` is
    # interpolated verbatim into every council node's LLM prompt
    # (``f"Ticker: {state['symbol']}"``), so an unvalidated value is a
    # direct prompt-injection channel. The FastAPI route closes that hole
    # with a Pydantic field pattern before request.symbol ever reaches
    # ``run_council()``; this tool calls ``run_council()`` directly, with
    # no Pydantic model in between, so it has to enforce the same pattern
    # itself or reopen exactly the hole the API layer closed.
    if not SYMBOL_RE.match(normalized_symbol):
        raise ValueError(
            f"{symbol!r} is not a valid US equity/ETF ticker "
            "(A-Z, digits, '.', '-', max 10 chars, starting with a letter)."
        )
    if horizon not in _VALID_HORIZONS:
        raise ValueError(f"horizon must be one of {_VALID_HORIZONS!r}, got {horizon!r}")
    validated_horizon = cast(Horizon, horizon)

    # No equity_resolver (contrast apps/api/app/routers/agent.py::_equity_resolver,
    # which reads the caller's latest reconciler-cached account equity from
    # Postgres). An MCP caller has no authenticated session to resolve real
    # equity from, and this is a first cut — noted here rather than silently
    # skipped: a Postgres-backed run through this tool sizes the ATR sizer
    # against the synthetic-feature 100k equity fixture, not the demo
    # user's real broker equity, even when USE_POSTGRES=1.
    feature_provider = resolve_feature_provider(equity_resolver=None)

    result = await run_council(
        symbol=normalized_symbol,
        horizon=validated_horizon,
        user_id=DEMO_USER_ID,
        feature_provider=feature_provider,
        decision_log=get_decision_log(),
        confidence_store=get_confidence_store(),
    )
    return {**result, "symbol": normalized_symbol, "horizon": validated_horizon}


# ─────────────────────────────────────────────────────────────────────
# Tool 2 — list_positions
# ─────────────────────────────────────────────────────────────────────


async def list_positions() -> dict[str, Any]:
    """Open agent-managed positions for the demo user, with live marks and
    the disclosed exit plan (stop loss / target / time stop).

    Postgres-only, like the route it mirrors
    (``apps/api/app/routers/positions.py``) — ``list_open_positions``
    itself already returns an honest empty list in MockStore mode (there
    is no position ledger without Postgres), so no extra guard is needed
    here.
    """
    positions = await list_open_positions(DEMO_USER_ID)
    return {
        "positions": [p.model_dump(by_alias=True, mode="json") for p in positions],
        "count": len(positions),
    }


# ─────────────────────────────────────────────────────────────────────
# Tool 3 — list_recent_decisions
# ─────────────────────────────────────────────────────────────────────


async def list_recent_decisions(
    symbol: str | None = None,
    action: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Newest-first page of the demo user's council decisions — every
    pass, whether or not it ever became a proposal.

    ``app.services.council.decisions_list.list_decisions`` does not
    self-guard on ``USE_POSTGRES`` (confirmed by reading it) — its router
    does instead (``apps/api/app/routers/decisions.py::list_all``,
    returning an honest empty page rather than calling into a function
    that assumes Postgres is live). This tool has no router in front of
    it, so it replicates that same guard directly.
    """
    if not env_flag("USE_POSTGRES"):
        return {"decisions": [], "total": 0, "postgres_backed": False}

    rows, total = await list_decisions(
        user_id=DEMO_USER_ID,
        symbol=symbol.upper() if symbol else None,
        action=action.upper() if action else None,
        limit=limit,
        offset=offset,
    )
    return {
        "decisions": [r.model_dump(by_alias=True, mode="json") for r in rows],
        "total": total,
        "postgres_backed": True,
    }


# ─────────────────────────────────────────────────────────────────────
# Tool 4 — get_scanner_status
# ─────────────────────────────────────────────────────────────────────


async def get_scanner_status() -> dict[str, Any]:
    """Read-only view of the trigger loop's state — whether the scanner
    is even on, and if so, what it last saw.

    ``build_scanner_status_report`` already reports its own on/off state
    honestly with no guard needed (confirmed by reading it): it returns a
    fully-populated "off" report when the scheduler was never started
    (``COUNCIL_SCHEDULER_ENABLED=0``, or ``USE_POSTGRES=0`` so it never
    started), and never raises.
    """
    report = await build_scanner_status_report()
    return report.model_dump(by_alias=True, mode="json")


# ─────────────────────────────────────────────────────────────────────
# Tool 5 — get_veto_ledger
# ─────────────────────────────────────────────────────────────────────


async def get_veto_ledger(window_days: int = 30) -> dict[str, Any]:
    """Per-rule veto scorecard for the demo user over the trailing window
    — the "here's why the risk gate said no" showcase tool. Surfaces the
    NAMED ``veto_rule`` identifiers (``pdt_block``, ``daily_drawdown_halt``,
    etc.) directly, not a paraphrase of them.

    ``build_veto_ledger`` does not self-guard on ``USE_POSTGRES`` either
    (same situation as ``list_recent_decisions`` — confirmed by reading
    ``ghost_service.py``) — its router raises a 404 instead
    (``apps/api/app/routers/insights.py::_require_postgres``). An MCP
    tool has no HTTP layer to raise a 404 through, and a raised tool
    error is a worse experience for an LLM caller mid-conversation than a
    labeled empty payload it can explain to the user — so this replicates
    the guard directly and returns an honest empty ledger instead of
    calling into a function that would try to open a Postgres session
    that was never configured.
    """
    if not env_flag("USE_POSTGRES"):
        return {
            "window_days": window_days,
            "total_vetoes": 0,
            "total_blocked_notional": 0.0,
            "rules": [],
            "postgres_backed": False,
        }

    ledger = await build_veto_ledger(window_days, user_id=DEMO_USER_ID)
    return {
        "window_days": ledger.window_days,
        "total_vetoes": ledger.total_vetoes,
        "total_blocked_notional": ledger.total_blocked_notional,
        "rules": [
            {
                "rule": r.rule,
                "count": r.count,
                "blocked_notional": r.blocked_notional,
                "ghost_pnl": r.ghost_pnl,
                "prevented_loss_usd": r.prevented_loss_usd,
                "last_at": r.last_at.isoformat() if r.last_at else None,
            }
            for r in ledger.rules
        ],
        "postgres_backed": True,
    }


# ─────────────────────────────────────────────────────────────────────
# Tool 6 — list_watchlist
# ─────────────────────────────────────────────────────────────────────


async def list_watchlist() -> dict[str, Any]:
    """The symbols the demo user has told the agent to track.

    Works with zero Postgres setup, unlike the other Postgres-shaped
    tools above — ``get_watchlist_store()`` falls back to an in-memory
    store when ``USE_POSTGRES`` is off (confirmed by reading
    ``watchlist_store.py``), so there is nothing to guard here.
    """
    items = await get_watchlist_store().list_items(DEMO_USER_ID)
    return {
        "items": [
            {
                "id": i.id,
                "symbol": i.symbol,
                "asset_class": i.asset_class,
                "active": i.active,
                "created_at": i.created_at.isoformat(),
            }
            for i in items
        ],
        "count": len(items),
    }
