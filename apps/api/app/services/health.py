"""Health-status aggregator.

Pulls per-component liveness signals from across the API + agents + risk
engine. Read-only: never writes, never raises (each component's read is
in its own try-block so a single broken backend doesn't kill the whole
status response).

Each component returns a ``ComponentHealth`` with a status + a short
label. The mobile UI maps the status enum to a color (gain / warning /
danger / muted).

Coverage today:
  - council: last council pass + count today (from MockStore activity).
  - approvals: pending count + oldest-age.
  - broker: user's active connection state.
  - reconciler: last positions_snapshot + breaker state (in-memory check;
    PostgresStore would read circuit_breaker_state).
  - llm_cost: placeholder until the LiteLLM ledger lands.

The Postgres versions of these reads ship as the same protocols flip on
USE_POSTGRES=1; for the in-memory default we surface "unknown" where the
data simply isn't there yet (fresh server, no council runs).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.schemas.health import ComponentHealth, HealthResponse
from app.services.broker_store import BrokerStore, get_broker_store
from app.services.store import Store, get_store

logger = logging.getLogger("api.health")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_age(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    delta = _now() - dt
    if delta.total_seconds() < 60:
        return f"{int(delta.total_seconds())}s ago"
    if delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if delta.total_seconds() < 86400:
        return f"{int(delta.total_seconds() / 3600)}h ago"
    return f"{int(delta.total_seconds() / 86400)}d ago"


# ─────────────────────────────────────────────────────────────────────
# Per-component readers
# ─────────────────────────────────────────────────────────────────────


async def _council_health(store: Store) -> ComponentHealth:
    try:
        activity = await store.list_activity(limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: council read failed — %s", exc)
        return ComponentHealth(status="unknown", label="Unavailable")

    if not activity:
        return ComponentHealth(
            status="warning",
            label="No council runs yet — tap Run on Approvals to seed",
        )

    last = activity[0]
    last_dt = last.timestamp
    cutoff = _now() - timedelta(hours=24)
    today_count = sum(1 for a in activity if a.timestamp >= cutoff)

    # Stale if no run in the last 4 hours of business time (rough).
    if last_dt < _now() - timedelta(hours=8):
        status = "warning"
    else:
        status = "ok"

    return ComponentHealth(
        status=status,
        label=f"{today_count} run{'s' if today_count != 1 else ''} in last 24h · last {_format_age(last_dt)}",
        last_event_at=last_dt,
    )


async def _approvals_health(store: Store) -> ComponentHealth:
    try:
        pending = await store.list_pending()
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: approvals read failed — %s", exc)
        return ComponentHealth(status="unknown", label="Unavailable")

    if not pending:
        return ComponentHealth(status="ok", label="Inbox clear")

    oldest = min(pending, key=lambda p: p.proposed_at)
    age_s = (_now() - oldest.proposed_at).total_seconds()

    # Approvals get stale fast — 15-min default expiry. Warn at 10m.
    if age_s > 600:
        status = "warning"
    else:
        status = "ok"

    return ComponentHealth(
        status=status,
        label=f"{len(pending)} pending · oldest {_format_age(oldest.proposed_at)}",
        last_event_at=oldest.proposed_at,
    )


async def _broker_health(
    broker_store: BrokerStore, user_id: str
) -> ComponentHealth:
    try:
        connections = await broker_store.list_connections(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: broker read failed — %s", exc)
        return ComponentHealth(status="unknown", label="Unavailable")

    active = [c for c in connections if c.status == "active"]
    if not active:
        return ComponentHealth(
            status="warning",
            label="No broker connected — connect Alpaca in Settings",
        )
    conn = active[0]
    env_tag = "paper" if conn.is_paper else "live"
    label = f"{conn.broker.title()} {env_tag} · "
    if conn.last_used_at is not None:
        label += f"last used {_format_age(conn.last_used_at)}"
    else:
        label += "never used"
    return ComponentHealth(
        status="ok",
        label=label,
        last_event_at=conn.last_used_at,
    )


async def _reconciler_health() -> ComponentHealth:
    """Phase 0/1: only Postgres backs the reconciler. With MockStore we
    don't have a real tick to read; we return "unknown" so the UI shows
    a muted pill instead of a misleading green one.
    """
    import os

    if not _is_truthy(os.environ.get("USE_POSTGRES")):
        return ComponentHealth(
            status="unknown",
            label="Reconciler runs only when USE_POSTGRES=1",
        )

    # Postgres path: read the newest positions_snapshot. Keep this in a
    # try so a transient DB blip doesn't 500 the whole health route.
    try:
        from engine.db import async_session_factory
        from engine.db.models import CircuitBreakerState, PositionsSnapshot
        from sqlalchemy import desc, select

        session_factory = async_session_factory()
        async with session_factory() as session:
            snap = (await session.execute(
                select(PositionsSnapshot).order_by(desc(PositionsSnapshot.captured_at)).limit(1)
            )).scalar_one_or_none()
            breaker = (await session.execute(
                select(CircuitBreakerState).limit(1)
            )).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: reconciler read failed — %s", exc)
        return ComponentHealth(status="unknown", label="Reconciler read failed")

    if snap is None:
        return ComponentHealth(
            status="warning",
            label="Reconciler hasn't written a snapshot yet",
        )

    if breaker and breaker.status == "halted":
        return ComponentHealth(
            status="danger",
            label=f"Circuit breaker HALTED · {breaker.halt_reason or 'drawdown'}",
            last_event_at=breaker.halted_at,
        )

    # Reconciler default interval is 30s; warn if older than ~3 cycles.
    age_s = (_now() - snap.captured_at).total_seconds()
    status = "ok" if age_s < 120 else "warning"
    return ComponentHealth(
        status=status,
        label=f"Last tick {_format_age(snap.captured_at)}",
        last_event_at=snap.captured_at,
    )


async def _llm_cost_health() -> ComponentHealth:
    """Sum YTD real-LLM spend from the cost ledger.

    Threshold is configurable via ``LLM_COST_WARN_USD`` (default $25 /
    30 days). Pure-mock processes report ``unknown`` so we don't surface
    "$0.00 spent" as a healthy signal when nothing real has happened.
    """
    from datetime import timedelta as _td
    import os as _os

    from trading_agents.cost_ledger import get_cost_ledger

    ledger = get_cost_ledger()
    try:
        # 30-day window — matches PLAN.md §9's monthly budget framing.
        total, n_calls = await ledger.sum_cost_since(
            _td(days=30), exclude_mock=True
        )
        all_total, all_count = await ledger.sum_cost_since(
            _td(days=30), exclude_mock=False
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(status="unknown", label=f"Cost ledger error: {exc}")

    # Operator can set a soft cap via env.
    try:
        warn_at = float(_os.environ.get("LLM_COST_WARN_USD", "25.00"))
    except ValueError:
        warn_at = 25.0

    if n_calls == 0:
        if all_count == 0:
            return ComponentHealth(
                status="unknown",
                label="No LLM calls in last 30d",
            )
        # Mock-only — explicit so ops sees the situation.
        return ComponentHealth(
            status="ok",
            label=f"Mock-only ({all_count} calls in 30d, $0.00 spend)",
        )

    if total >= warn_at:
        return ComponentHealth(
            status="warning",
            label=f"30d spend ${total:.2f} ≥ ${warn_at:.2f} cap ({n_calls} calls)",
        )
    return ComponentHealth(
        status="ok",
        label=f"30d spend ${total:.2f} across {n_calls} real calls",
    )


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────
# Public
# ─────────────────────────────────────────────────────────────────────


async def build_health_report(*, user_id: str) -> HealthResponse:
    """Aggregate all components. Never raises."""
    store = get_store()
    broker_store = get_broker_store()

    council = await _council_health(store)
    approvals = await _approvals_health(store)
    broker = await _broker_health(broker_store, user_id)
    reconciler = await _reconciler_health()
    llm_cost = await _llm_cost_health()

    return HealthResponse(
        council=council,
        approvals=approvals,
        broker=broker,
        reconciler=reconciler,
        llm_cost=llm_cost,
        generated_at=_now(),
    )
