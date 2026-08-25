"""Scanner wire types — the boundary between "watch the tape" and "wake the council".

Zero-dependency dataclasses, same hygiene as ``engine.risk.types`` and
``engine.sizing.types``. Nothing here imports a provider, a network client,
or an LLM.

The important type is ``ScanSignal``. It carries a **named rule identifier**
— ``dma20_cross_up``, ``volume_spike_2x`` — for exactly the reason the risk
engine carries ``veto_rule``: every time the system spends money on an LLM
pass, the audit log must be able to answer "what specifically woke it", in a
string a human can grep and a test can assert on. "The scanner thought it
looked interesting" is not an auditable reason to spend $0.066.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Direction = Literal["bullish", "bearish"]


class TriggerRule:
    """The complete set of named trigger identifiers.

    A class of string constants rather than an ``Enum`` on purpose: these
    ids are persisted in decision rows and rendered into prompts, and the
    repo's convention (``Side``, veto rules) is that a persisted identifier
    is the plain string. ``ALL`` exists so a test can assert the registry
    and the documented catalogue never drift apart.
    """

    # ── Moving-average crosses ───────────────────────────────────────
    DMA20_CROSS_UP = "dma20_cross_up"
    DMA20_CROSS_DOWN = "dma20_cross_down"
    DMA50_CROSS_UP = "dma50_cross_up"
    DMA50_CROSS_DOWN = "dma50_cross_down"
    DMA200_CROSS_UP = "dma200_cross_up"
    DMA200_CROSS_DOWN = "dma200_cross_down"

    # ── RSI band transitions ─────────────────────────────────────────
    RSI_EXIT_OVERSOLD = "rsi_exit_oversold"
    RSI_EXIT_OVERBOUGHT = "rsi_exit_overbought"
    RSI_ENTER_OVERSOLD = "rsi_enter_oversold"
    RSI_ENTER_OVERBOUGHT = "rsi_enter_overbought"

    # ── Volume ───────────────────────────────────────────────────────
    VOLUME_SPIKE_2X = "volume_spike_2x"
    VOLUME_SPIKE_3X = "volume_spike_3x"

    # ── Volatility ───────────────────────────────────────────────────
    ATR_EXPANSION = "atr_expansion_1_5x"
    GAP_UP = "gap_up_2pct"
    GAP_DOWN = "gap_down_2pct"

    # ── Donchian channel ─────────────────────────────────────────────
    DONCHIAN_BREAKOUT_UP = "donchian_20_breakout_up"
    DONCHIAN_BREAKDOWN = "donchian_10_breakdown"
    DONCHIAN_UPPER_APPROACH = "donchian_upper_approach"
    DONCHIAN_LOWER_APPROACH = "donchian_lower_approach"

    # ── Standardized stretch ─────────────────────────────────────────
    ZSCORE_STRETCH_UP = "zscore_stretch_up"
    ZSCORE_STRETCH_DOWN = "zscore_stretch_down"

    ALL: frozenset[str] = frozenset(
        {
            "dma20_cross_up",
            "dma20_cross_down",
            "dma50_cross_up",
            "dma50_cross_down",
            "dma200_cross_up",
            "dma200_cross_down",
            "rsi_exit_oversold",
            "rsi_exit_overbought",
            "rsi_enter_oversold",
            "rsi_enter_overbought",
            "volume_spike_2x",
            "volume_spike_3x",
            "atr_expansion_1_5x",
            "gap_up_2pct",
            "gap_down_2pct",
            "donchian_20_breakout_up",
            "donchian_10_breakdown",
            "donchian_upper_approach",
            "donchian_lower_approach",
            "zscore_stretch_up",
            "zscore_stretch_down",
        }
    )


@dataclass(frozen=True)
class ScanSignal:
    """One deterministic trigger firing on one symbol at one instant.

    ``strength`` is 0..1 and is only comparable WITHIN a rule — it says
    "how far past the threshold", not "how good a trade". Ranking two
    different rules against each other by strength would be assigning
    edge the scanner has no basis to claim; the council does that job.

    ``context`` carries the numbers the rule actually compared, so a
    decision row can reconstruct the trigger without re-fetching bars.
    """

    symbol: str
    trigger_rule: str
    strength: float
    observed_at: datetime
    direction: Direction
    detail: str
    context: Mapping[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Prompt/JSON view. ``rule`` is the short key the analysts read."""
        return {
            "symbol": self.symbol,
            "rule": self.trigger_rule,
            "strength": round(self.strength, 3),
            "direction": self.direction,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat(),
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class SymbolSnapshot:
    """Everything the trigger rules read, for one symbol, at one instant.

    Built once per symbol per scan by ``engine.scanner.engine``. The split
    matters: the ``*_prior`` fields come from SETTLED daily bars and do not
    move intraday, while ``last_price`` / ``session_*`` come from today's
    intraday tape. Every "cross" rule is precisely a disagreement between
    those two halves, which is why they are separate fields and not one
    merged series.
    """

    symbol: str
    observed_at: datetime

    # ── Live (intraday) ──────────────────────────────────────────────
    last_price: float
    session_open: float | None
    session_high: float | None
    session_low: float | None
    session_volume: float
    intraday_bars: int

    # ── Settled (daily bars, constant through the session) ───────────
    prior_close: float
    sma20: float | None
    sma50: float | None
    sma200: float | None
    rsi_prior: float | None
    rsi_live: float | None
    atr_14: float | None
    avg_volume_20d: float | None
    donchian_high_20: float | None
    donchian_low_20: float | None
    donchian_low_10: float | None
    close_mean_20: float | None
    close_std_20: float | None

    @property
    def has_intraday(self) -> bool:
        """False when today's tape is empty — no cross rules can fire."""
        return self.intraday_bars > 0


@dataclass(frozen=True)
class ScannerConfig:
    """Thresholds for the trigger rules. Every number here is a policy
    choice, so it lives in one reviewable place rather than inline.

    Defaults are deliberately conservative: this gate decides how often
    real money gets spent on an LLM council pass, and a scanner that fires
    on everything is the same as no scanner at all.
    """

    bar_minutes: int = 15
    """Intraday bar size. 15 matches the free feed's ~15-minute embargo —
    a finer bar would poll for data that does not exist yet."""

    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    volume_spike_mult: float = 2.0
    volume_spike_strong_mult: float = 3.0

    atr_expansion_mult: float = 1.5
    """Today's true range vs ATR-14. 1.5x is roughly a 2-sigma range day."""

    gap_pct: float = 2.0
    """Session open vs prior close, in percent."""

    donchian_approach_band_pct: float = 10.0
    """Approach is measured as POSITION IN THE CHANNEL, not distance in
    percent. Price in the top (or bottom) decile of its own 20-day
    high/low range counts as approaching that edge.

    A raw percent-distance rule looks simpler and is wrong: a name whose
    20-day range is only 1% wide sits permanently 'within 1% of its high',
    so the rule would fire forever on exactly the quietest names — the
    ones with the least to say."""

    zscore_threshold: float = 2.0
    """|price z-score vs its own 20-day mean| beyond this is a real extreme."""

    cross_buffer_pct: float = 0.1
    """A cross must clear the level by this % to count. Without it, a name
    sitting exactly on its 20-DMA re-triggers on every scan as the last
    print oscillates a cent either side of the line."""

    min_daily_bars: int = 60
    """Below this there is not enough settled history to trust any level."""


@dataclass(frozen=True)
class ScanResult:
    """One complete scan pass. What the scheduler logs and acts on."""

    scanned_at: datetime
    market_open: bool
    symbols_scanned: tuple[str, ...]
    signals: tuple[ScanSignal, ...]
    suppressed: tuple[ScanSignal, ...]
    """Signals a cooldown swallowed. Kept for observability — a scan that
    is constantly suppressing the same rule means the cooldown or the
    threshold is mistuned, and that is invisible if we drop them."""
    relative_strength: Mapping[str, float] = field(default_factory=dict)
    """Cross-sectional 63-day return rank across the scanned universe."""
    errors: Mapping[str, str] = field(default_factory=dict)
    """symbol → reason, for symbols that could not be evaluated."""

    @property
    def triggered_symbols(self) -> tuple[str, ...]:
        """Symbols with at least one live signal, in first-fired order."""
        seen: list[str] = []
        for s in self.signals:
            if s.symbol not in seen:
                seen.append(s.symbol)
        return tuple(seen)

    def signals_for(self, symbol: str) -> tuple[ScanSignal, ...]:
        return tuple(s for s in self.signals if s.symbol == symbol)
