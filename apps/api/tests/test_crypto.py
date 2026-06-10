"""Crypto helper tests.

The whole module is gated on ``cryptography`` being installed. When it's
not (the current default — declared but un-synced), the entire suite is
skipped with a clear marker so a future ``uv sync`` makes them visible
without code changes.
"""

from __future__ import annotations

import pytest

from app.services import crypto

if not crypto.is_available():
    pytest.skip(
        "cryptography not installed — run `uv sync` to enable these tests",
        allow_module_level=True,
    )


def test_roundtrip_recovers_plaintext() -> None:
    plaintext = "sk-alpaca-paper-XXXXXXXXXXXXXXXX"
    ct = crypto.encrypt_for_storage(plaintext)
    assert ct != plaintext
    assert crypto.decrypt_from_storage(ct) == plaintext


def test_two_encrypts_of_same_input_produce_different_ciphertexts() -> None:
    """Fernet uses a fresh IV per call — same input must NOT produce the same
    ciphertext. Otherwise an attacker could fingerprint token reuse via the
    encrypted column.
    """
    plaintext = "sk-alpaca-paper-XYZ"
    a = crypto.encrypt_for_storage(plaintext)
    b = crypto.encrypt_for_storage(plaintext)
    assert a != b
    assert crypto.decrypt_from_storage(a) == plaintext
    assert crypto.decrypt_from_storage(b) == plaintext


def test_tampered_ciphertext_raises() -> None:
    """Fernet's HMAC catches in-place edits to the ciphertext."""
    from cryptography.fernet import InvalidToken

    ct = crypto.encrypt_for_storage("hello")
    # Flip a character somewhere safe in the body.
    tampered = ct[:-5] + ("X" if ct[-5] != "X" else "Y") + ct[-4:]
    with pytest.raises(InvalidToken):
        crypto.decrypt_from_storage(tampered)


def test_empty_inputs_rejected() -> None:
    with pytest.raises(ValueError):
        crypto.encrypt_for_storage("")
    with pytest.raises(ValueError):
        crypto.decrypt_from_storage("")


def test_dev_key_flag_surfaces() -> None:
    """The /broker/* start route surfaces a warning when the dev fallback
    is in play. Verify the helper this route uses returns True by default.
    """
    assert crypto.is_dev_key_in_use() is True
