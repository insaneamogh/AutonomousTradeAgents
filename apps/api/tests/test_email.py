"""Magic-link email delivery — env-gating + the request-login wiring.

Verifies: disabled by default (no provider) → no-op + login still returns a
dev token in non-prod; when a provider IS configured, request_login calls
the sender with the raw token. The provider transport itself (Resend/SMTP)
is not hit — we monkeypatch the send seam.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.services.auth import auth as auth_service  # noqa: E402
from app.services.auth.auth_store import MockAuthStore  # noqa: E402
from app.services.notifications import email as email_mod  # noqa: E402


def test_email_disabled_without_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    assert email_mod.email_enabled() is False


def test_email_enabled_with_resend_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    assert email_mod.email_enabled() is True


def test_magic_link_url_carries_email_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMAIL_LINK_BASE", raising=False)
    url = email_mod._magic_link_url("a@b.com", "tok123")
    assert url.startswith("autotrader://auth/verify?")
    assert "email=a%40b.com" in url and "token=tok123" in url


async def test_request_login_sends_email_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, str]] = []

    async def fake_send(email: str, token: str) -> bool:
        captured.append((email, token))
        return True

    monkeypatch.setattr(email_mod, "email_enabled", lambda: True)
    monkeypatch.setattr("app.services.notifications.email.send_magic_link", fake_send)

    store = MockAuthStore()
    challenge = await auth_service.request_login(
        email="mailer@example.com", store=store, is_production=True
    )
    # Emailed with the raw token; prod response carries no dev token.
    assert captured and captured[0][0] == "mailer@example.com"
    assert challenge.dev_token is None


async def test_request_login_no_email_still_returns_dev_token_in_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(email_mod, "email_enabled", lambda: False)
    store = MockAuthStore()
    challenge = await auth_service.request_login(
        email="devuser@example.com", store=store, is_production=False
    )
    assert challenge.dev_token is not None  # dev deep-link shortcut preserved
