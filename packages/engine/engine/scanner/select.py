"""Env-driven Scanner construction — mirrors ``feature_provider_from_env``.

Every knob is read here, once, so the scheduler stays a scheduler and the
policy numbers live next to the code that enforces them.

Environment:

    SCANNER_BAR_MINUTES        Intraday bar size. Default 15 — the free IEX
                               plan embargoes the last ~15 minutes, so a
                               finer bar polls for data that does not exist.
    SCANNER_COOLDOWN_MINUTES   Per-(symbol, rule) debounce. Default 240.
    SCANNER_VOLUME_SPIKE_MULT  Session volume ÷ 20-day average. Default 2.0.
    SCANNER_ATR_EXPANSION_MULT True range ÷ ATR-14. Default 1.5.
    SCANNER_GAP_PCT            Open-vs-prior-close gap, percent. Default 2.0.
    SCANNER_ZSCORE_THRESHOLD   |price z-score| extreme. Default 2.0.
    SCANNER_DONCHIAN_BAND_PCT  Channel decile that counts as "approaching
                               the edge". Default 10.0.

Returns None when Alpaca data keys are absent, so MOCK-mode development and
the test suite never need a network or a key — the caller treats None as
"scanning unavailable" exactly as it treats a missing feature provider.
"""

from __future__ import annotations

import logging
import os

from engine.features.bars import AlpacaDailyBarsProvider, intraday_provider_from_env
from engine.scanner.cooldown import DEFAULT_COOLDOWN_MINUTES, TriggerCooldown
from engine.scanner.engine import Scanner
from engine.scanner.types import ScannerConfig

logger = logging.getLogger("engine.scanner.select")


def _env_float(name: str, default: float) -> float:
    """Read a float knob, falling back loudly on anything unparseable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("scanner: %s=%r is not a number — using %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def scanner_config_from_env() -> ScannerConfig:
    """Thresholds from the environment, defaults from ``ScannerConfig``."""
    base = ScannerConfig()
    return ScannerConfig(
        bar_minutes=_env_int("SCANNER_BAR_MINUTES", base.bar_minutes),
        volume_spike_mult=_env_float("SCANNER_VOLUME_SPIKE_MULT", base.volume_spike_mult),
        atr_expansion_mult=_env_float("SCANNER_ATR_EXPANSION_MULT", base.atr_expansion_mult),
        gap_pct=_env_float("SCANNER_GAP_PCT", base.gap_pct),
        zscore_threshold=_env_float("SCANNER_ZSCORE_THRESHOLD", base.zscore_threshold),
        donchian_approach_band_pct=_env_float(
            "SCANNER_DONCHIAN_BAND_PCT", base.donchian_approach_band_pct
        ),
    )


def scanner_from_env() -> Scanner | None:
    """A wired ``Scanner``, or None when Alpaca data keys are missing."""
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    intraday = intraday_provider_from_env()
    if not api_key or not secret or intraday is None:
        logger.info("scanner: ALPACA_API_KEY/SECRET not set — scanner unavailable")
        return None
    return Scanner(
        daily_bars=AlpacaDailyBarsProvider(api_key, secret),
        intraday=intraday,
        config=scanner_config_from_env(),
        cooldown=TriggerCooldown(
            _env_int("SCANNER_COOLDOWN_MINUTES", DEFAULT_COOLDOWN_MINUTES)
        ),
    )
