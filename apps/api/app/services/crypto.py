"""Symmetric encryption for at-rest broker tokens.

Backed by ``cryptography.fernet`` (AES-128 in CBC + HMAC-SHA256 + URL-safe
base64). PLAN.md §3 + apps/api/AUTH.md require broker refresh tokens to be
encrypted at rest; this is the helper every write to
``broker_connections.encrypted_*`` goes through.

Why we don't roll this ourselves:
  - Unlike HMAC-SHA256 (which is one line on stdlib primitives), symmetric
    AEAD encryption involves block ciphers, padding, IV/nonce hygiene, and
    constant-time integrity checks. Rolling it is a real foot-gun.
  - ``cryptography`` is listed in ``apps/api/pyproject.toml`` (via
    ``python-jose[cryptography]``). The user hasn't ``uv sync``'d yet —
    when they do, this module light up.

Module behavior when ``cryptography`` is unavailable:
  - Importing this module does NOT fail (so the rest of the API still
    boots in MOCK paths that never touch broker tokens).
  - Calling ``encrypt_for_storage`` / ``decrypt_from_storage`` raises a
    clear ``CryptoUnavailableError`` so the failure mode is obvious and
    bounded to the OAuth path.
  - Tests check for the optional import and ``skipif`` accordingly.

Key management:
  - Master key comes from ``BROKER_TOKEN_ENCRYPTION_KEY`` (Doppler in prod,
    a known dev default locally — see ``DEV_KEY`` below).
  - Key must be 32 bytes URL-safe base64. Generate with
    ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
  - Key rotation: ``MultiFernet`` is supported via ``decryption_keys``
    env list. Phase 4 hardening adds a rotation runbook.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("api.crypto")


# Local-dev fallback key. **NEVER** ship this past dev — Doppler hands out
# a real key in any non-local env. The base64-decoded value is the literal
# ASCII string "DEV-ONLY-broker-token-key-32byts" — intentionally obvious
# so a grep catches accidental misuse.
DEV_KEY: str = "REVWLU9OTFktYnJva2VyLXRva2VuLWtleS0zMmJ5dHM="

try:
    from cryptography.fernet import Fernet, MultiFernet  # type: ignore[import-not-found]

    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover — declared but not yet uv-synced
    Fernet = None  # type: ignore[misc,assignment]
    MultiFernet = None  # type: ignore[misc,assignment]
    _CRYPTO_AVAILABLE = False
    logger.warning(
        "cryptography not installed — broker OAuth encryption is unavailable. "
        "Run `uv sync` to enable. Mock paths that don't touch broker tokens still work."
    )


class CryptoUnavailableError(RuntimeError):
    """Raised when ``encrypt_for_storage`` / ``decrypt_from_storage`` is
    called but ``cryptography`` isn't installed. Routers translate to a
    503 with a clear message.
    """


def is_available() -> bool:
    """True iff broker-token crypto works in this process. Use to short-
    circuit OAuth routes early so the user gets a clean 503 instead of an
    opaque ImportError trace.
    """
    return _CRYPTO_AVAILABLE


def _resolve_keys() -> tuple[str, list[str]]:
    """Return (primary_key, all_decryption_keys).

    Primary key is used for ENCRYPT operations. ``decryption_keys`` (comma-
    separated env) augments the decrypt set so a key-rotation cycle can
    serve old ciphertexts while new writes use the new key.
    """
    primary = os.environ.get("BROKER_TOKEN_ENCRYPTION_KEY", "").strip() or DEV_KEY
    rotated_csv = os.environ.get("BROKER_TOKEN_DECRYPTION_KEYS", "").strip()
    extras = [k.strip() for k in rotated_csv.split(",") if k.strip()]
    # Primary is always FIRST in the decrypt list so the MultiFernet tries
    # the active key first.
    return primary, [primary, *extras]


def _build_fernet() -> Any:
    """Return a Fernet (single key) or MultiFernet (rotation). Raises if
    crypto is unavailable.
    """
    if not _CRYPTO_AVAILABLE:
        raise CryptoUnavailableError(
            "cryptography is not installed — broker token encryption disabled. "
            "Run `uv sync` to enable."
        )
    _, all_keys = _resolve_keys()
    fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in all_keys]
    if len(fernets) == 1:
        return fernets[0]
    return MultiFernet(fernets)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def encrypt_for_storage(plaintext: str) -> str:
    """Encrypt a token for at-rest storage.

    Returns a string suitable for a TEXT column. The Fernet token already
    includes the IV + HMAC + ciphertext + scheme version, base64-encoded —
    we don't add a wrapper.
    """
    if not plaintext:
        raise ValueError("encrypt_for_storage: empty plaintext")
    fernet = _build_fernet()
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_from_storage(ciphertext: str) -> str:
    """Inverse of ``encrypt_for_storage``. Raises ``CryptoUnavailableError``
    if crypto isn't available, or ``cryptography.fernet.InvalidToken`` if
    the ciphertext is tampered / under a key we don't hold.
    """
    if not ciphertext:
        raise ValueError("decrypt_from_storage: empty ciphertext")
    fernet = _build_fernet()
    return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")


def is_dev_key_in_use() -> bool:
    """Tooling helper: surface a warning in /broker/* responses when the
    fallback dev key is in play. Production deploys MUST set
    ``BROKER_TOKEN_ENCRYPTION_KEY``.
    """
    primary, _ = _resolve_keys()
    return primary == DEV_KEY
