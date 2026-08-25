"""Transactional email — magic-link delivery.

Env-gated, provider-pluggable, and a hard no-op when unconfigured (same
posture as Langfuse/Sentry): missing config must never break login — it
falls back to the dev-token path in non-production.

Providers (pick via env):
  - Resend  : ``EMAIL_PROVIDER=resend`` + ``RESEND_API_KEY`` (HTTP, simplest).
  - SMTP    : ``EMAIL_PROVIDER=smtp`` + ``SMTP_HOST/PORT/USER/PASSWORD``.
Common: ``EMAIL_FROM`` (e.g. "Autonomous Trader <login@yourdomain.com>").

The magic link the email carries is the app deep link the mobile handler
already parses (``autotrader://auth/verify``); ``EMAIL_LINK_BASE`` overrides
it with a universal/https link once you have one.

Test seam: ``send_magic_link`` is the single entry point routers call; tests
monkeypatch it. The provider ``_transport`` functions are also injectable.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

logger = logging.getLogger("api.email")

_DEFAULT_LINK_BASE = "autotrader://auth/verify"


def email_enabled() -> bool:
    """True when a provider is configured well enough to send."""
    provider = os.environ.get("EMAIL_PROVIDER", "").strip().lower()
    if provider == "resend":
        return bool(os.environ.get("RESEND_API_KEY", "").strip())
    if provider == "smtp":
        return bool(os.environ.get("SMTP_HOST", "").strip())
    return False


def _magic_link_url(email: str, token: str) -> str:
    base = os.environ.get("EMAIL_LINK_BASE", "").strip() or _DEFAULT_LINK_BASE
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}email={quote(email)}&token={quote(token)}"


def _render(email: str, token: str) -> tuple[str, str, str]:
    """(subject, text, html) for the magic-link email."""
    url = _magic_link_url(email, token)
    subject = "Your Autonomous Trader sign-in link"
    text = (
        "Tap to sign in (expires in 15 minutes):\n\n"
        f"{url}\n\n"
        "If you didn't request this, ignore this email."
    )
    html = (
        f'<p>Tap to sign in (expires in 15 minutes):</p>'
        f'<p><a href="{url}">Sign in to Autonomous Trader</a></p>'
        f'<p style="color:#888;font-size:12px">If you didn\'t request this, ignore this email.</p>'
    )
    return subject, text, html


async def send_magic_link(email: str, token: str) -> bool:
    """Send the magic-link email. Returns True on a successful send, False if
    email isn't configured or the send failed (caller keeps the token valid
    either way — the user can retry). NEVER raises into the login path."""
    if not email_enabled():
        return False
    provider = os.environ.get("EMAIL_PROVIDER", "").strip().lower()
    subject, text, html = _render(email, token)
    sender = os.environ.get("EMAIL_FROM", "").strip() or "login@localhost"
    try:
        if provider == "resend":
            return await _send_resend(sender, email, subject, html, text)
        if provider == "smtp":
            return _send_smtp(sender, email, subject, html, text)
    except Exception:  # noqa: BLE001 — email failure must not break login
        logger.exception("email: send failed for %s via %s", email, provider)
        return False
    return False


async def _send_resend(
    sender: str, to: str, subject: str, html: str, text: str
) -> bool:
    import httpx

    api_key = os.environ["RESEND_API_KEY"].strip()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": sender, "to": [to], "subject": subject, "html": html, "text": text},
        )
    if resp.status_code >= 300:
        logger.warning("email: resend returned %s — %s", resp.status_code, resp.text[:200])
        return False
    return True


def _send_smtp(sender: str, to: str, subject: str, html: str, text: str) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.sendmail(sender, [to], msg.as_string())
    return True
