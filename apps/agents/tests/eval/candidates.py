"""Candidate signals, pre-registered, run through the same bar as the incumbent.

docs/PLAN_PLATFORM.md Phase 3. The two cheapest candidates in the plan,
the ones that need nothing but daily bars. Their rules and horizons were
fixed from the standard literature definitions BEFORE this file was first
run, and are not tuned after:

  short_term_reversal   price_zscore_20 <= -2 -> long, >= +2 -> short,
                        otherwise no call. Judged at 5 trading days.
                        (Short-horizon reversal: Lehmann 1990, Jegadeesh
                        1990. The effect is documented as weaker in large
                        caps, which is what this universe is.)

  momentum_12_1         sign of the 12-month return EXCLUDING the latest
                        month, (1 + r252) / (1 + r21) - 1. Judged at 60
                        trading days, in EQUITY: about 84 calendar days,
                        past any contract the desk may buy.
                        (Time-series momentum: Moskowitz, Ooi & Pedersen
                        2012. The 12-1 construction is Jegadeesh & Titman.)

Each candidate is judged at exactly ONE horizon, with Bonferroni over the
two candidates (n_tests=2). Scanning horizons for the one that works is
how the 5d/20d stride artefact nearly got reported as an edge.

    python -m tests.eval.candidates
    python -m tests.eval.candidates --market IN   # the same two, on NSE (PLAN_ZERODHA Z4)

On NSE the rules and horizons are unchanged (re-tuning them for India
would be the scan this file forbids), long calls only (delivery cannot be
held short), net of the NSE delivery round trip, and there is no option
line: option_backtest has no NSE lot or cost model yet.

RESULT, first and only run (2026-09-23, 58 large-cap symbols, 2021-2026):

  short_term_reversal  equity  5d   mean_net -0.087%  t=-0.72  hit 47.8%  n=1485  FAIL
  short_term_reversal  option  5d   mean     -4.571%  t=-1.68  hit 26.3%          FAIL
  momentum_12_1        equity 60d   mean_net +0.362%  t=+0.66  hit 50.8%  n=1083  FAIL
                                    (halves t -0.43 / +1.20: not stable)

Neither clears the bar. With the incumbent that makes three price-only
signals on this universe with no detectable edge. Large-cap daily prices
are the most-studied data in finance, so an edge is unlikely to live in
features derived only from them. The next candidates need NEW information:
the earnings calendar (post-earnings drift), estimate revisions, and the
event interpreter's structured news features (PLAN_PLATFORM §D P0,
Phase 5b).
"""

from __future__ import annotations

from typing import Any

from tests.eval.acceptance import AcceptanceBar, Observation, evaluate
from tests.eval.option_backtest import OPTIONS_BAR, backtest
from tests.eval.signal_backtest import _load, market, non_overlapping, run

from engine.alpha import Signal

REVERSAL_Z = 2.0
REVERSAL_HORIZON = 5
MOMENTUM_HORIZON = 60
N_TESTS = 2


def _num(block: Any, key: str) -> float | None:
    v = block.get(key) if isinstance(block, dict) else getattr(block, key, None)
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _call(name: str, symbol: str, direction: str, value: float, **meta: Any) -> Signal:
    """A directional call in the shape `signal_backtest.run` scores:
    `meta.strategy_id` means "a call was made"."""
    signed = abs(value) if direction == "long" else -abs(value)
    return Signal(
        name=name, symbol=symbol, value=max(-1.0, min(1.0, signed)),
        meta={"strategy_id": name, "direction": direction, "score": abs(value), **meta},
    )


class ShortTermReversal:
    name = "short_term_reversal"

    def evaluate(self, snapshot: dict[str, Any], *, symbol: str) -> Signal:
        z = _num(snapshot.get("quant") or {}, "price_zscore_20")
        if z is None:
            return Signal.abstain(self.name, symbol, reason="no_price_zscore_20")
        if z <= -REVERSAL_Z:
            return _call(self.name, symbol, "long", min(1.0, abs(z) / 4.0), z=z)
        if z >= REVERSAL_Z:
            return _call(self.name, symbol, "short", min(1.0, abs(z) / 4.0), z=z)
        return Signal(name=self.name, symbol=symbol, value=0.0, reason="inside_band")


class Momentum12_1:
    name = "momentum_12_1"

    def evaluate(self, snapshot: dict[str, Any], *, symbol: str) -> Signal:
        quant = snapshot.get("quant") or {}
        r252 = _num(quant, "ret_252d_pct")
        r21 = _num(quant, "ret_21d_pct")
        if r252 is None or r21 is None:
            return Signal.abstain(self.name, symbol, reason="no_12m_or_1m_return")
        m = (1 + r252 / 100.0) / (1 + r21 / 100.0) - 1.0
        if m == 0:
            return Signal(name=self.name, symbol=symbol, value=0.0, reason="exactly_flat")
        return _call(self.name, symbol, "long" if m > 0 else "short", min(1.0, abs(m)), m=m)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="pre-registered candidates")
    ap.add_argument("--market", choices=("US", "IN"), default="US")
    args = ap.parse_args(argv)
    for line in verdicts(args.market):
        print(line)
    return 0


def verdicts(market_name: str = "US", data: dict | None = None) -> list[str]:
    """One line per candidate, judged exactly as registered above."""
    data = data if data is not None else _load(market_name)
    bar = AcceptanceBar(cost_pct=market(market_name).cost_pct)
    lines = [f"\npre-registered candidates ({market_name}), one horizon each, "
             "Bonferroni over 2\n"]

    rev = run(model=ShortTermReversal(), market_name=market_name, data=data)
    obs = [Observation(s.day, s.fwd[REVERSAL_HORIZON])
           for s in non_overlapping(rev, REVERSAL_HORIZON)]
    lines.append(f"  short_term_reversal  equity  {REVERSAL_HORIZON}d  "
                 f"{evaluate(obs, bar, n_tests=N_TESTS).line()}")
    if market_name == "US":
        opt = backtest(REVERSAL_HORIZON, signals=rev, data=data)
        lines.append(f"  short_term_reversal  option  {REVERSAL_HORIZON}d  "
                     f"{evaluate(opt, OPTIONS_BAR, n_tests=N_TESTS).line()}")

    mom = run(model=Momentum12_1(), market_name=market_name, data=data)
    obs = [Observation(s.day, s.fwd[MOMENTUM_HORIZON])
           for s in non_overlapping(mom, MOMENTUM_HORIZON)]
    lines.append(f"  momentum_12_1        equity {MOMENTUM_HORIZON}d  "
                 f"{evaluate(obs, bar, n_tests=N_TESTS).line()}")
    if market_name == "IN":
        lines.append("\n  Universe is today's NIFTY 50: survivorship bias flatters both lines.")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
