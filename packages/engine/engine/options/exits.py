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

A SIBLING LIVES BELOW: ``option_ratchet_signal`` replaces the flat
+60%/-50% take-profit above with a trailing ratchet (arm at a threshold,
give back a fraction of the peak) once ``RiskCaps.options_ratchet_enabled``
is on — the default. ``option_exit_signal`` above is left completely
untouched so the whole ratchet is revertible by flipping that one cap
back off. See the module-level note above ``RatchetOutcome`` for the
state machine itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


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


# ─────────────────────────────────────────────────────────────────────
# The trailing ratchet — a sibling, not a replacement.
#
# WHY THIS EXISTS ALONGSIDE THE FUNCTION ABOVE. The flat take-profit above
# cuts every winner off at the same point regardless of how it got there —
# +60% because of three days of follow-through and +60% because IV spiked
# on one gap exit identically. The user's stated strategy is "hold the
# winners, cut the losers early"; a hard ceiling at +60% does the opposite
# of the first half. This module leaves ``option_exit_signal`` untouched
# on purpose: the entire ratchet is switched on ONE named cap
# (``RiskCaps.options_ratchet_enabled``), so turning it back off reverts
# every open position to the exact behavior above, unconditionally.
#
# THE STATE MACHINE, in the order the rules are checked:
#
#   peak       = max(peak_persisted, pl)        monotone — never decreases
#   armed      = peak >= arm_pct                once armed, always armed
#   trail_line = peak * (1 - giveback_frac)     PROPORTIONAL, not point-giveback
#
#   1. pl <= -stop_loss_pct   -> CLOSE option_stop_loss   never consults
#   2. pl >= hard_tp_pct      -> CLOSE option_take_profit  never consults
#   3. armed and pl <= trail_line
#                             -> CLOSE option_trail_stop   never consults
#   4. armed                  -> HOLD,  may_consult = True
#   5. else                   -> HOLD,  may_consult = False
#
# RULE 1 STAYS FIRST EVEN WHEN RULE 3 ALSO FIRES. A gap from +50 to -60
# satisfies both "below the trail line" and "past the stop." A give-back
# that goes through zero reads more honestly in the ledger as a stop than
# as a trail, so the stop check runs first and returns before the trail
# check is even evaluated.
#
# PROPORTIONAL GIVEBACK, NOT POINT-GIVEBACK. ``trail_line = peak * (1 -
# giveback_frac)``: a peak of +80% draws the line at +56%; a peak of
# +200% draws it at +140%. A point-giveback (``peak - 30``) would give a
# big winner and a small winner the same absolute leash, which is
# backwards for "hold the winners" — and proportional giveback also
# guarantees a trail exit is always realized as a profit (the line only
# exists once ``peak >= arm_pct > 0``).
#
# ``unrealized_pl_pct is None`` -> HOLD, ``may_consult=False``, peak left
# exactly as it was. The existing "no mark never closes" invariant from
# ``option_exit_signal`` survives unweakened: a missing broker read must
# never manufacture a data point, in either direction.
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RatchetOutcome:
    """One tick's verdict from the trailing ratchet.

    ``peak_pl_pct`` is always a concrete number, never ``None`` — a
    position that has never shown a gain simply has a peak of ``0.0``
    (the P&L at entry), which is also why it can never spuriously arm a
    trail on a position that has only ever lost money.
    """

    action: Literal["CLOSE", "HOLD"]
    reason: str | None
    """``option_stop_loss`` | ``option_take_profit`` | ``option_trail_stop``
    when ``action == "CLOSE"``; ``None`` on a HOLD."""

    detail: str
    """Human-readable arithmetic, for the notification and the audit row."""

    pnl_pct: float | None
    """This tick's reading. ``None`` only when the broker reported no mark."""

    peak_pl_pct: float
    """The high-water mark INCLUDING this tick's reading — the value the
    caller persists as ``peak_pl_pct`` for the next tick to read back."""

    trail_line_pct: float | None
    """``None`` until armed — there is no trail line before that."""

    armed: bool
    """Once true for this position, the caller's persisted state keeps it
    true forever (``armed`` only ever turns on, via ``peak`` being
    monotone — this function does not itself remember state across
    calls)."""

    may_consult: bool
    """True only on a HOLD where the trail is armed. The exit agent (a
    separate, later piece of work) is the only thing that reads this —
    it is not used by anything in this module."""

    peak_advanced: bool
    """True when this tick's peak is strictly greater than the
    ``peak_pl_pct`` passed in. The caller persists the new peak ONLY when
    this is true — at a 30s tick cadence across a whole session that is
    the difference between roughly 10 writes and roughly 800."""


def option_ratchet_signal(
    *,
    unrealized_pl_pct: float | None,
    peak_pl_pct: float | None,
    arm_pct: float,
    giveback_frac: float,
    hard_take_profit_pct: float,
    stop_loss_pct: float,
) -> RatchetOutcome:
    """The trailing-ratchet state machine for one open long option, one tick.

    Pure function: no I/O, no clock read, no LLM import — same contract as
    ``option_exit_signal``. The caller (the position manager) owns reading
    the persisted peak in and writing the returned peak back out.

    ``peak_pl_pct`` is the HIGH-WATER MARK PERSISTED FROM THE PREVIOUS TICK,
    not this tick's reading — pass ``None`` when nothing has been persisted
    yet (a fresh position). ``giveback_frac`` is a FRACTION (0.30), not a
    percent — the caller converts ``RiskCaps.options_trail_giveback_pct``
    (30.0) before calling this.

    ``stop_loss_pct`` and ``hard_take_profit_pct`` follow
    ``option_exit_signal``'s own sign convention for continuity: the stop is
    a POSITIVE magnitude (50.0 = "close if the premium has lost half its
    value") and is read via ``abs()`` so a sign typo in an env var cannot
    silently disable it; an explicit ``0`` is the only way to turn a side
    off. ``arm_pct <= 0`` or ``giveback_frac`` outside ``[0, 1)`` are caller
    misconfigurations this function does not guard against — it implements
    the state machine exactly as specified, nothing more defensively.
    """
    prior_peak = float(peak_pl_pct) if peak_pl_pct is not None else 0.0

    if unrealized_pl_pct is None:
        armed = prior_peak >= arm_pct
        trail_line = prior_peak * (1.0 - giveback_frac) if armed else None
        return RatchetOutcome(
            action="HOLD",
            reason=None,
            detail=(
                f"no broker mark this tick; peak left at +{prior_peak:.1f}% "
                "rather than guessing"
            ),
            pnl_pct=None,
            peak_pl_pct=round(prior_peak, 2),
            trail_line_pct=round(trail_line, 2) if trail_line is not None else None,
            armed=armed,
            may_consult=False,
            peak_advanced=False,
        )

    pl = float(unrealized_pl_pct)
    peak = max(prior_peak, pl)
    peak_advanced = peak > prior_peak
    armed = peak >= arm_pct
    trail_line = peak * (1.0 - giveback_frac) if armed else None

    # Rule 1 — hard stop. Checked before the trail even though a gap could
    # satisfy both: see the module-level note on why the stop must win.
    if abs(stop_loss_pct) > 0 and pl <= -abs(stop_loss_pct):
        return RatchetOutcome(
            action="CLOSE",
            reason="option_stop_loss",
            detail=f"premium {pl:.1f}% vs stop -{abs(stop_loss_pct):.1f}%",
            pnl_pct=round(pl, 2),
            peak_pl_pct=round(peak, 2),
            trail_line_pct=round(trail_line, 2) if trail_line is not None else None,
            armed=armed,
            may_consult=False,
            peak_advanced=peak_advanced,
        )

    # Rule 2 — hard take-profit backstop. Set far above the arm point; the
    # trail is expected to fire first on almost every real path. This rule
    # exists for the case where the trail somehow never catches up (a
    # single-tick gap from below the arm point straight past this ceiling).
    if hard_take_profit_pct > 0 and pl >= hard_take_profit_pct:
        return RatchetOutcome(
            action="CLOSE",
            reason="option_take_profit",
            detail=(
                f"premium +{pl:.1f}% vs hard take-profit "
                f"+{hard_take_profit_pct:.1f}%"
            ),
            pnl_pct=round(pl, 2),
            peak_pl_pct=round(peak, 2),
            trail_line_pct=round(trail_line, 2) if trail_line is not None else None,
            armed=armed,
            may_consult=False,
            peak_advanced=peak_advanced,
        )

    # Rule 3 — the trail itself. Only reachable once armed; ``trail_line``
    # is ``None`` before that; so this rule cannot fire early even if a
    # future edit dropped the explicit ``armed`` guard.
    if armed and trail_line is not None and pl <= trail_line:
        return RatchetOutcome(
            action="CLOSE",
            reason="option_trail_stop",
            detail=(
                f"premium {pl:.1f}% retraced to the trail line "
                f"+{trail_line:.1f}% (peak +{peak:.1f}%, "
                f"{giveback_frac * 100:.0f}% giveback)"
            ),
            pnl_pct=round(pl, 2),
            peak_pl_pct=round(peak, 2),
            trail_line_pct=round(trail_line, 2),
            armed=armed,
            may_consult=False,
            peak_advanced=peak_advanced,
        )

    # Rules 4/5 — hold. ``may_consult`` is True only once armed; the exit
    # agent has nothing useful to reason about on a position that has
    # never reached the arm threshold.
    detail = f"premium +{pl:.1f}%, peak +{peak:.1f}%"
    detail += (
        f", trail line +{trail_line:.1f}%" if trail_line is not None else ", not yet armed"
    )
    return RatchetOutcome(
        action="HOLD",
        reason=None,
        detail=detail,
        pnl_pct=round(pl, 2),
        peak_pl_pct=round(peak, 2),
        trail_line_pct=round(trail_line, 2) if trail_line is not None else None,
        armed=armed,
        may_consult=armed,
        peak_advanced=peak_advanced,
    )
