"""The continuous scanner — turn bars into snapshots, snapshots into signals.

This is the orchestration layer. It owns three things and nothing else:

  1. **When not to run.** Market-hours gate before any request goes out.
     Scanning a closed tape re-reads yesterday and can only produce stale
     triggers.
  2. **How to build a snapshot.** Settled daily levels (which do not move
     intraday) joined to today's live tape. This split is the whole reason
     the design is cheap: the expensive-to-compute half is computed once
     per day and cached; only the cheap half is re-fetched every scan.
  3. **What survives the cooldown.** Signals go through the debounce before
     anyone hears about them.

It does NOT decide what to trade, size anything, or talk to an LLM. It
answers one question — "which symbols are worth waking the council for
right now" — and hands over named, auditable reasons.

**Cost shape.** One batched market-data request per scan regardless of
watchlist size, plus one cached daily-bars request per symbol per day. At a
5-minute cadence over a 6.5-hour session that is ~78 batched requests and
~15 cached ones per day: free-tier territory, and flat in the number of
symbols.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from engine.features.bars import BarsProvider, IntradayBar, IntradayBarsProvider
from engine.features.clock import ClockProvider
from engine.features.market_calendar import (
    is_us_market_open,
    us_market_session_bounds,
)
from engine.features.quant import relative_strength_ranks
from engine.features.technicals import DailyBar, atr_wilder, rsi_wilder, sma
from engine.scanner.cooldown import TriggerCooldown
from engine.scanner.triggers import evaluate_triggers
from engine.scanner.types import (
    ScannerConfig,
    ScanResult,
    ScanSignal,
    SymbolSnapshot,
)

logger = logging.getLogger("engine.scanner")

#: Daily history pulled per symbol. 320 calendar days ≈ 220 trading bars —
#: enough for the 200-day SMA that the regime-line trigger needs.
DAILY_LOOKBACK_DAYS = 320

#: Cross-sectional relative-strength window, in trading days.
RS_WINDOW = 63


@dataclass
class Scanner:
    """Deterministic watchlist scanner. No LLM anywhere in this class."""

    daily_bars: BarsProvider
    intraday: IntradayBarsProvider
    clock: ClockProvider | None = None
    """Optional real market clock — in production, ``resolved_clock_from_env()``
    (Alpaca's own CLI, when ``USE_ALPACA_CLI=1``, layered in front of
    ``AlpacaClock`` via ``clock_from_env()``; either way, any ``ClockProvider``
    works here, including a bare ``AlpacaClock`` in tests).

    When present, ``scan()`` asks it whether the market is open instead of
    consulting the local holiday calendar — the same real ``/v2/clock`` that
    decides whether Alpaca will accept an order, so it also catches an
    unscheduled early close the local table cannot know about. ``None``
    (the default) preserves the exact previous behaviour: the local
    calendar decides, unconditionally."""
    config: ScannerConfig = field(default_factory=ScannerConfig)
    cooldown: TriggerCooldown = field(default_factory=TriggerCooldown)

    async def scan(
        self,
        symbols: list[str],
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> ScanResult:
        """One scan pass over ``symbols``.

        ``force`` bypasses the market-hours gate. It exists for demos and
        for operator-driven rescans, and it is the only way to get a signal
        out of this class outside the session — deliberately explicit,
        because a forced scan reads a stale tape and the caller has to have
        decided that is acceptable.
        """
        at = (now or datetime.now(UTC)).astimezone(UTC)
        syms = [s.strip().upper() for s in symbols if s.strip()]

        market_open, market_open_source = await self._market_open(at)

        if not force and not market_open:
            logger.debug(
                "scanner: market closed at %s (source=%s) — no scan",
                at.isoformat(), market_open_source,
            )
            return ScanResult(
                scanned_at=at,
                market_open=False,
                symbols_scanned=(),
                signals=(),
                suppressed=(),
                market_open_source=market_open_source,
            )
        if not syms:
            return ScanResult(
                scanned_at=at,
                market_open=True,
                symbols_scanned=(),
                signals=(),
                suppressed=(),
                market_open_source=market_open_source,
            )

        errors: dict[str, str] = {}
        daily = await self._load_daily(syms, errors)
        intraday = await self._load_intraday(syms, at, errors)

        signals: list[ScanSignal] = []
        returns: dict[str, float | None] = {}
        for sym in syms:
            bars = daily.get(sym) or []
            if len(bars) < self.config.min_daily_bars:
                errors.setdefault(
                    sym, f"only {len(bars)} settled daily bars (need {self.config.min_daily_bars})"
                )
                continue
            returns[sym] = _trailing_return_pct(bars, RS_WINDOW)
            snap = build_snapshot(sym, bars, intraday.get(sym) or [], observed_at=at)
            if snap is None:
                continue
            signals.extend(evaluate_triggers(snap, self.config))

        allowed, suppressed = self.cooldown.filter(signals, at)
        if allowed:
            logger.info(
                "scanner: %d trigger(s) on %d symbol(s) — %s",
                len(allowed),
                len({s.symbol for s in allowed}),
                ", ".join(f"{s.symbol}:{s.trigger_rule}" for s in allowed),
            )
        return ScanResult(
            scanned_at=at,
            market_open=True,
            symbols_scanned=tuple(syms),
            signals=tuple(allowed),
            suppressed=tuple(suppressed),
            market_open_source=market_open_source,
            relative_strength=relative_strength_ranks(returns),
            errors=errors,
        )

    async def _market_open(self, at: datetime) -> tuple[bool, str]:
        """Whether the market is open at ``at``, and which source answered.

        Delegates to the injected ``clock`` when one is configured — real
        session state from Alpaca's own ``/v2/clock``, the same clock that
        will decide whether an order is accepted. Falls back to the local
        holiday-table calendar when no clock is present, exactly as before
        this method existed. Never raises: ``AlpacaClock.now()`` already
        swallows its own failures and falls back internally, reporting
        ``source="local_calendar"`` when it does.
        """
        if self.clock is not None:
            clock = await self.clock.now(at=at)
            return clock.is_open, clock.source
        return is_us_market_open(at), "local_calendar"

    async def _load_daily(
        self, symbols: list[str], errors: dict[str, str]
    ) -> dict[str, list[DailyBar]]:
        """Settled daily bars per symbol. Provider-cached per UTC day.

        Sequential, not gathered: these are cache hits after the first scan
        of the day, and a 15-way parallel burst on a cold cache is the one
        thing that would trip the free tier's rate limit.
        """
        out: dict[str, list[DailyBar]] = {}
        for sym in symbols:
            try:
                out[sym] = await self.daily_bars.daily_bars(
                    sym, lookback_days=DAILY_LOOKBACK_DAYS
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("scanner: daily bars failed for %s — %s", sym, exc)
                errors[sym] = f"daily bars unavailable: {type(exc).__name__}"
                out[sym] = []
        return out

    async def _load_intraday(
        self, symbols: list[str], at: datetime, errors: dict[str, str]
    ) -> dict[str, list[IntradayBar]]:
        """Today's tape for the whole watchlist in one batched request.

        A failure here is total — there is no live price for anyone, so the
        pass produces no signals rather than half a scan.
        """
        bounds = us_market_session_bounds(at.date())
        session_start = bounds[0] if bounds else None
        try:
            return await self.intraday.intraday_bars(
                symbols,
                bar_minutes=self.config.bar_minutes,
                session_start=session_start,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("scanner: intraday fetch failed — %s", exc)
            for sym in symbols:
                errors[sym] = f"intraday bars unavailable: {type(exc).__name__}"
            return {}


def build_snapshot(
    symbol: str,
    daily: list[DailyBar],
    intraday: list[IntradayBar],
    *,
    observed_at: datetime,
) -> SymbolSnapshot | None:
    """Join settled daily levels to today's live tape.

    Pure and provider-agnostic so the trigger layer can be tested end to
    end without a network. Returns None only when there is no usable
    settled history at all.

    Two guards worth naming:

      - **Today's daily bar is dropped** if a provider ever returns one.
        A half-formed daily candle would contaminate every level the
        crosses compare against, and then the "cross" would be price
        crossing a line drawn from itself.
      - **No intraday prints → last_price falls back to the prior close.**
        The snapshot still carries the settled levels (useful to a caller
        that wants them) but ``has_intraday`` is False, and
        ``evaluate_triggers`` refuses to fire on it.
    """
    settled = _settled_bars(daily, observed_at.date())
    if not settled:
        return None

    closes = [b.close for b in settled]
    prior_close = closes[-1]

    last_price = intraday[-1].close if intraday else prior_close
    session_open = intraday[0].open if intraday else None
    session_high = max(b.high for b in intraday) if intraday else None
    session_low = min(b.low for b in intraday) if intraday else None
    session_volume = sum(b.volume for b in intraday)

    recent_20 = closes[-20:]
    mean_20 = sum(recent_20) / len(recent_20) if len(recent_20) == 20 else None
    std_20 = _stdev(recent_20) if len(recent_20) == 20 else None

    vols = [b.volume for b in settled[-20:]]
    avg_volume = sum(vols) / len(vols) if len(vols) == 20 and sum(vols) > 0 else None

    return SymbolSnapshot(
        symbol=symbol.upper(),
        observed_at=observed_at,
        last_price=last_price,
        session_open=session_open,
        session_high=session_high,
        session_low=session_low,
        session_volume=session_volume,
        intraday_bars=len(intraday),
        prior_close=prior_close,
        sma20=sma(closes, 20),
        sma50=sma(closes, 50),
        sma200=sma(closes, 200),
        rsi_prior=rsi_wilder(closes, 14),
        # The live RSI is the same Wilder recursion with today's price
        # appended as if it were the close. That is what makes an intraday
        # band exit a real transition rather than a restatement.
        rsi_live=rsi_wilder([*closes, last_price], 14),
        atr_14=atr_wilder(settled, 14),
        avg_volume_20d=avg_volume,
        donchian_high_20=max(b.high for b in settled[-20:]) if len(settled) >= 20 else None,
        donchian_low_20=min(b.low for b in settled[-20:]) if len(settled) >= 20 else None,
        donchian_low_10=min(b.low for b in settled[-10:]) if len(settled) >= 10 else None,
        close_mean_20=mean_20,
        close_std_20=std_20,
    )


def _settled_bars(daily: list[DailyBar], today: date) -> list[DailyBar]:
    """Daily bars strictly before ``today``, oldest → newest."""
    return sorted((b for b in daily if b.day < today), key=lambda b: b.day)


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mu = sum(values) / len(values)
    var = sum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return var**0.5 if var > 0 else 0.0


def _trailing_return_pct(bars: list[DailyBar], window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    base = bars[-(window + 1)].close
    if base <= 0:
        return None
    return (bars[-1].close / base - 1.0) * 100.0
