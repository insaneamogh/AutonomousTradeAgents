"""FastAPI gateway entry point.

Phase 0/1 surface:
    GET  /health
    GET  /api/v1/account
    GET  /api/v1/activity
    GET  /api/v1/approvals/pending
    POST /api/v1/approvals/{proposal_id}/decision
    POST /api/v1/agent/run            ← runs the LangGraph council

Also serves the web build: the Expo web export of ``apps/mobile`` (the
Platinum Glass desktop UI and the calm/muted mobile UI in one bundle — the
client picks which to render from viewport width, see
``apps/mobile/src/components/DesktopShell.tsx``). Mounted at the very
bottom of this file, after every API route, and entirely absent unless the
Docker build actually produced ``apps/mobile/dist`` — a local
``uvicorn --reload`` run without that build step still just serves the
API, same as always.

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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import get_settings
from app.routers import (
    account,
    activity,
    agent,
    approvals,
    auth,
    broker,
    circuit_breaker,
    decisions,
    insights,
    notifications,
    orders,
    portfolio,
    positions,
    review,
    scanner,
    symbols,
)
from app.routers import (
    health as health_router,
)
from app.routers import (
    strategies as strategies_router,
)
from app.routers import (
    watchlist as watchlist_router,
)
from engine.env import env_flag

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
    except Exception:
        logger.exception("Sentry init failed — continuing without it")


_init_sentry()


# Fixture user id — matches PostgresStore.DEFAULT_USER_ID. Phase 3 derives
# this from real auth claims and the reconciler runs per-user.
_DEFAULT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast in production if critical secrets are missing/default. No-op
    # outside production. This is the last gate before we accept traffic.
    from app.core.config import require_production_readiness

    require_production_readiness()

    reconciler = None
    use_pg = env_flag("USE_POSTGRES")
    enable_reconciler = env_flag("RECONCILER_ENABLED", default=use_pg)

    if use_pg and enable_reconciler:
        # Import lazily so MockStore code paths never pull these in.
        from app.services.broker.broker_store import get_broker_store
        from app.services.orders.reconciler_fleet import FleetConfig, ReconcilerFleet
        from engine.db.session import async_session_factory

        interval = float(os.environ.get("RECONCILER_INTERVAL_SECONDS", "30"))
        threshold = float(os.environ.get("DRAWDOWN_HALT_THRESHOLD_PCT", "-3.0"))

        session_factory = async_session_factory()

        # Seed the fixture user before the first reconciler tick — the
        # store's lazy ensure_seed() only fires on an API request, and a
        # cold-boot reconciler tick would otherwise hit the FK on
        # positions_snapshot.user_id.
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from engine.db.models import User

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

        # Give every existing user the env-key Alpaca PAPER connection when
        # ALPACA_API_KEY / ALPACA_SECRET_KEY are configured. Without a
        # broker_connections row the keys bought market data but nothing
        # could trade, and the UI read "No broker linked". Idempotent, paper
        # only, and never auto-grants live consent.
        from sqlalchemy import select as _select

        from app.services.broker.env_bootstrap import (
            ensure_env_broker_connection,
            env_keys_present,
        )

        if env_keys_present():
            async with session_factory() as session:
                user_ids = (await session.execute(_select(User.id))).scalars().all()
            created = 0
            for uid in user_ids:
                if await ensure_env_broker_connection(str(uid)):
                    created += 1
            logger.info(
                "env broker bootstrap: %d/%d users linked to Alpaca paper",
                created, len(user_ids),
            )

        # Warm the ticker universe so the first typeahead keystroke isn't
        # a 6s wait. Best-effort: search degrades to empty, never 500s.
        from app.services.broker.symbol_search import warm_symbol_cache

        try:
            n = await warm_symbol_cache()
            if n:
                logger.info("symbol search ready: %d tradable tickers", n)
        except Exception:
            logger.exception("symbol cache warm failed — search will lazy-load")

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

        # Scheduled council scans. Everything for an autonomous pass
        # already existed (daily_cron walks the watchlist, notifies on
        # each proposal) but nothing invoked it, so picks only appeared
        # on a manual tap. Opt-in via COUNCIL_SCHEDULER_ENABLED — it
        # spends LLM budget, so it must never arm itself by accident.
        from app.services.council.scheduler import start_council_scheduler

        start_council_scheduler()
    elif use_pg:
        logger.info("PostgresStore active but reconciler disabled (RECONCILER_ENABLED=0)")
    else:
        logger.info("MockStore active — no reconciler")

    try:
        yield
    finally:
        from app.services.council.scheduler import stop_council_scheduler

        await stop_council_scheduler()
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

# The JSON API + a couple of plain-HTML OAuth landing pages: no scripts, no
# styles, no images, never framed. Two carve-outs get their own, looser
# policy: the docs UI (loads Swagger from a CDN) and the web app (the
# Expo export mounted at the bottom of this file — a real script+style
# bundle that fetches the API from the same origin).
_CSP_API = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_CSP_DOCS = (
    "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' https://fastapi.tiangolo.com data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
# react-native-web injects its CSS-in-JS as inline <style> tags at runtime,
# hence 'unsafe-inline' on style-src — there's no build-time hash to pin.
# The desktop (Platinum Glass) tree also loads Inter/Space Grotesk from
# Google Fonts (apps/mobile/src/desktop/runtime.ts) — allowed explicitly
# rather than widened to any host.
_CSP_WEB = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data:; font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
_API_PATH_PREFIX = "/api/"


@app.middleware("http")
async def _security_headers(request, call_next):
    """Baseline hardening headers on every response. Cheap; matters most for
    the broker-OAuth HTML pages and the web app (clickjacking / MIME-sniff)
    and HTTPS pinning behind the TLS-terminating proxy (Railway/Fly)."""
    response = await call_next(request)
    path = request.url.path
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if path.startswith(_DOCS_PATHS):
        csp = _CSP_DOCS
    elif path.startswith(_API_PATH_PREFIX) or path == "/health":
        csp = _CSP_API
    else:
        csp = _CSP_WEB
    response.headers.setdefault("Content-Security-Policy", csp)
    # The web app has no use for any of these either — deny every powerful
    # feature regardless of which policy branch served the response.
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
app.include_router(scanner.router, prefix="/api/v1")
app.include_router(strategies_router.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(symbols.router, prefix="/api/v1")
app.include_router(decisions.router, prefix="/api/v1")
app.include_router(insights.router, prefix="/api/v1")
app.include_router(watchlist_router.router, prefix="/api/v1")


# ─────────────────────────────────────────────────────────────────────
# Web UI — the Expo web export of apps/mobile (Platinum Glass desktop +
# calm/muted mobile, one bundle; the client picks which to render from
# viewport width). Built by the Docker web-builder stage into
# apps/mobile/dist, which lands at this exact relative position in both
# the container (WORKDIR /app, source copied to /app/apps/...) and a
# local checkout — so no env var is needed to find it. Registered dead
# last: every route above still wins its exact/prefix match first, so
# this can only ever catch what nothing else claimed.
# ─────────────────────────────────────────────────────────────────────
_web_dist = Path(__file__).resolve().parents[2] / "mobile" / "dist"

if _web_dist.is_dir():
    app.mount("/_expo", StaticFiles(directory=_web_dist / "_expo"), name="web-expo-static")
    if (_web_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_web_dist / "assets"), name="web-assets")

    _web_index = _web_dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_web_app(full_path: str) -> FileResponse:
        """SPA fallback for everything not matched above. Desktop vs. mobile
        is decided entirely client-side (DesktopShell.tsx reads viewport
        width), so every path — '/', '/positions', '/auth/verify', a deep
        link's landing route — gets the same index.html and expo-router
        renders the right screen from the URL once the bundle boots.

        ``/api/*`` is excluded so a bad/removed endpoint still 404s as JSON
        instead of silently returning the app shell.
        """
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        # No-cache: index.html references content-hashed bundle filenames
        # (e.g. entry-<hash>.js) that change every deploy — a cached shell
        # pointing at a hash the server no longer has would 404 on load.
        return FileResponse(_web_index, headers={"Cache-Control": "no-cache"})

    logger.info("Web UI enabled — serving %s", _web_dist)
else:
    logger.info(
        "Web UI disabled — %s not found (run `pnpm --filter @app/mobile run build` "
        "to generate it locally; the Docker image builds it automatically)",
        _web_dist,
    )
