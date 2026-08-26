""""Continue with Google" tests.

Mints a throwaway RSA keypair, serves its public JWK from a mocked JWKS
endpoint, and signs test ID tokens with the private key — the same
injectable-``httpx.AsyncClient`` + ``httpx.MockTransport`` pattern already
proven in ``test_broker.py`` for Alpaca's OAuth token exchange. Google's
real servers are never touched.

The router calls ``google_oauth.verify_google_id_token`` as a MODULE
attribute (``from app.services.auth import google_oauth`` then
``google_oauth.verify_google_id_token(...)``), resolved at call time — the
exact same reason ``test_broker.py`` can monkeypatch
``alpaca_oauth.exchange_code_for_tokens`` and have the router observe it.
We do the same thing here via ``_patch_google_verify``.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import google_oauth  # noqa: E402
from app.services.auth.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.auth.rate_limit import (  # noqa: E402
    GOOGLE_IP_LIMIT,
    reset_rate_limit_for_tests,
)

TEST_CLIENT_ID = "test-client-id.apps.googleusercontent.com"


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Fresh store, rate-limit window, and JWKS cache for every test — the
    JWKS cache especially: without resetting it, a key set mocked in one
    test would silently leak into the next test's assertions."""
    reset_auth_store_for_tests()
    reset_rate_limit_for_tests()
    google_oauth.reset_google_jwks_cache_for_tests()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _configure_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google sign-in configured by default; individual tests that want the
    unconfigured (503) case override this with ``monkeypatch.delenv``."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_IDS", TEST_CLIENT_ID)
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ─────────────────────────────────────────────────────────────────────
# RSA keypair + JWT signing helpers
# ─────────────────────────────────────────────────────────────────────


def _new_rsa_keypair() -> tuple[str, dict[str, int]]:
    """Return (private_key_pem, public_numbers) for a fresh 2048-bit key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    numbers = key.public_key().public_numbers()
    return private_pem, {"n": numbers.n, "e": numbers.e}


def _b64url_uint(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    raw = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk_for(kid: str, pub_numbers: dict[str, int]) -> dict[str, object]:
    """RFC 7518 §6.3.1 — a public RSA JWK from (n, e)."""
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url_uint(pub_numbers["n"]),
        "e": _b64url_uint(pub_numbers["e"]),
    }


def _sign_google_token(
    private_pem: str,
    *,
    kid: str,
    email: str = "user@example.com",
    email_verified: bool = True,
    aud: str = TEST_CLIENT_ID,
    iss: str = "https://accounts.google.com",
    sub: str = "1234567890",
    name: str | None = "Test User",
    exp_delta: timedelta = timedelta(minutes=5),
) -> str:
    """Build + sign a Google-shaped OIDC ID token with our test private key."""
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "iat": now,
        "exp": now + int(exp_delta.total_seconds()),
    }
    if name is not None:
        claims["name"] = name
    return jose_jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


class _JWKSServer:
    """Mutable mocked JWKS endpoint. Tests add/rotate keys mid-test and can
    assert exactly how many times the endpoint was actually hit."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, object]] = {}
        self.call_count = 0

    def add_key(self, kid: str, pub_numbers: dict[str, int]) -> None:
        self.keys[kid] = _jwk_for(kid, pub_numbers)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        return httpx.Response(200, json={"keys": list(self.keys.values())})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))


@pytest.fixture
def jwks_server() -> _JWKSServer:
    return _JWKSServer()


def _patch_google_verify(monkeypatch: pytest.MonkeyPatch, server: _JWKSServer) -> None:
    """Force the router's ``google_oauth.verify_google_id_token`` call to use
    our mocked JWKS transport instead of reaching Google's real servers."""
    real_verify = google_oauth.verify_google_id_token

    async def patched(id_token: str, *, audience, client=None):  # noqa: ANN001, ANN401
        async with server.client() as c:
            return await real_verify(id_token, audience=audience, client=c)

    monkeypatch.setattr(google_oauth, "verify_google_id_token", patched)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


def test_google_login_happy_path_issues_tokens(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    private_pem, pub = _new_rsa_keypair()
    jwks_server.add_key("kid-1", pub)
    _patch_google_verify(monkeypatch, jwks_server)

    token = _sign_google_token(private_pem, kid="kid-1", email="newgoogle@example.com")
    r = client.post("/api/v1/auth/google", json={"idToken": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "newgoogle@example.com"
    assert body["userId"]
    assert body["accessToken"]
    assert body["refreshToken"]
    assert body["accessExpiresInSeconds"] > 0
    assert body["refreshExpiresInSeconds"] > body["accessExpiresInSeconds"]

    me = client.get("/api/v1/auth/me", headers=_bearer(body["accessToken"]))
    assert me.status_code == 200
    assert me.json()["authMethod"] == "google"


# ─────────────────────────────────────────────────────────────────────
# Rejections
# ─────────────────────────────────────────────────────────────────────


def test_google_login_bad_signature_is_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    """Token claims kid-1 but was actually signed with a DIFFERENT private
    key than the one published under kid-1 in the mocked JWKS."""
    _real_pem, pub1 = _new_rsa_keypair()
    attacker_pem, _pub2 = _new_rsa_keypair()
    jwks_server.add_key("kid-1", pub1)
    _patch_google_verify(monkeypatch, jwks_server)

    token = _sign_google_token(attacker_pem, kid="kid-1")
    r = client.post("/api/v1/auth/google", json={"idToken": token})
    assert r.status_code == 401


def test_google_login_wrong_audience_is_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    private_pem, pub = _new_rsa_keypair()
    jwks_server.add_key("kid-1", pub)
    _patch_google_verify(monkeypatch, jwks_server)

    token = _sign_google_token(private_pem, kid="kid-1", aud="some-other-app.apps.googleusercontent.com")
    r = client.post("/api/v1/auth/google", json={"idToken": token})
    assert r.status_code == 401


def test_google_login_unverified_email_is_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    private_pem, pub = _new_rsa_keypair()
    jwks_server.add_key("kid-1", pub)
    _patch_google_verify(monkeypatch, jwks_server)

    token = _sign_google_token(private_pem, kid="kid-1", email_verified=False)
    r = client.post("/api/v1/auth/google", json={"idToken": token})
    assert r.status_code == 401


def test_google_login_expired_token_is_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    private_pem, pub = _new_rsa_keypair()
    jwks_server.add_key("kid-1", pub)
    _patch_google_verify(monkeypatch, jwks_server)

    token = _sign_google_token(private_pem, kid="kid-1", exp_delta=timedelta(seconds=-10))
    r = client.post("/api/v1/auth/google", json={"idToken": token})
    assert r.status_code == 401


def test_google_login_unknown_kid_after_refetch_is_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    """A kid that was NEVER published fails cleanly after exactly one forced
    refetch on top of the cache already being warm — not an infinite
    retry loop."""
    pem_a, pub_a = _new_rsa_keypair()
    jwks_server.add_key("kid-a", pub_a)
    _patch_google_verify(monkeypatch, jwks_server)

    # Warm the cache with a real, successful verification first (1 fetch).
    good_token = _sign_google_token(pem_a, kid="kid-a", email="warm@example.com")
    warm = client.post("/api/v1/auth/google", json={"idToken": good_token})
    assert warm.status_code == 200, warm.text
    assert jwks_server.call_count == 1

    # Now a kid that was NEVER published — the cache is warm but doesn't
    # have it, so this costs exactly one MORE fetch (the forced refetch),
    # then fails cleanly rather than retrying further.
    ghost_pem, _pub_ghost = _new_rsa_keypair()
    token = _sign_google_token(ghost_pem, kid="kid-ghost")
    r = client.post("/api/v1/auth/google", json={"idToken": token})
    assert r.status_code == 401
    assert jwks_server.call_count == 2, "expected exactly one forced refetch, then failure"


# ─────────────────────────────────────────────────────────────────────
# Accepted quirk: magic-link user + Google under the same email
# ─────────────────────────────────────────────────────────────────────


def test_google_login_existing_magic_link_user_keeps_same_id_and_method(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    """``upsert_user`` only sets ``auth_method`` on first INSERT. Pinning
    this exact behavior: a magic-link user signing in with Google under the
    same email resolves to the SAME user_id, and auth_method STAYS
    'magic_link' — not something a future change should silently flip."""
    email = "hybrid-user@example.com"
    challenge = client.post("/api/v1/auth/request-login", json={"email": email}).json()
    ml_issued = client.post(
        "/api/v1/auth/verify", json={"email": email, "token": challenge["devToken"]}
    ).json()
    me_before = client.get("/api/v1/auth/me", headers=_bearer(ml_issued["accessToken"])).json()
    assert me_before["authMethod"] == "magic_link"

    private_pem, pub = _new_rsa_keypair()
    jwks_server.add_key("kid-1", pub)
    _patch_google_verify(monkeypatch, jwks_server)
    token = _sign_google_token(private_pem, kid="kid-1", email=email)

    g_issued = client.post("/api/v1/auth/google", json={"idToken": token}).json()
    assert g_issued["userId"] == ml_issued["userId"]

    me_after = client.get("/api/v1/auth/me", headers=_bearer(g_issued["accessToken"])).json()
    assert me_after["authMethod"] == "magic_link", "accepted quirk: Google never overwrites it"


# ─────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────


def test_google_login_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    """No caller-supplied email to key on before the token verifies, so the
    bucket is IP-only — a flood of garbage tokens still trips it."""
    private_pem, pub = _new_rsa_keypair()
    jwks_server.add_key("kid-1", pub)
    _patch_google_verify(monkeypatch, jwks_server)

    for _ in range(GOOGLE_IP_LIMIT):
        r = client.post("/api/v1/auth/google", json={"idToken": "not-a-real-jwt"})
        assert r.status_code == 401, r.text

    blocked = client.post("/api/v1/auth/google", json={"idToken": "not-a-real-jwt"})
    assert blocked.status_code == 429


# ─────────────────────────────────────────────────────────────────────
# Unconfigured server
# ─────────────────────────────────────────────────────────────────────


def test_google_login_unconfigured_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_IDS", raising=False)
    get_settings.cache_clear()

    r = client.post("/api/v1/auth/google", json={"idToken": "irrelevant"})
    assert r.status_code == 503


# ─────────────────────────────────────────────────────────────────────
# JWKS key rotation — the one piece of new caching logic
# ─────────────────────────────────────────────────────────────────────


def test_google_login_survives_key_rotation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, jwks_server: _JWKSServer
) -> None:
    """Cache starts with only kid-a. A token signed under a brand-new
    kid-b — simulating Google having rotated to a new signing key — must
    still verify, because the unknown kid forces exactly one refetch."""
    private_pem_a, pub_a = _new_rsa_keypair()
    jwks_server.add_key("kid-a", pub_a)
    _patch_google_verify(monkeypatch, jwks_server)

    token_a = _sign_google_token(private_pem_a, kid="kid-a", email="rotation@example.com")
    r1 = client.post("/api/v1/auth/google", json={"idToken": token_a})
    assert r1.status_code == 200, r1.text
    assert jwks_server.call_count == 1

    # Simulate Google publishing a new key under a new kid.
    private_pem_b, pub_b = _new_rsa_keypair()
    jwks_server.add_key("kid-b", pub_b)

    token_b = _sign_google_token(private_pem_b, kid="kid-b", email="rotation@example.com")
    r2 = client.post("/api/v1/auth/google", json={"idToken": token_b})
    assert r2.status_code == 200, r2.text
    assert jwks_server.call_count == 2, "unknown kid should force exactly one refetch"
