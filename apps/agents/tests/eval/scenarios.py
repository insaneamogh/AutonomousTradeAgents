"""The golden dataset — 100 labelled market scenarios.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
These are ARCHETYPE scenarios with hand-reasoned expected outcomes,
generated deterministically from a seed. They are **not** a historical
backtest: this suite runs offline with no Alpaca credentials and no
network, so no real bars were fetched and no real P&L was measured.

That distinction matters and must not be blurred, because the two answer
different questions:

  * A backtest answers *"would this strategy have made money?"* — which
    needs real history, survivorship-bias handling, and far more than a
    hundred samples to say anything with statistical weight.
  * This suite answers *"does the deterministic layer actually fire, and
    does it narrow the funnel the way the architecture claims?"* — which
    is a question about CODE BEHAVIOUR, and for that, labelled archetypes
    are the right instrument and real bars would add noise, not rigor.

The second question is the one worth being sure about the night before a
submission, and it is the one that is answerable for free, in CI, in
under a second, and reproducibly. If a scenario's expected outcome is
wrong, that is a bug in this file and should be fixed here — the point is
that the expectation is written down and checked, not that it came from
the market.

HOW THE ARCHETYPES WERE CHOSEN
------------------------------
Ten archetypes x ten variations. The archetypes are the shapes the
deterministic layer is supposed to tell apart, including the three that
have actually cost money or gone wrong in this repo:

  * ``thin_evidence``   — the empty-dict bug: ``best_strategy({})`` used to
    return rsi_mean_reversion at 0.60 because the "unknown" trend sentinel
    satisfied ``not_a_trend_break`` as a genuine TRUE.
  * ``illiquid_chain``  — the CME shape: a chain where one contract
    scrapes past the liquidity gate and the ranking does no work.
  * ``thin_open_interest`` — sizing with no liquidity dimension, which
    turned 167 open interest into a 5-lot position.

Variations move the inputs across each archetype's decision boundary on
purpose, so roughly a third of every archetype sits near its own
threshold. A suite where every case is comfortably far from the boundary
passes whatever the thresholds are, and therefore tests nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Expectation = Literal["trade", "hold", "either"]


@dataclass(frozen=True)
class Scenario:
    """One symbol-day, plus what the deterministic layer should decide."""

    case_id: str
    archetype: str
    symbol: str
    features: dict[str, Any]

    expect: Expectation
    """``trade`` — must clear the fit floor and reach the LLM.
    ``hold``  — must be refused BEFORE any LLM call.
    ``either`` — near a threshold on purpose; the suite asserts only that
    the outcome is deterministic and carries a named reason, not which
    way it lands. Marking a boundary case ``trade`` or ``hold`` would pin
    the current threshold value as if it were a requirement, and these
    thresholds are explicitly tunable."""

    why: str
    """One line: why a human says this is the right answer. If a case
    fails, this is what tells you whether the code or the label is wrong."""

    options: dict[str, Any] | None = None
    """Contract-level inputs for the options stages (chain depth, open
    interest, premium). ``None`` for equity-only scenarios."""

    tags: tuple[str, ...] = field(default_factory=tuple)


def _features(
    *,
    symbol: str,
    trend: str,
    rsi: float,
    dma20: float,
    dma50: float,
    dma200: float,
    zscore: float,
    donchian: float,
    sharpe: float,
    ret21: float,
    ret63: float,
    ret252: float,
    vol_pct: float,
    atr_z: float = 0.0,
    volume_ratio: float = 1.0,
    last_price: float = 100.0,
    include_quant: bool = True,
    include_technicals: bool = True,
) -> dict[str, Any]:
    """Build a feature dict in the exact shape a real pass produces.

    Mirrors ``trading_agents.features.synthetic.synthetic_features`` key
    for key. Built explicitly rather than by perturbing that function's
    output so a scenario's inputs are readable at the call site — the
    label is only trustworthy if you can see what produced it.
    """
    out: dict[str, Any] = {
        "symbol": symbol,
        "horizon": "short",
        "universe": "US",
        "last_price": last_price,
        "portfolio_equity": 100_000.0,
        "fundamentals": {
            "quality_score": 60.0,
            "business_quality_score": 60.0,
            "earnings_power_score": 55.0,
            "valuation_score": 50.0,
            "growth_trajectory": 55.0,
            "capital_efficiency": 58.0,
            "shareholder_returns": 50.0,
            "piotroski_f_score": 6,
        },
    }
    if include_technicals:
        out["technicals"] = {
            "trend_regime": trend,
            "dma20_pct": dma20,
            "dma50_pct": dma50,
            "dma200_pct": dma200,
            "rsi_14": rsi,
            "atr_14": round(last_price * 0.02, 4),
            "vwap_position": "above" if dma20 > 0 else "below",
            "mean_reversion_risk": 30.0,
            "trend_position_score": 60.0,
            "volume_ratio_20d": volume_ratio,
        }
    if include_quant:
        out["quant"] = {
            "price_zscore_20": zscore,
            "atr_zscore": atr_z,
            "donchian_pct": donchian,
            "sharpe": sharpe,
            "ret_21d_pct": ret21,
            "ret_63d_pct": ret63,
            "ret_252d_pct": ret252,
            "realized_vol_pct": vol_pct,
            "corr_benchmark": 0.5,
        }
    return out


# ── Archetype builders ───────────────────────────────────────────────
#
# Each returns ONE scenario for variation index `i` in 0..9. `i` walks
# the archetype across its own decision boundary, so a third of every
# archetype sits near the threshold rather than comfortably inside it.


def _clean_uptrend(i: int) -> Scenario:
    """Strong, confirmed uptrend with room left. The bread-and-butter
    long. If the maths cannot fire on THIS, it fires on nothing."""
    strength = 1.0 - (i * 0.06)
    return Scenario(
        case_id=f"uptrend_{i:02d}",
        archetype="clean_uptrend",
        symbol=f"UPT{i}",
        features=_features(
            symbol=f"UPT{i}", trend="uptrend",
            rsi=58.0 + i * 0.8, dma20=3.0 * strength, dma50=6.0 * strength,
            dma200=18.0 * strength, zscore=0.9 * strength, donchian=78.0 - i,
            sharpe=1.4 * strength, ret21=6.0 * strength, ret63=14.0 * strength,
            ret252=32.0 * strength, vol_pct=22.0, atr_z=-0.3,
            volume_ratio=1.25,
        ),
        expect="trade" if i < 7 else "either",
        why=(
            "Uptrend confirmed on three moving averages, RSI mid-band with "
            "headroom, positive Sharpe and trailing returns. The later "
            "variations decay toward the floor on purpose."
        ),
        tags=("long", "trend"),
    )


def _clean_downtrend(i: int) -> Scenario:
    """The mirror image. Bearish options (long puts) are a live, proven
    path here — 4 real fills — so this must not be silently unreachable."""
    strength = 1.0 - (i * 0.06)
    return Scenario(
        case_id=f"downtrend_{i:02d}",
        archetype="clean_downtrend",
        symbol=f"DWN{i}",
        features=_features(
            symbol=f"DWN{i}", trend="downtrend",
            rsi=42.0 - i * 0.8, dma20=-3.0 * strength, dma50=-6.0 * strength,
            dma200=-16.0 * strength, zscore=-0.9 * strength, donchian=22.0 + i,
            sharpe=-1.2 * strength, ret21=-6.0 * strength, ret63=-13.0 * strength,
            ret252=-28.0 * strength, vol_pct=26.0, atr_z=0.2,
            volume_ratio=1.2,
        ),
        expect="either",
        why=(
            "A confirmed downtrend. Whether it TRADES depends on whether "
            "shorts/puts are enabled for the run, which is a caller "
            "decision — so this asserts determinism and a named reason, "
            "not a direction."
        ),
        tags=("short", "trend"),
    )


def _choppy_nothing(i: int) -> Scenario:
    """No edge. The single most important HOLD: this is the shape that
    makes up most of any watchlist on most days, and every one of these
    that reaches an LLM is money burned for a foregone conclusion."""
    return Scenario(
        case_id=f"choppy_{i:02d}",
        archetype="choppy_nothing",
        symbol=f"CHP{i}",
        features=_features(
            symbol=f"CHP{i}", trend="choppy",
            rsi=49.0 + (i % 3) * 0.5, dma20=0.2 - (i % 3) * 0.1,
            dma50=-0.1, dma200=0.4, zscore=0.05 * (1 if i % 2 else -1),
            donchian=50.0 + (i % 4), sharpe=0.05, ret21=0.3, ret63=-0.4,
            ret252=1.1, vol_pct=19.0, atr_z=0.0, volume_ratio=0.98,
        ),
        expect="hold",
        why=(
            "Flat across every timeframe, RSI at the midpoint, Sharpe ~0. "
            "There is no directional evidence to pay a model to think about."
        ),
        tags=("hold", "no-edge"),
    )


def _thin_evidence(i: int) -> Scenario:
    """Missing data must read as "we don't know", never as "nothing is
    wrong". This is the empty-dict bug: the 'unknown' trend sentinel used
    to satisfy `not_a_trend_break` as a genuine TRUE and score 0.60."""
    variants = [
        dict(include_quant=False, include_technicals=True, trend="unknown"),
        dict(include_quant=False, include_technicals=True, trend="uptrend"),
        dict(include_quant=True, include_technicals=False, trend="uptrend"),
        dict(include_quant=False, include_technicals=False, trend="unknown"),
        dict(include_quant=True, include_technicals=True, trend="unknown"),
    ]
    v = variants[i % len(variants)]
    return Scenario(
        case_id=f"thin_{i:02d}",
        archetype="thin_evidence",
        symbol=f"THN{i}",
        features=_features(
            symbol=f"THN{i}", trend=str(v["trend"]),
            rsi=55.0, dma20=1.0, dma50=2.0, dma200=5.0, zscore=0.3,
            donchian=60.0, sharpe=0.8, ret21=2.0, ret63=5.0, ret252=12.0,
            vol_pct=20.0,
            include_quant=bool(v["include_quant"]),
            include_technicals=bool(v["include_technicals"]),
        ),
        expect="hold",
        why=(
            "A feature block is missing or the trend regime is 'unknown'. "
            "The evidence gate must refuse: absence of evidence is not "
            "evidence of a good setup."
        ),
        tags=("hold", "evidence-gate", "regression"),
    )


def _overbought_extreme(i: int) -> Scenario:
    """Extended far above the mean. Either a mean-reversion short or a
    stand-aside — never a fresh momentum long at the top."""
    stretch = 2.2 + i * 0.15
    return Scenario(
        case_id=f"overbought_{i:02d}",
        archetype="overbought_extreme",
        symbol=f"OVB{i}",
        features=_features(
            symbol=f"OVB{i}", trend="uptrend",
            rsi=78.0 + i * 0.9, dma20=9.0, dma50=15.0, dma200=41.0,
            zscore=stretch, donchian=99.0, sharpe=1.9, ret21=19.0,
            ret63=34.0, ret252=75.0, vol_pct=38.0, atr_z=1.4,
            volume_ratio=1.9,
        ),
        expect="either",
        why=(
            "RSI ~80 and 2+ standard deviations extended. A momentum long "
            "here buys the top; the zscore_stretch component exists to "
            "penalise exactly this."
        ),
        tags=("mean-reversion", "boundary"),
    )


def _high_volatility(i: int) -> Scenario:
    """Directionally fine, but the vol makes the position size the real
    question. Tests that vol reaches sizing rather than being ignored."""
    return Scenario(
        case_id=f"highvol_{i:02d}",
        archetype="high_volatility",
        symbol=f"HVL{i}",
        features=_features(
            symbol=f"HVL{i}", trend="uptrend",
            rsi=61.0, dma20=4.0, dma50=7.0, dma200=15.0, zscore=1.0,
            donchian=72.0, sharpe=0.7, ret21=8.0, ret63=12.0, ret252=25.0,
            vol_pct=55.0 + i * 3.0, atr_z=2.0 + i * 0.1, volume_ratio=1.6,
        ),
        expect="either",
        why=(
            "A real uptrend under 55%+ realized vol. vol_regime_calm should "
            "drag the score down; whether it clears the floor is a "
            "threshold question, not a correctness one."
        ),
        tags=("volatility", "sizing"),
    )


def _illiquid_chain(i: int) -> Scenario:
    """The CME shape. The UNDERLYING looks fine — that is the trap. The
    refusal has to come from the chain, and it has to come before the
    debate is paid for."""
    return Scenario(
        case_id=f"illiquid_{i:02d}",
        archetype="illiquid_chain",
        symbol=f"ILQ{i}",
        features=_features(
            symbol=f"ILQ{i}", trend="uptrend",
            rsi=59.0, dma20=3.5, dma50=6.5, dma200=17.0, zscore=0.85,
            donchian=76.0, sharpe=1.3, ret21=6.5, ret63=13.0, ret252=30.0,
            vol_pct=24.0,
        ),
        options={
            "liquid_chain_depth": i % 5,      # 0..4, all under the depth floor of 5
            "open_interest": 120 + i * 8,
            "ask": 4.60,
            "expect_refusal": True,
        },
        expect="hold",
        why=(
            "Fewer than 5 contracts survive the liquidity stage, so the "
            "ranking has nothing to rank — the contract is not selected, "
            "it is all that is left. CME261016P00270000 was exactly this."
        ),
        tags=("options", "regression", "cme"),
    )


def _thin_open_interest(i: int) -> Scenario:
    """Chain deep enough to trade, but the SELECTED contract is thin.
    Sizing must shrink; a veto here would be wrong."""
    oi = 100 + i * 45
    return Scenario(
        case_id=f"thinoi_{i:02d}",
        archetype="thin_open_interest",
        symbol=f"TOI{i}",
        features=_features(
            symbol=f"TOI{i}", trend="uptrend",
            rsi=60.0, dma20=3.2, dma50=6.0, dma200=16.0, zscore=0.8,
            donchian=74.0, sharpe=1.25, ret21=6.0, ret63=12.5, ret252=29.0,
            vol_pct=23.0,
        ),
        options={
            "liquid_chain_depth": 12,
            "open_interest": oi,
            "ask": 4.60,
            "expect_refusal": False,
            "expect_qty_at_most": max(1, oi // 100),
        },
        expect="trade",
        why=(
            "A tradeable chain with a thin selected contract. The liquidity "
            "trim caps size at 1% of open interest; it must TRIM, never "
            "veto, and never round a viable trade to zero."
        ),
        tags=("options", "sizing", "regression"),
    )


def _liquid_options(i: int) -> Scenario:
    """A genuinely liquid contract. The liquidity trim must NOT bind here
    — if it does, it is shrinking every position, not the doubtful ones,
    and the asymmetry the cap was chosen for is gone."""
    return Scenario(
        case_id=f"liquidopt_{i:02d}",
        archetype="liquid_options",
        symbol=f"LQO{i}",
        features=_features(
            symbol=f"LQO{i}", trend="uptrend",
            rsi=57.0 + i * 0.4, dma20=3.8, dma50=7.2, dma200=19.0,
            zscore=0.75, donchian=77.0, sharpe=1.5, ret21=7.0, ret63=15.0,
            ret252=33.0, vol_pct=21.0, volume_ratio=1.3,
        ),
        options={
            "liquid_chain_depth": 40 + i * 10,
            "open_interest": 2800 + i * 400,
            "ask": 4.60,
            "expect_refusal": False,
            "trim_must_not_bind": True,
        },
        expect="trade",
        why=(
            "SPY-shaped: thousands of contracts of open interest. The "
            "premium budget must remain the binding constraint here."
        ),
        tags=("options", "liquid"),
    )


def _earnings_gap_risk(i: int) -> Scenario:
    """Strong setup, but stretched and volatile enough that a gap through
    the stop is the live risk. The honest expectation is 'either' — this
    documents that neither our stop nor a broker stop fixes gap risk."""
    return Scenario(
        case_id=f"gaprisk_{i:02d}",
        archetype="gap_risk",
        symbol=f"GAP{i}",
        features=_features(
            symbol=f"GAP{i}", trend="uptrend",
            rsi=66.0 + i, dma20=6.0, dma50=9.0, dma200=22.0,
            zscore=1.6 + i * 0.08, donchian=88.0, sharpe=1.1, ret21=12.0,
            ret63=20.0, ret252=44.0, vol_pct=44.0 + i * 2, atr_z=1.7,
            volume_ratio=2.1,
        ),
        expect="either",
        why=(
            "Extended and volatile. Gap risk is not addressed by any stop, "
            "ours or the broker's — it is addressed by entry-side chain "
            "depth and by sizing, which is what this case exercises."
        ),
        tags=("volatility", "gap", "boundary"),
    )


_ARCHETYPES = (
    _clean_uptrend,
    _clean_downtrend,
    _choppy_nothing,
    _thin_evidence,
    _overbought_extreme,
    _high_volatility,
    _illiquid_chain,
    _thin_open_interest,
    _liquid_options,
    _earnings_gap_risk,
)


def golden_scenarios() -> list[Scenario]:
    """All 100 cases: 10 archetypes x 10 variations, in a stable order.

    Order is stable so a failure names the same case on every run, and
    generation is pure (no clock, no RNG, no I/O) so two runs of this
    suite on the same commit are byte-identical.
    """
    return [build(i) for build in _ARCHETYPES for i in range(10)]


def scenarios_by_archetype() -> dict[str, list[Scenario]]:
    out: dict[str, list[Scenario]] = {}
    for scenario in golden_scenarios():
        out.setdefault(scenario.archetype, []).append(scenario)
    return out
