"""JWT mint + verify — stdlib HMAC-SHA256.

This is a tight, internal implementation of HS256 JWT using only the
standard library (``hmac``, ``hashlib``, ``base64``, ``json``, ``secrets``).
It exists because we can't pull ``python-jose`` until the user runs
``uv sync`` (lockfile commits are hands-off). The shape mirrors the
``python-jose`` API so swapping in later is one import + a config flag.

We rely on **stdlib crypto primitives only** — HMAC, SHA-256, base64,
``hmac.compare_digest`` for constant-time verification. The HS256 spec
(RFC 7519 §3.1) is one line of code on top of those primitives; we're
not inventing the algorithm.

Token shape (compact JWS):
    base64url(header_json) + "." + base64url(payload_json) + "." + base64url(signature)
    header_json = {"alg": "HS256", "typ": "JWT"}

DO NOT:
  - Lower the algorithm to "none" via header overrides — we always
    compare against ``_HEADER_BYTES`` byte-for-byte. Algorithm-confusion
    attacks (alg=none, alg=HS256-via-RSA-public-key) are closed.
  - Use this for asymmetric signing (RSA/ECDSA). Asymmetric is python-jose
    territory; if we need that, that's the trigger to flip the dep.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


class TokenError(Exception):
    """Raised on any verification failure. Routers translate to HTTP 401."""


_HEADER = {"alg": "HS256", "typ": "JWT"}


def _b64u_encode(b: bytes) -> str:
    """URL-safe base64 with stripped padding (per JWS spec)."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    """Inverse of ``_b64u_encode``; tolerates missing padding."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _header_bytes() -> bytes:
    # Cached, deterministic encoding so header_b64 is stable across processes.
    return _b64u_encode(json.dumps(_HEADER, separators=(",", ":"), sort_keys=True).encode()).encode()


_HEADER_BYTES = _header_bytes()


def _sign(secret: str, signing_input: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256)
    return _b64u_encode(mac.digest())


@dataclass(frozen=True)
class Claims:
    """Decoded claims. Times are tz-aware UTC."""

    sub: str
    """Subject — user id (UUID string)."""
    iat: datetime
    exp: datetime
    typ: str
    """"access" or "refresh" — discriminator so an access token can't be
    passed to the refresh endpoint and vice versa."""
    sid: str | None = None
    """Session id (refresh tokens only). Lets the auth store look up the
    matching ``user_sessions`` row + check revocation."""
    extra: dict[str, Any] | None = None


def mint(
    *,
    secret: str,
    user_id: str,
    typ: str,
    lifetime: timedelta,
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Build + sign a JWT. Use the convenience wrappers below.

    Each mint embeds a fresh ``jti`` (JWT ID, RFC 7519 §4.1.7) so two
    tokens minted in the same second still differ byte-for-byte — without
    this, refresh-token rotation would emit identical tokens on fast
    paths and the replay-detection store check would be a no-op.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "typ": typ,
        "jti": secrets.token_urlsafe(12),
    }
    if session_id is not None:
        payload["sid"] = session_id
    if extra:
        payload["extra"] = extra

    payload_b64 = _b64u_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).encode()
    signing_input = _HEADER_BYTES + b"." + payload_b64
    sig = _sign(secret, signing_input)
    return f"{_HEADER_BYTES.decode()}.{payload_b64.decode()}.{sig}"


def verify(*, secret: str, token: str, expected_typ: str) -> Claims:
    """Verify signature, expiry, and ``typ``. Raise ``TokenError`` on any fail.

    ``expected_typ`` MUST match the token's ``typ`` claim — this prevents an
    access token from being accepted where a refresh is required (and vice
    versa). It also closes the "use my long-lived refresh as an access" hole.
    """
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc

    # Lock the header to our HS256 algorithm to defeat alg-confusion attacks.
    # We compare bytes directly — never trust a token's self-declared alg.
    if header_b64.encode() != _HEADER_BYTES:
        raise TokenError("unexpected header (algorithm-confusion guard)")

    signing_input = header_b64.encode() + b"." + payload_b64.encode()
    expected_sig = _sign(secret, signing_input)
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise TokenError("bad signature")

    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise TokenError("malformed payload") from exc

    now_ts = datetime.now(timezone.utc).timestamp()
    exp = payload.get("exp")
    iat = payload.get("iat")
    sub = payload.get("sub")
    typ = payload.get("typ")
    sid = payload.get("sid")
    extra = payload.get("extra")

    if not isinstance(exp, int) or not isinstance(iat, int) or not isinstance(sub, str):
        raise TokenError("missing required claim")
    if typ != expected_typ:
        raise TokenError(f"wrong token type (got {typ!r}, expected {expected_typ!r})")
    if exp < now_ts:
        raise TokenError("token expired")

    return Claims(
        sub=sub,
        iat=datetime.fromtimestamp(iat, tz=timezone.utc),
        exp=datetime.fromtimestamp(exp, tz=timezone.utc),
        typ=typ,
        sid=sid if isinstance(sid, str) else None,
        extra=extra if isinstance(extra, dict) else None,
    )


# ─────────────────────────────────────────────────────────────────────
# Convenience wrappers — access vs refresh lifetimes per PLAN.md §3
# ─────────────────────────────────────────────────────────────────────

ACCESS_TOKEN_TTL: timedelta = timedelta(minutes=15)
REFRESH_TOKEN_TTL: timedelta = timedelta(days=30)


def mint_access(*, secret: str, user_id: str) -> str:
    """Short-lived access token. Sent on every authenticated request."""
    return mint(
        secret=secret,
        user_id=user_id,
        typ="access",
        lifetime=ACCESS_TOKEN_TTL,
    )


def mint_refresh(*, secret: str, user_id: str, session_id: str) -> str:
    """Long-lived refresh token. Tied to a ``user_sessions`` row id so
    revoking the session immediately invalidates the refresh — even
    though the JWT itself wouldn't have expired yet.
    """
    return mint(
        secret=secret,
        user_id=user_id,
        typ="refresh",
        lifetime=REFRESH_TOKEN_TTL,
        session_id=session_id,
    )


def verify_access(*, secret: str, token: str) -> Claims:
    return verify(secret=secret, token=token, expected_typ="access")


def verify_refresh(*, secret: str, token: str) -> Claims:
    return verify(secret=secret, token=token, expected_typ="refresh")


# ─────────────────────────────────────────────────────────────────────
# Opaque tokens (magic-link, refresh-token "secret" before hashing)
# ─────────────────────────────────────────────────────────────────────


def new_opaque_token(length_bytes: int = 32) -> str:
    """Return a high-entropy URL-safe token. Used for magic-link tokens
    (one-shot email login) and as the raw refresh secret before hashing.
    """
    return secrets.token_urlsafe(length_bytes)


def hash_token(token: str, *, salt: str = "") -> str:
    """Hash an opaque token for at-rest storage.

    We use scrypt (stdlib ``hashlib.scrypt``) for two reasons:
      1. It's in the standard library — no dependency on passlib/bcrypt yet.
      2. scrypt is memory-hard; even a stolen DB doesn't enable cheap
         brute-force.

    Salt is optional + per-record so a salt leak doesn't enable rainbow tables.
    """
    if not salt:
        salt = secrets.token_urlsafe(16)
    digest = hashlib.scrypt(
        token.encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=2**14,  # CPU/memory cost — bump in prod if you need.
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${salt}${base64.urlsafe_b64encode(digest).rstrip(b'=').decode()}"


def verify_token_hash(token: str, *, stored: str) -> bool:
    """Constant-time compare. Returns False on any malformed stored hash."""
    try:
        scheme, salt, _ = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    return hmac.compare_digest(hash_token(token, salt=salt), stored)
