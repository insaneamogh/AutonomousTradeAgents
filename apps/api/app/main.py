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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import get_settings
from app.routers import (
    account,
    activity,
    agent,
    approvals,
    auth,
    broker,
    decisions,
    health as health_router,
    insights,
    circuit_breaker,
    notifications,
    orders,
    portfolio,
    positions,
    review,
    strategies as strategies_router,
    watchlist as watchlist_router,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("api.main")


def _init_sentry() -> None:
    """Wire Sentry when SENTRY_DSN is set; hard no-op otherwise. Exceptions
    surfacing in the API + reconciler fleet are otherwise invisible in prod."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry disabled (no SENTRY_DSN)")
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=settings.env,
            # Conservative defaults; tune per traffic. Errors always sent.
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        )
        logger.info("Sentry initialized (env=%s)", settings.env)
    except Exception:  # noqa: BLE001 — telemetry must never block boot
        logger.exception("Sentry init failed — continuing without it")


_init_sentry()


# Fixture user id — matches PostgresStore.DEFAULT_USER_ID. Phase 3 derives
# this from real auth claims and the reconciler runs per-user.
_DEFAULT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast in production if critical secrets are missing/default. No-op
    # outside production. This is the last gate before we accept traffic.
    from app.core.config import require_production_readiness

    require_production_readiness()

    reconciler = None
    use_pg = _is_truthy(os.environ.get("USE_POSTGRES"))
    enable_reconciler = _is_truthy(os.environ.get("RECONCILER_ENABLED", "1" if use_pg else "0"))

    if use_pg and enable_reconciler:
        # Import lazily so MockStore code paths never pull these in.
        from engine.db.session import async_session_factory

        from app.services.broker_store import get_broker_store
        from app.services.reconciler_fleet import FleetConfig, ReconcilerFleet

        interval = float(os.environ.get("RECONCILER_INTERVAL_SECONDS", "30"))
        threshold = float(os.environ.get("DRAWDOWN_HALT_THRESHOLD_PCT", "-3.0"))

        session_factory = async_session_factory()

        # Seed the fixture user before the first reconciler tick — the
        # store's lazy ensure_seed() only fires on an API request, and a
        # cold-boot reconciler tick would otherwise hit the FK on
        # positions_snapshot.user_id.
        from engine.db.models import User
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with session_factory() as session:
            await session.execute(
                pg_insert(User)
                .values(
                    id=_DEFAULT_USER_ID,
                    email="demo@local.dev",
                    display_name="Demo (Phase 0)",
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()

        # Per-user reconciliation against the REAL broker. The mock-poller
        # fallback only exists off-production so a local box with no broker
        # connection still produces snapshots for demos.
        reconciler = ReconcilerFleet(
            session_factory=session_factory,
            broker_store=get_broker_store(),
            config=FleetConfig(
                interval_seconds=interval,
                halt_threshold_pct=threshold,
                allow_mock_fallback=not settings.is_production,
            ),
        )
        reconciler.start()
        logger.info(
            "reconciler fleet started (interval=%ss, threshold=%s%%, mock_fallback=%s)",
            interval, threshold, not settings.is_production,
        )
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
    # Auth is Bearer-only (Authorization header) — no cookies, no browser
    # credentials. Keeping this False means a hostile page can never ride an
    # ambient session, and it stops a future wildcard origin from silently
    # becoming credentialed.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# Host allow-list. Set ALLOWED_HOSTS (comma-separated) on deployed boxes —
# it closes Host-header poisoning + DNS-rebinding against the API. Unset
# falls back to "*" with a production warning rather than hard-failing a
# deploy that predates this env var.
_allowed_hosts = [
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
] or ["*"]
if settings.is_production and _allowed_hosts == ["*"]:
    logger.warning(
        "ALLOWED_HOSTS is unset in production — the API accepts any Host "
        "header. Set it to your API hostname(s), e.g. "
        "'api.example.com,healthcheck.railway.app'."
    )
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)


# Paths whose responses carry bearer tokens or broker-account detail. Marked
# no-store so no proxy, CDN, or browser cache retains them.
_NO_STORE_PREFIXES = ("/api/v1/auth", "/api/v1/broker")

# This is a JSON API plus a couple of plain-HTML OAuth landing pages: no
# scripts, no styles, no images, never framed. The docs UI is the one
# exception (it loads Swagger from a CDN) and gets its own policy.
_CSP_API = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_CSP_DOCS = (
    "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' https://fastapi.tiangolo.com data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def _security_headers(request, call_next):  # noqa: ANN001, ANN201
    """Baseline hardening headers on every response. Cheap; matters most for
    the broker-OAuth HTML pages (clickjacking / MIME-sniff) and HTTPS pinning
    behind the TLS-terminating proxy (Railway/Fly)."""
    response = await call_next(request)
    path = request.url.path
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        _CSP_DOCS if path.startswith(_DOCS_PATHS) else _CSP_API,
    )
    # Nothing here is a user-facing web app — deny every powerful feature.
    response.headers.setdefault(
        "Permissions-Policy",
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()",
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if path.startswith(_NO_STORE_PREFIXES):
        # Access/refresh tokens and broker-account payloads must never be
        # held by an intermediary cache. setdefault would let a route's own
        # weaker value win, so this one is unconditional.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


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
app.include_router(positions.router, prefix="/api/v1")
app.include_router(circuit_breaker.router, prefix="/api/v1")
app.include_router(health_router.router, prefix="/api/v1")
app.include_router(strategies_router.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(decisions.router, prefix="/api/v1")
app.include_router(insights.router, prefix="/api/v1")
app.include_router(watchlist_router.router, prefix="/api/v1")
