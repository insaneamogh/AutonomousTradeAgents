"""App configuration. Env-driven; sensible local-dev defaults.

The ``CORS_ORIGINS`` env var is a comma-separated allow-list (no surrounding
brackets — Pydantic Settings handles that). Two special-case values:

  - ``*``     : allow any origin. ONLY honored when ``ENV`` is not in the
                production set; production with `*` would silently disable
                `allow_credentials`, which we don't want to ship by accident.
  - empty     : a permissive default for local dev (``*`` semantics).

PORT is read at process start (Railway / Fly inject it); we don't pin it
here so the deploy command stays standard ``uvicorn --port $PORT``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from engine.env import env_flag

_PRODUCTION_ENVS: set[str] = {"prod", "production", "live"}


def _csv_to_list(v: object, *, empty: list[str]) -> object:
    """Split a comma-separated env string into a list; pass non-strings
    through untouched (pydantic-settings may already hand us a list).
    ``empty`` is the field-specific default for an unset/blank value —
    ``["*"]`` for CORS (permissive local dev), ``[]`` for Google client ids
    (unconfigured, not "allow everything").
    """
    if isinstance(v, str):
        if not v.strip():
            return list(empty)
        return [s.strip() for s in v.split(",") if s.strip()]
    return v


class Settings(BaseSettings):
    """Runtime config. Reads from env + a local ``.env`` if present."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = Field(default="local")
    log_level: str = Field(default="INFO")
    jwt_secret: str = Field(default="change-me-locally-32-bytes-min")

    # Council theater: artificial per-node pause applied ONLY in MOCK LLM
    # mode (runtime gates on llm.mock) so the progress feed is visible
    # instead of completing in one frame. Real LLM latency needs no pacing.
    theater_mock_pacing_seconds: float = Field(default=0.6)

    # NoDecode: stop pydantic-settings from JSON-decoding the env value
    # before our CSV validator runs — bare ``a,b`` is not valid JSON.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"],
        description=(
            "Comma-separated allow-list. '*' allowed only in non-production. "
            "Production deploys must list explicit origins."
        ),
    )

    # Google commonly issues a SEPARATE OAuth client id per platform (iOS,
    # Android, Web) for one logical app — the ID token's ``aud`` claim holds
    # whichever one the client used, so verification must accept ANY of
    # them. Empty (the default) means Google sign-in is unconfigured:
    # POST /api/v1/auth/google returns 503 — magic-link login is unaffected
    # either way, and this is deliberately NOT one of
    # ``production_config_problems()``'s hard-fail checks (see auth.google
    # router: magic-link alone must remain a valid production config).
    google_oauth_client_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Comma-separated Google OAuth client ids (iOS/Android/Web) whose "
            "ID tokens we accept. Empty = Google sign-in disabled (503)."
        ),
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept ``CORS_ORIGINS`` as comma-separated string from env."""
        return _csv_to_list(v, empty=["*"])

    @field_validator("google_oauth_client_ids", mode="before")
    @classmethod
    def _split_google_client_ids(cls, v: object) -> object:
        """Accept ``GOOGLE_OAUTH_CLIENT_IDS`` as comma-separated string."""
        return _csv_to_list(v, empty=[])

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in _PRODUCTION_ENVS

    @property
    def effective_cors_origins(self) -> list[str]:
        """In production, refuse the ``*`` wildcard — a deploy that ships
        with the default would unintentionally disable ``allow_credentials``.
        We coerce to an empty list so the operator gets a clear "no origins
        allowed" failure rather than a silent loosening of policy.
        """
        if self.is_production and self.cors_origins == ["*"]:
            return []
        return self.cors_origins


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# The insecure local defaults a production deploy must never ship with.
_DEFAULT_JWT_SECRET = "change-me-locally-32-bytes-min"


def production_config_problems(settings: Settings) -> list[str]:
    """Return a list of fatal misconfigurations for a production deploy.

    Empty list = safe to boot. Each entry is a human-readable problem. This
    is the single source of truth for "what MUST be set in prod" — a
    forgotten env var here is how a deploy silently ships with a known JWT
    signing key or an unencrypted-token dev key.
    """
    if not settings.is_production:
        return []

    problems: list[str] = []

    secret = (settings.jwt_secret or "").strip()
    if secret == _DEFAULT_JWT_SECRET or len(secret) < 32:
        problems.append(
            "JWT_SECRET is unset/default/too-short — set a random ≥32-char value "
            "(anyone with the default can forge tokens for any user)."
        )

    # Verify-only rotation keys (JWT_SECRET_PREVIOUS). A retired key that is
    # the known-default — or the same value as the current secret, which
    # means the rotation never actually happened — buys nothing and keeps a
    # forgeable key alive.
    previous = [
        s.strip()
        for s in os.environ.get("JWT_SECRET_PREVIOUS", "").split(",")
        if s.strip()
    ]
    if any(p in (_DEFAULT_JWT_SECRET, secret) for p in previous):
        problems.append(
            "JWT_SECRET_PREVIOUS contains the default secret or repeats "
            "JWT_SECRET — remove it (it is accepted for verification, so it "
            "keeps a forgeable key alive)."
        )

    # Broker OAuth tokens are Fernet-encrypted with this key; the dev fallback
    # is public in the repo, so real tokens would be trivially decryptable.
    if not os.environ.get("BROKER_TOKEN_ENCRYPTION_KEY", "").strip():
        problems.append(
            "BROKER_TOKEN_ENCRYPTION_KEY is unset — broker tokens would be "
            "encrypted with the public dev key. Set a real Fernet key."
        )

    if not settings.effective_cors_origins:
        problems.append(
            "CORS_ORIGINS is unset/wildcard — set an explicit comma-separated "
            "allow-list (every cross-origin request is denied otherwise)."
        )

    if env_flag("DEV_AUTH_BYPASS"):
        problems.append(
            "DEV_AUTH_BYPASS is truthy in production — it is force-disabled at "
            "runtime, but unset it to avoid confusion."
        )

    return problems


def require_production_readiness() -> None:
    """Fail fast at startup if a production deploy is missing critical secrets.
    No-op outside production. Called from the app lifespan."""
    settings = get_settings()
    problems = production_config_problems(settings)
    if problems:
        joined = "\n  - ".join(problems)
        raise RuntimeError(
            "Refusing to start in production with insecure configuration:\n  - "
            + joined
        )
