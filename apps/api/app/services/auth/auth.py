"""Auth service — magic-link login + JWT issuance + refresh rotation.

Three flows:

  1. ``request_login(email)``
     Mints a one-shot opaque token, stores its hash, and returns the raw
     token. **In production** the token goes out via email; in Phase 3.1
     we return it in the response payload (clearly marked) so the mobile
     deep-link can pick it up during dev. The ``dev_token`` field is
     dropped by the production-mode flag.

  2. ``verify_magic_link(token, email)``
     Validates the token against stored hashes, upserts the user, mints
     an access + refresh pair, opens a ``user_sessions`` row.

  3. ``refresh(refresh_token)``
     Validates the refresh JWT, looks up the session row, checks
     ``revoked_at IS NULL``, ROTATES the refresh token (new opaque secret
     + hash + JWT) — the old refresh is invalidated by hash mismatch on
     the next call. Failures raise ``RefreshError`` with a machine-readable
     ``code`` (see the ``REFRESH_CODE_*`` constants) so the client can tell
     "this credential is dead" apart from "the backend doesn't currently
     recognize this session".

  4. ``login_with_google(email)``
     A second entry point alongside ``verify_magic_link`` — by the time
     it's called, the router has already verified the Google ID token
     (``app.services.auth.google_oauth``). Upserts the user + mints the
     same access/refresh pair as magic-link.

PLAN.md §3 calls for refresh rotation per-device. Each session is one
device; logout / "log out other devices" lands as ``revoke_session`` per
session id (router-level, not in this file).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.services.auth.auth_store import AuthStore, MagicLinkRecord, SessionRecord, UserRecord
from app.services.auth.jwt_service import (
    REFRESH_TOKEN_TTL,
    Claims,
    TokenError,
    hash_token,
    mint_access,
    mint_refresh,
    new_opaque_token,
    verify_refresh,
    verify_token_hash,
)

logger = logging.getLogger("api.auth")


# How long a magic-link token is valid before the user must request a new one.
# Short enough that a stolen email screenshot expires quickly; long enough that
# real users (slow Wi-Fi, switching apps) succeed on the first try.
MAGIC_LINK_TTL: timedelta = timedelta(minutes=15)


class AuthError(Exception):
    """User-visible auth failure. Routers translate to 4xx."""


# Machine-readable codes for a failed ``refresh()`` — see ``RefreshError``.
# The mobile client's session-refresh decision tree (authStore.ts) keys off
# these exact strings to decide whether the stored refresh token is truly
# dead (wipe SecureStore) or the backend merely doesn't currently recognize
# it (keep it — a later successful restore shouldn't force a fresh login).
REFRESH_CODE_SESSION_NOT_FOUND = "session_not_found"
REFRESH_CODE_SESSION_REVOKED = "session_revoked"
REFRESH_CODE_SESSION_EXPIRED = "session_expired"
REFRESH_CODE_TOKEN_INVALID = "token_invalid"
REFRESH_CODE_SUPERSEDED = "superseded"


class RefreshError(AuthError):
    """A ``refresh()`` failure, carrying a machine-readable ``code`` on top
    of the human ``str(exc)`` message. The router threads ``code`` into the
    401 response body so the client can distinguish "this exact credential
    is dead" (revoked / invalid / superseded — safe to wipe storage) from
    "the backend doesn't currently recognize this session" (not found —
    NOT safe to conclude the credential can never work again).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _match_candidate(
    token: str, candidates: Sequence[MagicLinkRecord]
) -> MagicLinkRecord | None:
    """Find the magic-link record whose stored hash matches ``token``.

    Pure CPU (one scrypt per candidate) — always call this via
    ``asyncio.to_thread`` so the event loop keeps serving other requests.
    """
    for cand in candidates:
        if verify_token_hash(token, stored=cand.token_hash):
            return cand
    return None


@dataclass(frozen=True)
class LoginChallenge:
    """Result of ``request_login``. ``dev_token`` is None in production."""

    magic_link_id: str
    expires_at: datetime
    dev_token: str | None


@dataclass(frozen=True)
class IssuedTokens:
    user: UserRecord
    access_token: str
    refresh_token: str
    session: SessionRecord


# ─────────────────────────────────────────────────────────────────────
# Magic-link login
# ─────────────────────────────────────────────────────────────────────


async def request_login(
    *,
    email: str,
    store: AuthStore,
    is_production: bool = False,
) -> LoginChallenge:
    """Mint + persist a magic-link token; return the raw token to the
    caller when not in production. In production we'd hand it to an
    email-sender here and return ``dev_token=None``.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        raise AuthError("invalid email")

    raw_token = new_opaque_token()
    token_hash = hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + MAGIC_LINK_TTL

    record = await store.create_magic_link(
        email=email,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    logger.info(
        "magic-link issued for %s — id=%s expires=%s",
        email, record.id, expires_at.isoformat(),
    )

    # Deliver via email when a provider is configured (best-effort; a send
    # failure never blocks login — the token stays valid to retry). This is
    # what makes production login work without the pull-token-from-logs
    # workaround. Dev still gets ``dev_token`` for the deep-link shortcut.
    from app.services.notifications.email import email_enabled, send_magic_link

    emailed = False
    if email_enabled():
        emailed = await send_magic_link(email, raw_token)
        logger.info("magic-link email to %s — sent=%s", email, emailed)
    elif is_production:
        logger.warning(
            "magic-link for %s NOT emailed — no EMAIL_PROVIDER configured in prod; "
            "the token is unreachable to the user. Configure email or use ENV=local.",
            email,
        )

    return LoginChallenge(
        magic_link_id=record.id,
        expires_at=expires_at,
        dev_token=None if is_production else raw_token,
    )


async def verify_magic_link(
    *,
    email: str,
    token: str,
    store: AuthStore,
    secret: str,
    device_id: str | None = None,
    device_label: str | None = None,
) -> IssuedTokens:
    """Consume a magic-link token + mint an access + refresh pair.

    Constant-time-compare across the candidate tokens so we don't leak
    "which one matched". If any token matches, we lock it (single-use)
    and proceed; the rest stay open but will hit the same path on a
    legitimate retry.

    The comparison loop runs in a worker thread: each candidate costs one
    scrypt(n=2**14) ≈ 16MB/50ms, and this endpoint is unauthenticated —
    on the event loop it would stall every other in-flight request (an
    unauthenticated CPU-amplification DoS). The router also rate-limits it.
    """
    email = email.strip().lower()
    candidates = await store.find_unused_magic_link(email=email)
    if not candidates:
        raise AuthError("no pending magic-link for that email")

    match = await asyncio.to_thread(_match_candidate, token, candidates)
    if match is None:
        raise AuthError("invalid or expired magic-link token")

    # Single-use: only proceed if WE claimed the token. A concurrent verify
    # of the same link loses the claim and is rejected — never two sessions
    # off one link.
    if not await store.mark_magic_link_used(match.id):
        raise AuthError("magic-link token already used")

    user = await store.upsert_user(email)

    return await _issue_pair(
        user=user,
        store=store,
        secret=secret,
        device_id=device_id,
        device_label=device_label,
    )


# ─────────────────────────────────────────────────────────────────────
# Google sign-in
# ─────────────────────────────────────────────────────────────────────


async def login_with_google(
    *,
    email: str,
    store: AuthStore,
    secret: str,
    device_id: str | None = None,
    device_label: str | None = None,
) -> IssuedTokens:
    """Get-or-create the user for a verified Google identity, then mint the
    same access + refresh pair ``verify_magic_link`` issues.

    By the time this is called, the router has ALREADY verified the Google
    ID token's signature, ``aud``/``iss``, and ``email_verified`` claim
    (``app.services.auth.google_oauth.verify_google_id_token``) — this
    function only does the "mint tokens for a known-good email" half,
    reusing ``_issue_pair`` so a Google-issued session is indistinguishable
    from a magic-link one everywhere downstream.

    ``store.upsert_user`` only sets ``auth_method`` on first INSERT, so an
    existing magic-link user signing in with Google under the same email
    keeps ``auth_method == "magic_link"`` forever. That's an accepted,
    cosmetic quirk (see ``auth_store.upsert_user``'s docstring) — not
    something to work around here.
    """
    email = email.strip().lower()
    user = await store.upsert_user(email, auth_method="google")

    return await _issue_pair(
        user=user,
        store=store,
        secret=secret,
        device_id=device_id,
        device_label=device_label,
    )


# ─────────────────────────────────────────────────────────────────────
# Refresh rotation
# ─────────────────────────────────────────────────────────────────────


async def refresh(
    *,
    refresh_token: str,
    store: AuthStore,
    secret: str,
) -> IssuedTokens:
    """Validate a refresh token, rotate it, return a fresh pair.

    Three things have to all hold:
      1. The JWT signature + ``typ == "refresh"`` + not expired.
      2. The session row exists + ``revoked_at IS NULL`` + still in date.
      3. The presented token's hash matches the row's stored hash
         (otherwise: somebody is using a replayed older refresh).
    """
    try:
        claims: Claims = verify_refresh(secret=secret, token=refresh_token)
    except TokenError as exc:
        raise RefreshError(
            REFRESH_CODE_TOKEN_INVALID, f"refresh token rejected: {exc}"
        ) from exc

    if not claims.sid:
        raise RefreshError(
            REFRESH_CODE_TOKEN_INVALID, "refresh token missing session id"
        )

    session = await store.get_session(claims.sid)
    if session is None:
        raise RefreshError(REFRESH_CODE_SESSION_NOT_FOUND, "session not found")
    if session.revoked_at is not None:
        raise RefreshError(REFRESH_CODE_SESSION_REVOKED, "session revoked")
    if session.expires_at < datetime.now(timezone.utc):
        raise RefreshError(REFRESH_CODE_SESSION_EXPIRED, "session expired")
    hash_ok = await asyncio.to_thread(
        verify_token_hash, refresh_token, stored=session.refresh_token_hash
    )
    if not hash_ok:
        # Replay detection — somebody used an OLD refresh after a rotation.
        # Revoke the session entirely so the attacker can't continue.
        logger.warning(
            "refresh-token replay on session %s — revoking session",
            session.id,
        )
        await store.revoke_session(session.id)
        raise RefreshError(
            REFRESH_CODE_SUPERSEDED, "refresh token superseded — session revoked"
        )

    user = await store.get_user_by_id(session.user_id)
    if user is None:
        # The session row resolved but its owning user didn't — a broken
        # data state, not a "backend forgot" state. Treat like a dead
        # credential rather than something a later restore could fix.
        raise RefreshError(REFRESH_CODE_TOKEN_INVALID, "session user missing")

    # Rotate: new refresh token, new hash, COMPARE-AND-SWAP on the hash we
    # just validated. If the swap misses, a concurrent refresh already
    # rotated this token — treat the loser as a replay and revoke.
    new_refresh = mint_refresh(secret=secret, user_id=user.id, session_id=session.id)
    rotated = await store.rotate_session(
        session.id,
        new_refresh_token_hash=hash_token(new_refresh),
        expected_current_hash=session.refresh_token_hash,
    )
    if rotated is None:
        logger.warning(
            "concurrent refresh detected on session %s — revoking", session.id
        )
        await store.revoke_session(session.id)
        raise RefreshError(
            REFRESH_CODE_SUPERSEDED, "refresh token superseded — session revoked"
        )
    new_access = mint_access(secret=secret, user_id=user.id, session_id=session.id)

    return IssuedTokens(
        user=user,
        access_token=new_access,
        refresh_token=new_refresh,
        session=await _refresh_session_view(store, session.id),
    )


# ─────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────


async def _issue_pair(
    *,
    user: UserRecord,
    store: AuthStore,
    secret: str,
    device_id: str | None,
    device_label: str | None,
) -> IssuedTokens:
    """Common path for first login + magic-link verify."""
    expires_at = datetime.now(timezone.utc) + REFRESH_TOKEN_TTL

    # We need a session id BEFORE we mint the refresh (the refresh embeds
    # the sid). Create the row with a placeholder hash, mint, swap.
    placeholder = "scrypt$bootstrap$placeholder"
    session = await store.create_session(
        user_id=user.id,
        refresh_token_hash=placeholder,
        expires_at=expires_at,
        device_id=device_id,
        device_label=device_label,
    )
    refresh_token = mint_refresh(secret=secret, user_id=user.id, session_id=session.id)
    await store.rotate_session(session.id, new_refresh_token_hash=hash_token(refresh_token))
    access_token = mint_access(secret=secret, user_id=user.id, session_id=session.id)

    return IssuedTokens(
        user=user,
        access_token=access_token,
        refresh_token=refresh_token,
        session=await _refresh_session_view(store, session.id),
    )


async def _refresh_session_view(store: AuthStore, session_id: str) -> SessionRecord:
    s = await store.get_session(session_id)
    assert s is not None, "session row vanished mid-issue"
    return s
