"""Where the thesis is wrong — a structural invalidation level.

Every exit in this system is a percentage of the OPTION'S PREMIUM, and that
number is disconnected from the thing the thesis is about. A -40% premium
stop on a 30-delta contract is roughly a 2-3% move in the underlying: an
ordinary session for NVDA, a large move for XLF. The same number is applied
to both, so it fires on noise in one name and far too late in another.

Professional practice is the inverse: the stop sits at the price where the
original thesis is **proven wrong** — under the swing low for a long, over
the swing high for a short, or beyond the moving average the trend had been
respecting. A structure stop is rarely tripped by a trade that is working,
because a working trade leaves structure intact; a percentage stop is
tripped by volatility that says nothing about the thesis either way.

Measured on our own book, the two positions that did the most damage both
peaked and then collapsed:

    AAPL260918C00340000   peaked +6.2%  ->  -41.9%
    XLE261016C00067000    peaked +6.8%  ->  -41.8%

while NVDA260914C00225000 ran to +62.5% without its underlying ever
breaking structure. A structural level separates those cases; a premium
percentage cannot.

**This is deterministic on purpose.** The level is computed here, in Python,
from values the scanner already produces (``donchian_low_20`` and friends on
``SymbolSnapshot``). The model may choose among the levels offered and may
decline them, but it never emits a raw price. A stop level authored by an
LLM is an LLM inside a risk path, which CLAUDE.md section 3 forbids — the
same reason ``effective_stop_loss_pct`` clamps the agent's stop rather than
trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass

_MIN_ATR_DISTANCE = 0.5
"""A level closer than half an ATR is inside the noise the position lives
in day to day, and would be tripped by an ordinary session rather than by
the thesis failing."""

_MAX_ATR_DISTANCE = 4.0
"""Past four ATR the level stops being a stop: on a long option the premium
would be near worthless before the underlying ever reached it, so the
premium cap would fire first and the structural level would be decorative."""


@dataclass(frozen=True)
class InvalidationLevel:
    """A price at which the thesis is falsified, plus why."""

    price: float
    source: str
    """Which structural feature produced it — ``donchian_low_20``,
    ``sma50``, ... Persisted with the decision so an exit can always be
    explained after the fact, the same way a veto carries its rule name."""

    distance_pct: float
    """How far the underlying must move to falsify the thesis. This is the
    number that should be compared against an expected move — a thesis
    needing a 9% move to be wrong is not a tight thesis."""

    distance_atr: float


def invalidation_level(
    *,
    direction: str,
    last_price: float,
    atr_14: float | None,
    donchian_low_10: float | None = None,
    donchian_low_20: float | None = None,
    donchian_high_20: float | None = None,
    sma20: float | None = None,
    sma50: float | None = None,
    min_atr_distance: float = _MIN_ATR_DISTANCE,
    max_atr_distance: float = _MAX_ATR_DISTANCE,
) -> InvalidationLevel | None:
    """The tightest structural level that is still outside the noise, or
    None when the inputs cannot support one.

    Returns None rather than inventing a level — a caller with no level must
    refuse the trade (``no_invalidation_level``), not fall back to a number
    with no structural meaning. That is the entire point: a thesis with no
    falsifying price is not a thesis.

    ``direction`` is the THESIS direction — ``"long"`` looks for support
    below, ``"short"`` for resistance above. A long put is a *short* thesis;
    callers pass the thesis, never the option's buy/sell side.
    """
    if last_price <= 0 or atr_14 is None or atr_14 <= 0:
        return None

    bullish = str(direction).lower() != "short"
    candidates: list[tuple[str, float | None]] = (
        [
            ("donchian_low_10", donchian_low_10),
            ("donchian_low_20", donchian_low_20),
            ("sma20", sma20),
            ("sma50", sma50),
        ]
        if bullish
        else [
            ("donchian_high_20", donchian_high_20),
            ("sma20", sma20),
            ("sma50", sma50),
        ]
    )

    best: InvalidationLevel | None = None
    for source, raw in candidates:
        if raw is None or raw <= 0:
            continue
        # Support must sit BELOW price for a long thesis and resistance
        # ABOVE for a short one. A "support" level already broken through
        # is not support; skipping it is what stops a stale feature from
        # producing a stop on the wrong side of the market.
        if bullish and raw >= last_price:
            continue
        if not bullish and raw <= last_price:
            continue

        distance = abs(last_price - raw)
        in_atr = distance / atr_14
        if not (min_atr_distance <= in_atr <= max_atr_distance):
            continue

        level = InvalidationLevel(
            price=round(raw, 4),
            source=source,
            distance_pct=round(distance / last_price * 100.0, 2),
            distance_atr=round(in_atr, 2),
        )
        # Tightest qualifying level wins: it is the first place the thesis
        # is genuinely in question, so it exits soonest on a real break
        # while still sitting outside the noise band above.
        if best is None or level.distance_atr < best.distance_atr:
            best = level
    return best


def is_invalidated(
    *, direction: str, underlying_price: float, level_price: float
) -> bool:
    """True when the underlying has broken the level and the thesis is done.

    Deliberately a bare comparison with no buffer: the buffer is already in
    the level itself (``_MIN_ATR_DISTANCE``). Adding a second one here would
    hide where the tolerance actually lives.
    """
    if underlying_price <= 0 or level_price <= 0:
        return False
    if str(direction).lower() != "short":
        return underlying_price <= level_price
    return underlying_price >= level_price
