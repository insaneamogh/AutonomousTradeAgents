"""Demo-session tests — docs/IMPL_DEMO_SESSION.md.

The whole feature is one reused field: a demo session sets
``AuthedUser.is_dev_bypass=True`` and every money-moving route already
refuses that via ``require_real_auth``. These tests exist to prove that
reuse actually holds, and to catch a FUTURE mutating route that forgets
the check (``test_every_mutating_route_uses_require_real_auth``).

Covers, per the spec's revert-check table:
  - a demo session is refused by all 6 originally-protected money routes
  - a demo session is refused by the 3 routes that used to leak the bypass
    (agent/run, agent/run/start, watchlist mutations, review grading)
  - generic router introspection: every POST/PUT/PATCH/DELETE route (bar
    the handful of deliberately-public auth flows) depends on
    ``require_real_auth`` somewhere in its dependency tree
  - a demo token can never be used as a normal access token
  - an expired demo token is refused
  - a demo session resolves to DEMO_USER_ID's data, never FIXTURE_USER_ID's
  - the exchange endpoint is rate-limited
  - the exchange endpoint 503s when DEMO_SESSION_ENABLED is off
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

# Same pattern every other auth test file in this suite uses: set env
# defaults BEFORE importing app.main, since settings/env are read at
# request/call time but the suite wants a clean, working default.
os.environ.setdefault("DEV_AUTH_BYPASS", "1")
os.environ.setdefault("DEMO_SESSION_ENABLED", "1")
DEMO_TEST_USER_ID = "77777777-7777-7777-7777-777777777777"
os.environ.setdefault("DEMO_USER_ID", DEMO_TEST_USER_ID)

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.middleware.auth import require_real_auth  # noqa: E402
from app.services.auth.auth_store import FIXTURE_USER_ID, reset_auth_store_for_tests  # noqa: E402
from app.services.auth.jwt_service import mint_demo, verify_access  # noqa: E402
from app.services.auth.rate_limit import DEMO_IP_LIMIT  # noqa: E402
from app.services.watchlist.watchlist_store import (  # noqa: E402
    reset_watchlist_store_for_tests,
)
from trading_agents.memory import (  # noqa: E402
    DecisionEntry,
    get_decision_log,
    reset_memory_stores_for_tests,
)

DUMMY_ID = "00000000-0000-0000-0000-000000000099"


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    reset_auth_store_for_tests()
    reset_watchlist_store_for_tests()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _mint_demo_token(
    user_id: str = DEMO_TEST_USER_ID, *, ttl: timedelta = timedelta(days=1)
) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + ttl
    return mint_demo(secret=settings.jwt_secret, user_id=user_id, expires_at=expires_at)


def _demo_access_token(client: TestClient, user_id: str = DEMO_TEST_USER_ID) -> str:
    """Full flow: mint a demo link token, exchange it like the client
    would, return the resulting access token."""
    token = _mint_demo_token(user_id)
    r = client.post("/api/v1/auth/demo", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refreshToken"] is None, "a demo session must never issue a refresh token"
    assert body["userId"] == user_id
    access_token: str = body["accessToken"]
    return access_token


# ─────────────────────────────────────────────────────────────────────
# The whole feature, as one test: the 6 originally-protected money routes
# ─────────────────────────────────────────────────────────────────────

MONEY_ROUTES = [
    ("POST", f"/api/v1/approvals/{DUMMY_ID}/decision", {"outcome": "declined"}),
    ("POST", f"/api/v1/orders/execute/{DUMMY_ID}", None),
    ("POST", f"/api/v1/positions/{DUMMY_ID}/close", None),
    ("DELETE", f"/api/v1/broker/connections/{DUMMY_ID}", None),
    ("POST", f"/api/v1/broker/connections/{DUMMY_ID}/auto-approve-consent", {"enabled": True}),
    ("POST", "/api/v1/circuit-breaker/acknowledge", None),
]


@pytest.mark.parametrize("method,path,body", MONEY_ROUTES)
def test_demo_session_refused_by_every_mutating_route(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Break this by dropping ``is_dev_bypass=True`` from the demo branch
    in ``get_current_user`` — every case below must then stop 401ing."""
    access = _demo_access_token(client)
    r = client.request(method, path, json=body, headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}: {r.text}"


# ─────────────────────────────────────────────────────────────────────
# The 3 previously-leaky routes (§3 of the spec)
# ─────────────────────────────────────────────────────────────────────

LEAKY_ROUTES = [
    ("POST", "/api/v1/agent/run", {"symbol": "AAPL"}),
    ("POST", "/api/v1/agent/run/start", {"symbol": "AAPL"}),
    ("POST", "/api/v1/watchlist", {"symbol": "AAPL"}),
    ("DELETE", "/api/v1/watchlist/AAPL", None),
    ("POST", f"/api/v1/review/{DUMMY_ID}", {"grade": "good"}),
]


@pytest.mark.parametrize("method,path,body", LEAKY_ROUTES)
def test_previously_leaky_routes_refuse_a_demo_session(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """These 5 endpoints (3 routers) used to accept DEV_AUTH_BYPASS via
    plain ``get_current_user``. A demo session must not reach any of
    them: unbounded LLM spend, a mutated trading universe, or polluted
    reflection data."""
    access = _demo_access_token(client)
    r = client.request(method, path, json=body, headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}: {r.text}"


# ─────────────────────────────────────────────────────────────────────
# Generic introspection — catches a FUTURE route that forgets the check
# ─────────────────────────────────────────────────────────────────────

# The only routes allowed to skip require_real_auth on a mutating verb:
# the handful of flows that are UNAUTHENTICATED BY DESIGN (you call them
# precisely because you don't have a session yet). Any other
# POST/PUT/PATCH/DELETE route must have require_real_auth somewhere in
# its dependency tree.
PUBLIC_AUTH_PATHS = {
    "/api/v1/auth/request-login",
    "/api/v1/auth/verify",
    "/api/v1/auth/refresh",
    "/api/v1/auth/google",
    "/api/v1/auth/google/exchange",
    "/api/v1/auth/demo",
}

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _dependant_uses(
    dependant: Dependant, target: object, *, _seen: set[int] | None = None
) -> bool:
    """Recursively search a FastAPI ``Dependant`` tree for ``target``.

    ``require_real_auth`` itself depends on ``get_current_user`` (a
    sub-dependency, not the route's own top-level Depends), so a route
    that lists ``Depends(require_real_auth)`` shows up as
    ``dependant.dependencies[i].call is require_real_auth`` for some i —
    this walks that tree rather than only checking the top level, which
    is what makes this a genuine check instead of a stub.
    """
    if _seen is None:
        _seen = set()
    if id(dependant) in _seen:
        return False
    _seen.add(id(dependant))
    if dependant.call is target:
        return True
    return any(_dependant_uses(sub, target, _seen=_seen) for sub in dependant.dependencies)


def _mutating_api_routes() -> list[APIRoute]:
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/")
        and route.methods & _MUTATING_METHODS
    ]


def test_every_mutating_route_uses_require_real_auth() -> None:
    """Introspect every POST/PUT/PATCH/DELETE route and assert
    ``require_real_auth`` is somewhere in its dependency tree, unless it's
    on the explicit public-auth-flow allowlist above.

    This is the test the spec calls the one hole the design can develop:
    a future route that copies ``Depends(get_current_user)`` from a GET
    sibling instead of ``require_real_auth`` would otherwise ship a 4th
    leaky route silently. Break it by reverting any one of the fixes in
    this commit (agent.py / watchlist.py / review.py) — that route must
    then appear in the failure list below.
    """
    routes = _mutating_api_routes()
    assert routes, "no mutating routes found at all — the introspection is broken, not the app"

    missing = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes
        if r.path not in PUBLIC_AUTH_PATHS and not _dependant_uses(r.dependant, require_real_auth)
    ]
    assert missing == [], (
        "these mutating routes do not depend on require_real_auth (add it, or if "
        "genuinely public by design add the path to PUBLIC_AUTH_PATHS above): "
        + ", ".join(missing)
    )


def test_public_auth_paths_allowlist_is_accurate() -> None:
    """The inverse check: every path on the allowlist must actually exist
    as a route AND actually lack require_real_auth — otherwise the
    allowlist is stale (e.g. naming a path that got renamed) and silently
    hiding a route the introspection test above should be covering."""
    routes = {r.path: r for r in _mutating_api_routes()}
    for path in PUBLIC_AUTH_PATHS:
        assert path in routes, f"{path!r} is on PUBLIC_AUTH_PATHS but no such route exists"
        assert not _dependant_uses(routes[path].dependant, require_real_auth), (
            f"{path!r} is on PUBLIC_AUTH_PATHS but actually uses require_real_auth — "
            "remove it from the allowlist, it's already covered by the real check"
        )


# ─────────────────────────────────────────────────────────────────────
# Token hygiene
# ─────────────────────────────────────────────────────────────────────


def test_demo_token_cannot_be_used_as_an_access_token(client: TestClient) -> None:
    """A demo-typ token must never verify as an access token via ANY path
    that only checks the signature. Break this by minting the demo token
    with typ='access' (or accepting typ='demo' in verify_access)."""
    demo_token = _mint_demo_token()

    settings = get_settings()
    from app.services.auth.jwt_service import TokenError

    with pytest.raises(TokenError):
        verify_access(secret=settings.jwt_secret, token=demo_token)

    # And end-to-end: presenting it as a Bearer access token must 401.
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {demo_token}"})
    assert r.status_code == 401


def test_expired_demo_token_refused(client: TestClient) -> None:
    """Skip the expiry check to make this fail."""
    expired = _mint_demo_token(ttl=timedelta(seconds=-10))
    r = client.post("/api/v1/auth/demo", json={"token": expired})
    assert r.status_code == 401


def test_demo_exchange_rejects_garbage_token(client: TestClient) -> None:
    r = client.post("/api/v1/auth/demo", json={"token": "not-a-real-token"})
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Resolves to the cron user's REAL data, never the fixture user's
# ─────────────────────────────────────────────────────────────────────


async def _seed_decision(user_id: str, symbol: str) -> DecisionEntry:
    entry = DecisionEntry(
        user_id=user_id,
        symbol=symbol,
        horizon="short",
        triggered_at=datetime.now(UTC) - timedelta(hours=1),
        selected_strategy="momentum",
        selector_confidence=0.6,
        final_action="BUY",
        risk_approved=True,
        fill_qty=10,
        fill_avg_price=200.0,
        realized_pnl=42.0,
        raw_state={"proposal": {"qty": 10, "bull_case": "cron-only-bull", "bear_case": "bear"}},
    )
    return await get_decision_log().record(entry)


def test_demo_resolves_to_the_cron_users_data_not_fixture_user(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving to FIXTURE_USER_ID (the DEV_AUTH_BYPASS identity) instead
    of DEMO_USER_ID is exactly the naive "just flip DEV_AUTH_BYPASS"
    mistake IMPL_DEMO_SESSION.md §0 already ruled out — it gives a judge a
    fully working, completely empty app."""
    import anyio

    reset_memory_stores_for_tests()
    cron_user_id = "88888888-8888-8888-8888-888888888888"
    monkeypatch.setenv("DEMO_USER_ID", cron_user_id)

    cron_decision = anyio.run(_seed_decision, cron_user_id, "CRONONLY")
    anyio.run(_seed_decision, FIXTURE_USER_ID, "FIXTUREONLY")

    access = _demo_access_token(client, user_id=cron_user_id)
    headers = {"Authorization": f"Bearer {access}"}

    body = client.get("/api/v1/review/queue?windowDays=30", headers=headers).json()
    ids = [i["decisionId"] for i in body["items"]]
    assert ids == [cron_decision.id], "demo session must see exactly the cron user's decision"

    raw = client.get("/api/v1/review/queue?windowDays=30", headers=headers).text
    assert "FIXTUREONLY" not in raw, "the fixture user's data leaked into the demo session"


# ─────────────────────────────────────────────────────────────────────
# The exchange endpoint itself: rate-limited, disableable
# ─────────────────────────────────────────────────────────────────────


def test_demo_endpoint_is_rate_limited(client: TestClient) -> None:
    """Remove the limit (or check_demo_rate's call in the router) to make
    this fail. The rate check runs before token verification, so a
    garbage token is enough to exercise it — matching how
    test_verify_is_rate_limited exercises /auth/verify."""
    for _ in range(DEMO_IP_LIMIT):
        r = client.post("/api/v1/auth/demo", json={"token": "garbage"})
        assert r.status_code == 401, r.text

    blocked = client.post("/api/v1/auth/demo", json={"token": "garbage"})
    assert blocked.status_code == 429


def test_demo_endpoint_disabled_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEMO_SESSION_ENABLED", "0")
    token = _mint_demo_token()
    r = client.post("/api/v1/auth/demo", json={"token": token})
    assert r.status_code == 503


def test_demo_endpoint_disabled_when_no_user_id_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither DEMO_USER_ID nor AGENT_CRON_USER_ID set -> nothing for the
    link to resolve to; refuse rather than mint a token to nobody."""
    monkeypatch.delenv("DEMO_USER_ID", raising=False)
    monkeypatch.delenv("AGENT_CRON_USER_ID", raising=False)
    token = _mint_demo_token()
    r = client.post("/api/v1/auth/demo", json={"token": token})
    assert r.status_code == 503


def test_demo_user_id_falls_back_to_agent_cron_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.auth.demo_session import demo_user_id

    monkeypatch.delenv("DEMO_USER_ID", raising=False)
    monkeypatch.setenv("AGENT_CRON_USER_ID", "99999999-9999-9999-9999-999999999999")
    assert demo_user_id() == "99999999-9999-9999-9999-999999999999"
