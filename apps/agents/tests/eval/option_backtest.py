"""Does a direction signal survive being traded through a long option?

docs/PLAN_PLATFORM.md Phase 3, part 3. `signal_backtest` asks whether the
UNDERLYING goes the predicted way. That is necessary and not sufficient:
entry_quality showed right calls losing 10-24% because the move was too
small to pay theta and the spread. This re-prices every signal as the
option the desk would have bought, on every day of the hold, so the
verdict is on premium P&L, not on the direction.

The model, with each choice stated so it can be argued with:

  contract   a call for long, a put for short; |delta| 0.45 at entry
             (the centre of the low-conviction band the live desk actually
             traded); DTE covering the holding horizon plus a week, clamped
             to the 10-45 day window `select_contract` uses
  IV         20-day realized vol x 1.15, floored at 10%. Implied usually
             sits ABOVE realized (the variance risk premium) and the same
             ratio is used at entry and exit, so IV neither helps nor hurts
             on average. It is a proxy, and `calibrate` reports how far it
             sits from the IVs actually paid on the recorded fills.
  price      Black-Scholes (engine.options.pricing), marked at the daily
             close. It under-prices American puts slightly, which flatters
             puts.
  costs      2.5% of premium per side, since measured entry spreads were
             0.1-5.9% and the live desk pays mid+tick
  exits      the live -40% premium stop on daily marks, otherwise out at
             the horizon. Daily marks see LESS intraday noise than the
             live 30s poller, so this is kinder to the stop than live was.

    python -m tests.eval.option_backtest
    python -m tests.eval.option_backtest --oracle   # plus the cost floor

`--oracle` also runs a signal that knows the future direction. It shows
the cost floor: how much a PERFECT direction call keeps once it is paid
through an option. If the oracle is weak at a horizon, no real signal can
be traded there through options.
"""

from __future__ import annotations

import argparse
import math
import statistics as st
from dataclasses import dataclass
from datetime import datetime

from tests.eval.acceptance import AcceptanceBar, Observation, evaluate
from tests.eval.signal_backtest import _load, non_overlapping, run

from engine.features.technicals import DailyBar
from engine.options.breakeven import expected_move_pct, required_move_pct
from engine.options.pricing import implied_vol, price, strike_for_delta

OPTION_HORIZONS = (2, 5, 10, 20)
"""60 is excluded: 60 trading days is ~84 calendar days, past the 45-DTE
ceiling, so there is no contract to hold for it. That mismatch is exactly
what the `horizon_exceeds_contract` rule refuses live."""

OPTIONS_BAR = AcceptanceBar(cost_pct=0.0)
"""Spread is charged inside the simulation, per side, in premium terms.
Charging the equity default on top would double-count it."""


@dataclass(frozen=True)
class OptionModel:
    target_abs_delta: float = 0.45
    min_dte: int = 10
    max_dte: int = 45
    iv_over_rv: float = 1.15
    iv_floor: float = 0.10
    half_spread_pct: float = 2.5
    stop_loss_pct: float | None = 40.0
    rv_window: int = 20
    breakeven_gate_ratio: float | None = None
    """When set, skip entries whose breakeven move over the hold exceeds
    this multiple of the typical move, using the same
    `engine.options.breakeven` functions as the live
    `expected_move_below_breakeven` rule. None = ungated."""


def realized_vol(bars: list[DailyBar], t: int, window: int) -> float | None:
    """Annualised close-to-close vol over the `window` returns ending at t."""
    if t < window:
        return None
    rets = [
        math.log(bars[i].close / bars[i - 1].close)
        for i in range(t - window + 1, t + 1)
        if bars[i - 1].close > 0 and bars[i].close > 0
    ]
    if len(rets) < 2:
        return None
    return st.stdev(rets) * math.sqrt(252.0)


def dte_for(horizon: int, model: OptionModel) -> int | None:
    """Calendar DTE covering `horizon` trading days plus a week, so the hold
    ends before the steepest part of the decay. None when even the ceiling
    is shorter than the hold."""
    need = math.ceil(horizon * 7 / 5)
    if need >= model.max_dte:
        return None
    return max(model.min_dte, min(model.max_dte, need + 7))


def simulate(
    bars: list[DailyBar], t: int, direction: str, horizon: int, model: OptionModel
) -> float | None:
    """Premium return in % for the option bought at bars[t]'s close and held
    to the stop or `horizon`. None when the inputs cannot price it."""
    if t + horizon >= len(bars):
        return None
    dte = dte_for(horizon, model)
    rv = realized_vol(bars, t, model.rv_window)
    if dte is None or rv is None:
        return None
    kind = "call" if direction == "long" else "put"
    spot = bars[t].close
    iv0 = max(model.iv_floor, rv * model.iv_over_rv)
    t0 = dte / 365.0
    strike = strike_for_delta(spot, t0, iv0, kind=kind, target_abs_delta=model.target_abs_delta)
    mid0 = price(spot, strike, t0, iv0, kind=kind)
    if mid0 <= 0:
        return None
    paid = mid0 * (1 + model.half_spread_pct / 100.0)

    if model.breakeven_gate_ratio is not None:
        hold = (bars[t + horizon].day - bars[t].day).days
        need = required_move_pct(
            spot=spot, strike=strike, kind=kind, dte_days=dte, iv=iv0, paid=paid,
            half_spread_pct=model.half_spread_pct, hold_days=hold,
        )
        typical = expected_move_pct(realized_vol_pct=rv * 100.0, hold_days=hold)
        if need is None or typical is None or need > typical * model.breakeven_gate_ratio:
            return None

    mark = mid0
    for k in range(1, horizon + 1):
        bar = bars[t + k]
        elapsed = (bar.day - bars[t].day).days
        rv_k = realized_vol(bars, t + k, model.rv_window) or rv
        iv_k = max(model.iv_floor, rv_k * model.iv_over_rv)
        mark = price(bar.close, strike, max(0.0, (dte - elapsed) / 365.0), iv_k, kind=kind)
        if model.stop_loss_pct is not None and mark <= paid * (1 - model.stop_loss_pct / 100.0):
            break
    received = mark * (1 - model.half_spread_pct / 100.0)
    return (received / paid - 1.0) * 100.0


def backtest(
    horizon: int, *, model: OptionModel | None = None, oracle: bool = False, step: int = 5,
    signals=None, data=None,
) -> list[Observation]:
    """One option-premium Observation per independent signal at `horizon`.
    `oracle=True` replaces the signal's direction with the direction the
    underlying ACTUALLY moved: the cost floor, not a candidate."""
    model = model or OptionModel()
    data = data if data is not None else _load()
    signals = signals if signals is not None else run(step=step)
    index = {sym: {b.day: i for i, b in enumerate(bars)} for sym, bars in data.items()}
    out: list[Observation] = []
    for s in non_overlapping(signals, horizon):
        bars = data.get(s.symbol)
        t = index.get(s.symbol, {}).get(s.day)
        if bars is None or t is None or t + horizon >= len(bars):
            continue
        direction = s.direction
        if oracle:
            direction = "long" if bars[t + horizon].close >= bars[t].close else "short"
        ret = simulate(bars, t, direction, horizon, model)
        if ret is not None:
            out.append(Observation(day=s.day, ret_pct=ret))
    return out


def calibrate(data: dict[str, list[DailyBar]] | None = None) -> list[tuple[str, float, float]]:
    """(occ, IV implied by the recorded entry price, the model's proxy IV) for
    every recorded fill whose entry day has a bar. The proxy IV is judged
    against real fills, so the reader knows how far to trust it.

    The spot is the entry day's CLOSE, not the fill-time price, so each
    pair is approximate. Only the ratio's typical size is informative."""
    import json
    from pathlib import Path

    data = data if data is not None else _load()
    paths = json.loads(
        (Path(__file__).parent / "fixtures" / "option_paths.json").read_text()
    )["paths"]
    model = OptionModel()
    out: list[tuple[str, float, float]] = []
    for p in paths:
        occ = p["occ"]
        root, tail = occ[:-15], occ[-15:]
        expiry = datetime.strptime(tail[:6], "%y%m%d").date()
        kind = "call" if tail[6] == "C" else "put"
        strike = int(tail[7:]) / 1000.0
        entry_day = datetime.fromtimestamp(p["samples"][0][0]).date() if p["samples"] else None
        bars = data.get(root)
        if not bars or entry_day is None:
            continue
        t = next((i for i, b in enumerate(bars) if b.day == entry_day), None)
        if t is None:
            continue
        dte = (expiry - entry_day).days
        paid_iv = implied_vol(p["entry"], bars[t].close, strike, dte / 365.0, kind=kind)  # type: ignore[arg-type]
        rv = realized_vol(bars, t, model.rv_window)
        if paid_iv is None or rv is None:
            continue
        out.append((occ, paid_iv, max(model.iv_floor, rv * model.iv_over_rv)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--step", type=int, default=5)
    args = ap.parse_args()

    data = _load()
    signals = run(step=args.step)
    print("\noption-premium backtest: |delta| 0.45, IV = RV20 x 1.15, 2.5%/side, -40% stop\n")
    for h in OPTION_HORIZONS:
        obs = backtest(h, signals=signals, data=data)
        print(f"  signal {h:>3}d  {evaluate(obs, OPTIONS_BAR, n_tests=len(OPTION_HORIZONS)).line()}")
        if args.oracle:
            orc = backtest(h, signals=signals, data=data, oracle=True)
            mean = st.mean(o.ret_pct for o in orc) if orc else float("nan")
            print(f"  oracle {h:>3}d  mean premium return {mean:+.1f}% (perfect direction, n={len(orc)})")

    print("\n  with the live breakeven gate (ratio 0.8), same signals:\n")
    gated = OptionModel(breakeven_gate_ratio=0.8)
    for h in OPTION_HORIZONS:
        obs = backtest(h, signals=signals, data=data, model=gated)
        print(f"  signal {h:>3}d  {evaluate(obs, OPTIONS_BAR, n_tests=len(OPTION_HORIZONS)).line()}")
        if args.oracle:
            orc = backtest(h, signals=signals, data=data, oracle=True, model=gated)
            mean = st.mean(o.ret_pct for o in orc) if orc else float("nan")
            print(f"  oracle {h:>3}d  mean premium return {mean:+.1f}% (n={len(orc)})")

    cal = calibrate(data)
    if cal:
        ratios = [paid / proxy for _, paid, proxy in cal]
        print(f"\n  IV proxy vs IV actually paid, {len(cal)} recorded fills with an entry-day bar:")
        print(f"    median paid/proxy {st.median(ratios):.2f}  (1.00 = the proxy is right; "
              f"range {min(ratios):.2f}-{max(ratios):.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

