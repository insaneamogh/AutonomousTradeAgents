"""FastAPI gateway entry point.

Phase 0/1 surface:
    GET  /health
    GET  /api/v1/account
    GET  /api/v1/activity
    GET  /api/v1/approvals/pending
    POST /api/v1/approvals/{proposal_id}/decision
    POST /api/v1/agent/run            ← runs the LangGraph council

Lifespan: when ``USE_POSTGRES=1`` (and the reconciler is enabled), a
background ``Reconciler`` task starts on app startup and runs until shutdown.
It writes ``positions_snapshot`` rows + flips ``circuit_breaker_state``
when the drawdown threshold is breached. See AGENTV1's "Next session"
playbook for the design rationale.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import (
    account,
    activity,
    agent,
    approvals,
    auth,
    broker,
    health as health_router,
    notifications,
    orders,
    portfolio,
    review,
    strategies as strategies_router,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("api.main")


# Fixture user id — matches PostgresStore.DEFAULT_USER_ID. Phase 3 derives
# this from real auth claims and the reconciler runs per-user.
_DEFAULT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    reconciler = None
    use_pg = _is_truthy(os.environ.get("USE_POSTGRES"))
    enable_reconciler = _is_truthy(os.environ.get("RECONCILER_ENABLED", "1" if use_pg else "0"))

    if use_pg and enable_reconciler:
        # Import lazily so MockStore code paths never pull these in.
        from engine.db.session import async_session_factory
        from engine.reconciler import (
            MockBrokerPoller,
            Reconciler,
            ReconcilerConfig,
        )

        interval = float(os.environ.get("RECONCILER_INTERVAL_SECONDS", "30"))
        threshold = float(os.environ.get("DRAWDOWN_HALT_THRESHOLD_PCT", "-3.0"))

        reconciler = Reconciler(
            poller=MockBrokerPoller(),  # Phase 0/1 default; Phase 2 swaps to AlpacaBrokerPoller
            session_factory=async_session_factory(),
            user_id=_DEFAULT_USER_ID,
            config=ReconcilerConfig(
                interval_seconds=interval,
                halt_threshold_pct=threshold,
            ),
        )
        reconciler.start()
        logger.info("reconciler started (interval=%ss, threshold=%s%%)", interval, threshold)
    elif use_pg:
        logger.info("PostgresStore active but reconciler disabled (RECONCILER_ENABLED=0)")
    else:
        logger.info("MockStore active — no reconciler")

    try:
        yield
    finally:
        if reconciler is not None:
            logger.info("stopping reconciler…")
            await reconciler.stop()


app = FastAPI(
    title="Autonomous Trader API",
    version="0.0.1",
    description=(
        "Gateway between the mobile app and the agent council / deterministic engine. "
        "Phase 0/1: in-memory or Postgres store; reconciler when on Postgres."
    ),
    lifespan=lifespan,
)

_effective_cors_origins = settings.effective_cors_origins
if settings.is_production and not _effective_cors_origins:
    # ``effective_cors_origins`` returns [] in production when CORS_ORIGINS
    # is the ``*`` default. Without an allow-list, EVERY cross-origin
    # request is denied — the mobile app can't reach the API at all. Log
    # loud so this gets caught at deploy time instead of via a "why is
    # everything CORS-blocked?" support thread an hour later.
    logger.warning(
        "CORS LOCKOUT — ENV=%s but CORS_ORIGINS is unset or wildcard. "
        "Every cross-origin request will be denied. Set CORS_ORIGINS to "
        "a comma-separated list of allowed origins (e.g. for Expo Go: "
        "'exp://exp.host,https://exp.host').",
        settings.env,
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_effective_cors_origins,
    # We use Bearer-token auth (Authorization header), not cookies, so
    # ``allow_credentials`` could be False. We keep it True for forward
    # compat with any cookie-based admin tooling — works as long as
    # origins are explicit (no wildcard) in production.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Used by Railway / Fly health checks + manual ops.

    Returns ``status: ok`` once the FastAPI app has booted. The
    ``/api/v1/health/full`` endpoint provides per-component depth + is
    Bearer-gated.
    """
    return {
        "status": "ok",
        "env": settings.env,
        "version": app.version,
    }


# v1 routers
app.include_router(auth.router, prefix="/api/v1")
app.include_router(broker.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(account.router, prefix="/api/v1")
app.include_router(activity.router, prefix="/api/v1")
app.include_router(approvals.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(orders.router, prefix="/api/v1")
app.include_router(portfolio.router, prefix="/api/v1")
app.include_router(health_router.router, prefix="/api/v1")
app.include_router(strategies_router.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
