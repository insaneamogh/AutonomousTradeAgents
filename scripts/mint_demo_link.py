"""Mint a read-only demo-session link for judges/reviewers.

See docs/IMPL_DEMO_SESSION.md. Generates a long-lived, signed "demo" token
offline — no running API needed, just the same config the deployed API
resolves (``JWT_SECRET``, ``DEMO_USER_ID``/``AGENT_CRON_USER_ID``,
``DEMO_TOKEN_TTL_DAYS``), read the same way the API reads it (``.env`` in
the current directory, or the process environment) — and prints the full
link to stdout.

The token is NEVER written to the repo. Copy it from stdout and hand it
out directly (chat, a private doc, the submission write-up) — never a
commit, an issue, or a public gist.

Usage:
    uv run python scripts/mint_demo_link.py --base-url https://your-app.example.com

The minted token itself does not depend on ``DEMO_SESSION_ENABLED`` being
set in THIS shell — that flag is checked by the DEPLOYED server at
exchange time (``POST /auth/demo``), not by this script. It only warns
here as a sanity check, since a common mistake is minting locally without
ever having turned the flag on anywhere.
"""

from __future__ import annotations

import argparse
import sys

from app.core.config import Settings, get_settings
from app.services.auth.demo_session import (
    DemoSessionDisabled,
    demo_session_enabled,
    demo_token_ttl,
    demo_user_id,
    mint_demo_link_token,
)


def cli() -> int:
    parser = argparse.ArgumentParser(
        description="Mint a read-only demo-session link (docs/IMPL_DEMO_SESSION.md)."
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="The deployed app's base URL, e.g. https://your-app.up.railway.app",
    )
    args = parser.parse_args()

    if not demo_session_enabled():
        print(
            "note: DEMO_SESSION_ENABLED is not truthy in THIS shell's environment. "
            "That's fine if it's set on the DEPLOYED server instead (e.g. a Railway "
            "variable) — this script only mints the token, it never calls the server. "
            "But if DEMO_SESSION_ENABLED=1 isn't set there either, POST /auth/demo "
            "will 503 no matter how valid this token is.",
            file=sys.stderr,
        )

    settings = get_settings()
    default_secret = Settings.model_fields["jwt_secret"].default
    if settings.jwt_secret == default_secret:
        print(
            "warning: JWT_SECRET is the local-dev default in THIS shell. The minted "
            "token is signed with it, so it will only verify against a server "
            "configured with the SAME secret. Export the deployed server's real "
            "JWT_SECRET before running this if you want a link that actually works "
            "against it.",
            file=sys.stderr,
        )

    try:
        token = mint_demo_link_token(secret=settings.jwt_secret)
    except DemoSessionDisabled as exc:
        print(f"refusing to mint: {exc}", file=sys.stderr)
        return 2

    print(f"demo user id : {demo_user_id()}", file=sys.stderr)
    print(f"link TTL     : {demo_token_ttl().days} days", file=sys.stderr)
    print("link (copy this — it is not saved anywhere):", file=sys.stderr)
    print(f"{args.base_url.rstrip('/')}/?demo={token}")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
