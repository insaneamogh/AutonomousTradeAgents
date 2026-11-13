"""Autonomy readiness: why the desk is, or is not, trading on its own.

Deterministic and read-only. Every check reuses the module that owns the
fact (the LLM resolver, the scheduler, the breaker service, the trading-mode
resolver, the cost ledger, the market calendar); nothing is re-derived here
except the arithmetic of combining them. No LLM is constructed or called, no
order is placed, and no secret value is ever put in the report: a key is
reported as present or absent, nothing more.

``build_report`` never raises. A check that errors becomes
``ok=False, severity="warning", detail="could not be read"``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.schemas.autonomy import AutonomyCheck, AutonomyReport

logger = logging.getLogger("api.autonomy")

_LLM_FIX = "Set GLM_API_KEY and LLM_PROVIDER=glm (or ANTHROPIC_API_KEY) on the API"
_UNREADABLE = "could not be read"


def _llm_key_present(provider: str) -> bool:
    """Real key present: set, and not one of the stand-ins ``LLM`` itself
    treats as unset. Inspects config only; never builds an ``LLM``."""
    from trading_agents.llm import LLM, _key_for_provider

    key = _key_for_provider(provider)
    return bool(key) and key.lower() not in LLM._PLACEHOLDER_KEYS


async def _check_llm(user_id: str) -> AutonomyCheck:
    from trading_agents.llm import active_provider

    provider = active_provider()
    if _llm_key_present(provider):
        return AutonomyCheck(
            id="llm", label="LLM available", ok=True, severity="blocker",
            detail=f"Provider {provider} has an API key.",
        )
    return AutonomyCheck(
        id="llm", label="LLM available", ok=False, severity="blocker",
        detail=f"Provider {provider} has no usable API key, so the council would run on canned MOCK output.",
        fix=_LLM_FIX,
    )


async def _check_llm_fallback(user_id: str) -> AutonomyCheck:
    from trading_agents.llm import active_provider, fallback_status

    provider = active_provider()
    if provider != "glm":
        return AutonomyCheck(
            id="llm_fallback", label="LLM fallback", ok=True, severity="info",
            detail=f"Not applicable: provider is {provider}; the fallback only backs GLM.",
        )
    status = fallback_status()
    armed = _llm_key_present("anthropic")
    if status.get("active"):
        return AutonomyCheck(
            id="llm_fallback", label="LLM fallback", ok=False, severity="warning",
            detail=f"GLM is failing; calls are being served by {status.get('model')} ({status.get('reason') or 'reason unknown'}).",
        )
    if armed:
        return AutonomyCheck(
            id="llm_fallback", label="LLM fallback", ok=True, severity="info",
            detail=f"Armed: an Anthropic key is set, so {status.get('model')} takes over if GLM fails.",
        )
    return AutonomyCheck(
        id="llm_fallback", label="LLM fallback", ok=False, severity="info",
        detail="Not armed: no Anthropic key, so a GLM outage stops the council.",
        fix="Set ANTHROPIC_API_KEY on the API to arm the fallback (optional)",
    )


async def _check_scheduler(user_id: str) -> AutonomyCheck:
    from app.services.council import scheduler as sch

    if not sch._enabled():
        return AutonomyCheck(
            id="scheduler", label="Scheduler armed", ok=False, severity="blocker",
            detail="COUNCIL_SCHEDULER_ENABLED is off, so no sweep ever starts.",
            fix="Set COUNCIL_SCHEDULER_ENABLED=1 on the API",
        )
    if sch.get_council_scheduler() is None:
        return AutonomyCheck(
            id="scheduler", label="Scheduler armed", ok=False, severity="warning",
            detail="Enabled in config but not running in this process.",
            fix="Restart the API",
        )
    return AutonomyCheck(
        id="scheduler", label="Scheduler armed", ok=True, severity="blocker",
        detail="The council scheduler is running.",
    )


async def _check_broker(user_id: str) -> AutonomyCheck:
    from app.services.broker.broker_use import get_active_broker_connection

    conn = await get_active_broker_connection(user_id)
    if conn is None:
        return AutonomyCheck(
            id="broker", label="Broker connected", ok=False, severity="blocker",
            detail="No active broker connection.",
            fix="Connect a broker in Settings",
        )
    from app.routers.broker import _needs_reconnect

    if _needs_reconnect(conn):
        return AutonomyCheck(
            id="broker", label="Broker connected", ok=False, severity="blocker",
            detail=f"The {conn.broker} session has expired.",
            fix=f"Reconnect {conn.broker}",
        )
    return AutonomyCheck(
        id="broker", label="Broker connected", ok=True, severity="blocker",
        detail=f"{conn.broker} is connected and the session is alive.",
    )


async def _check_breaker(user_id: str) -> AutonomyCheck:
    from app.services.platform import circuit_breaker_service as cb

    s = await cb.get_status(user_id)
    if s.halted:
        return AutonomyCheck(
            id="breaker", label="Circuit breaker", ok=False, severity="blocker",
            detail="The drawdown breaker is latched: "
            + (s.reason or "trading is halted") + ".",
            fix="Acknowledge the breaker",
        )
    return AutonomyCheck(
        id="breaker", label="Circuit breaker", ok=True, severity="blocker",
        detail="Not latched.",
    )


async def _check_auto_approve(user_id: str) -> AutonomyCheck:
    from app.services.broker.broker_use import get_active_broker_connection
    from engine.env import env_flag

    env_on = env_flag("AUTO_APPROVE_ENABLED")
    conn = await get_active_broker_connection(user_id)
    consent = bool(conn and conn.auto_approve_consent)
    if env_on and consent:
        return AutonomyCheck(
            id="auto_approve", label="Auto-approve", ok=True, severity="warning",
            detail="On: approved entries are placed without a tap (paper only).",
        )
    missing = []
    if not env_on:
        missing.append("AUTO_APPROVE_ENABLED is off")
    if not consent:
        missing.append("no auto-approve consent on the connection")
    return AutonomyCheck(
        id="auto_approve", label="Auto-approve", ok=False, severity="warning",
        detail="Every entry waits for your tap (" + "; ".join(missing) + ").",
        fix="Set AUTO_APPROVE_ENABLED=1 and turn on auto-approve in Settings"
        if not (env_on and consent) else None,
    )


async def _check_trading_mode(user_id: str) -> AutonomyCheck:
    from app.services.platform.trading_mode import live_trading_state

    st = await live_trading_state()
    if st.enabled:
        return AutonomyCheck(
            id="trading_mode", label="Trading mode", ok=True, severity="info",
            detail="Live trading is ON. Auto-approve is off by design: every live entry waits for your tap.",
        )
    return AutonomyCheck(
        id="trading_mode", label="Trading mode", ok=True, severity="info",
        detail="Paper trading.",
    )


def _market_open(now: datetime) -> bool:
    from engine.features import is_us_market_open

    return bool(is_us_market_open(now))


async def _check_market(user_id: str) -> AutonomyCheck:
    if _market_open(datetime.now(UTC)):
        return AutonomyCheck(
            id="market", label="US market", ok=True, severity="info",
            detail="The regular session is open.",
        )
    return AutonomyCheck(
        id="market", label="US market", ok=False, severity="info",
        detail="The US market is closed; the desk waits for the next session.",
    )


async def _check_spend(user_id: str) -> AutonomyCheck:
    from trading_agents.cost_ledger import get_cost_ledger
    from trading_agents.jobs.daily_cron import (
        _max_daily_llm_spend_usd,
        _seconds_since_midnight_utc,
    )

    cap = _max_daily_llm_spend_usd()
    spent, _calls = await get_cost_ledger().sum_cost_since(_seconds_since_midnight_utc())
    if spent >= cap:
        return AutonomyCheck(
            id="spend", label="Daily LLM spend", ok=False, severity="blocker",
            detail=f"${spent:.2f} spent today against a ${cap:.2f} cap; every candidate HOLDs until UTC midnight.",
            fix="Raise MAX_DAILY_LLM_SPEND_USD or wait for UTC midnight",
        )
    return AutonomyCheck(
        id="spend", label="Daily LLM spend", ok=True, severity="blocker",
        detail=f"${spent:.2f} of ${cap:.2f} used today.",
    )


_CHECKS: list[tuple[str, str, str, Callable[[str], Awaitable[AutonomyCheck]]]] = [
    ("llm", "LLM available", "blocker", _check_llm),
    ("llm_fallback", "LLM fallback", "info", _check_llm_fallback),
    ("scheduler", "Scheduler armed", "blocker", _check_scheduler),
    ("broker", "Broker connected", "blocker", _check_broker),
    ("breaker", "Circuit breaker", "blocker", _check_breaker),
    ("auto_approve", "Auto-approve", "warning", _check_auto_approve),
    ("trading_mode", "Trading mode", "info", _check_trading_mode),
    ("market", "US market", "info", _check_market),
    ("spend", "Daily LLM spend", "blocker", _check_spend),
]


async def _run(cid: str, label: str, fn: Callable[[str], Awaitable[AutonomyCheck]], user_id: str) -> AutonomyCheck:
    try:
        return await fn(user_id)
    except Exception:
        logger.exception("autonomy: check %s failed", cid)
        return AutonomyCheck(
            id=cid, label=label, ok=False, severity="warning", detail=_UNREADABLE,
        )


def _sweep_times(now: datetime) -> tuple[datetime | None, datetime | None]:
    """(last, next) baseline sweep. Last is what the running scheduler
    recorded; next is derived with the scheduler's own scan-time helpers,
    and only when the scheduler is armed."""
    from app.services.council import scheduler as sch

    s = sch.get_council_scheduler()
    last = s.last_run_at if s is not None else None
    nxt = None
    if s is not None and sch._flag("COUNCIL_BASELINE_ENABLED", default=True):
        nxt = now + timedelta(seconds=sch._seconds_until_next(now, sch._scan_times()))
    return last, nxt


def _headline(state: str, checks: list[AutonomyCheck]) -> str:
    by_id = {c.id: c for c in checks}
    if state == "blocked":
        first = next(c for c in checks if c.severity == "blocker" and not c.ok)
        return f"Blocked: {first.detail}"
    if state == "running":
        return "Running: the desk is armed and the market is open."
    sched = by_id.get("scheduler")
    if sched is not None and not sched.ok:
        return "Idle: the scheduler is not running."
    return "Idle: waiting for the next US market session."


async def build_report(user_id: str) -> AutonomyReport:
    now = datetime.now(UTC)
    checks = [await _run(cid, label, fn, user_id) for cid, label, _sev, fn in _CHECKS]
    by_id = {c.id: c for c in checks}
    blocked = any(c.severity == "blocker" and not c.ok for c in checks)
    armed = by_id["scheduler"].ok
    market = by_id["market"].ok
    state = "blocked" if blocked else ("running" if armed and market else "idle")
    try:
        last, nxt = _sweep_times(now)
    except Exception:
        logger.exception("autonomy: sweep times unreadable")
        last, nxt = None, None
    return AutonomyReport(
        state=state, headline=_headline(state, checks), checks=checks,
        last_sweep_at=last, next_sweep_at=nxt, as_of=now,
    )
