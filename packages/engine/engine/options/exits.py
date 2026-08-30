"""Premium-based exit rules for a long option — the strict half of the
options playbook.

WHY THIS EXISTS. Phase A holds long calls and puts, and until this module
an open option had exactly three ways out:

  - the time stop (5 calendar days on a "short" horizon),
  - a signal exit (a later council pass on the same underlying says SELL),
  - the ``dte <= 2`` expiry sweep.

None of them is a price rule. A call up 90% on Tuesday and back to zero
by Thursday hits none of them; neither does one down 85% on day one.
Alpaca cannot bracket a single-leg option order (``OrderClass`` allows
only simple/mleg for us_option), so the broker-side stop/target that
protects every equity entry is structurally unavailable here — which is
precisely why it has to live in our own deterministic sweep.

**No LLM is anywhere near this.** Same rule as everywhere else in the
engine: agents propose, deterministic code disposes. These are two
comparisons against a broker-reported number.

THE MEASURE IS THE PREMIUM, NOT THE UNDERLYING. A 50% stop means the
premium halved, not the stock. On a 0.5-delta call a 50% premium loss is
roughly a 5% move in the underlying — the leverage is the whole point of
the instrument and the reason a percentage stop that would be absurd on
shares is ordinary here.

ASYMMETRY IS DELIBERATE. Take profit at +60%, stop at -50%. Long options
decay: an option that has not worked is losing value every day it sits,
so the loss side has to be tighter in time-adjusted terms than the gain
side is in nominal terms. Both defaults are conservative for a 4-session
window where the goal is to preserve capital, not to squeeze the last
dollar out of a winner.

These two ARE env-tunable (``OPTIONS_TAKE_PROFIT_PCT`` /
``OPTIONS_STOP_LOSS_PCT``) — unlike the premium *caps*, which are not.
The distinction is deliberate and worth stating: a cap bounds how much
capital can ever be at risk, so widening it by env var would defeat it.
An exit threshold only decides when to realize a position whose size was
already bounded by those caps. Tightening or loosening it cannot increase
maximum loss beyond the premium already paid.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OptionExitSignal:
    """A fired exit rule. ``None`` from ``option_exit_signal`` means hold."""

    reason: str
    """Named rule: ``option_take_profit`` or ``option_stop_loss``. Named
    rather than a bare bool for the same reason every risk veto is named —
    "the agent closed it" is not an answer; "the premium stop fired at
    -52%" is."""

    detail: str
    """Human-readable arithmetic, for the notification and the audit row."""

    pnl_pct: float
    """The unrealized premium P&L that triggered it, in percent."""


def option_exit_signal(
    *,
    unrealized_pl_pct: float | None,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> OptionExitSignal | None:
    """Which premium exit fired, if any.

    ``unrealized_pl_pct`` is the broker's own percentage on the OPTION
    position (Alpaca reports it per contract, already scaled) expressed as
    a percent: +60.0 means the premium is up 60%. ``None`` means the
    broker did not report one — return None (hold) rather than guessing,
    since a fabricated mark would close a position on invented data.

    ``stop_loss_pct`` is given as a POSITIVE magnitude (50.0 = "close if
    the premium has lost half its value"). Accepting it positive and
    comparing against the negative reading keeps every caller and env var
    reading the way a human states the rule, instead of having to remember
    a sign convention.

    Take-profit is checked first. When a position somehow satisfies both
    (impossible with sane thresholds, but not worth depending on), taking
    the gain is the safe resolution.
    """
    if unrealized_pl_pct is None:
        return None

    pct = float(unrealized_pl_pct)

    if take_profit_pct > 0 and pct >= take_profit_pct:
        return OptionExitSignal(
            reason="option_take_profit",
            detail=(
                f"premium +{pct:.1f}% vs take-profit +{take_profit_pct:.1f}%"
            ),
            pnl_pct=round(pct, 2),
        )

    # Gated on the MAGNITUDE, not the raw sign. Callers state the rule as
    # "down 50%" and pass 50.0, but a `-50` typed into an env var must not
    # silently disable the stop — disabling protection is the one failure
    # direction a typo should never be able to reach. Only an explicit 0
    # turns the stop off. Take-profit above is gated the opposite way on
    # purpose: a negative take-profit is nonsense, and reading it as a
    # magnitude would close every LOSING position instantly.
    if abs(stop_loss_pct) > 0 and pct <= -abs(stop_loss_pct):
        return OptionExitSignal(
            reason="option_stop_loss",
            detail=(
                f"premium {pct:.1f}% vs stop -{abs(stop_loss_pct):.1f}%"
            ),
            pnl_pct=round(pct, 2),
        )

    return None
