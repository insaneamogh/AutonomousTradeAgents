"""Broker-side protective stop levels for a single long option leg.

Pure arithmetic — no I/O, no clock, no broker import — mirroring the
contract of ``engine.options.exits``. The caller (the API's
``option_stops`` service) owns placing, replacing and cancelling the
actual order; this module only answers "at what premium should the
resting stop sit right now?".

WHY THIS EXISTS
---------------
Until now the options stop lived ONLY in our own 30-second polling loop
(``position_manager``), because Alpaca cannot bracket a single-leg
option — ``OrderClass`` allows only ``simple``/``mleg`` for
``us_option``. That is still true, and it is why this is a SEPARATE,
standalone stop order rather than a bracket child.

What changed is the order TYPE. Alpaca's options-trading docs (read
2026-09-02, page last modified 2026-04-02) state that for an options
order ``type`` must be ``market``, ``limit``, ``stop`` or ``stop_limit``,
and that **``stop`` and ``stop_limit`` are available for single-leg
orders**. So a resting protective stop IS placeable today even though a
bracket is not, and the previous "no broker-side stop is structurally
possible" conclusion is simply out of date.

This does NOT make the software stop redundant, and it is not a fix for
the -$1,200 CME loss on its own. A stop — ours or the broker's — fires on
a PRINT. When ``CME261016P00270000`` went from -26% to -52% in a single
print, a resting broker stop at -35% would have elected on that same
print and filled at the same bad level. What a broker-side stop actually
buys is the failure mode our polling loop cannot cover at all:

  - our process being down, redeployed, or rate-limited;
  - ``_option_pl_pct_by_symbol`` returning ``{}`` on a broker read error,
    which holds every option un-stopped for that tick;
  - the 30-second gap between ticks;
  - the whole weekend, if a position is held over one.

Gap risk is addressed separately, at entry, by the chain-depth gate and
by liquidity-relative sizing — not here.

SIGN CONVENTION
---------------
``stop_loss_pct`` is a POSITIVE magnitude, matching
``engine.options.exits`` (50.0 = "stop once the premium has lost half its
value") and read via ``abs()`` so a sign typo in an env var cannot
silently invert the level. ``trail_line_pct`` is a SIGNED P&L percent
straight off ``RatchetOutcome.trail_line_pct`` — it can legitimately be
negative (a trail armed at +35% that gave back to -5%) or positive.
"""

from __future__ import annotations

from dataclasses import dataclass

# US option quoting increments. Standard across listed equity options:
# a penny below $3.00, a nickel at or above it. Submitting a level off
# these increments is rejected by the venue, so every price this module
# emits is snapped to one.
_PENNY_TICK_BELOW = 3.00
_PENNY = 0.01
_NICKEL = 0.05

# Below this premium a protective stop is not worth placing: the tick
# grid is coarser than the remaining value, and a stop-limit whose limit
# rounds to 0.00 can never fill. The software stop and the expiry sweep
# still cover these.
_MIN_STOPPABLE_PREMIUM = 0.05


@dataclass(frozen=True)
class ProtectiveStopLevels:
    """Where the resting stop-limit should sit, in premium dollars."""

    stop_price: float
    """Trigger. The order elects when the contract prints at or below it."""

    limit_price: float
    """Floor on the fill once elected. Always < ``stop_price``."""

    basis_pl_pct: float
    """The signed P&L percent this level encodes — the tighter of the fixed
    stop and the ratchet's trail line. Persisted so a later tick can tell
    whether the level actually moved before spending a broker round trip."""

    from_trail: bool
    """True when the ratchet's trail line (not the fixed stop) set the
    level. Purely for logging and the audit row."""


def round_to_option_tick(price: float, *, mode: str = "down") -> float:
    """Snap a premium to the venue's quoting increment.

    ``mode`` is ``"down"`` everywhere in this module, deliberately. For a
    SELL stop, rounding down moves the trigger slightly further away
    (fires marginally later, never on a tick of rounding noise) and moves
    the limit slightly lower (marginally more likely to fill). Both are
    the permissive direction: the failure this module must avoid is a
    stop that gets REJECTED or never fills, leaving the position with no
    broker-side protection at all while the audit row claims it has one.
    """
    if price <= 0:
        return 0.0
    tick = _PENNY if price < _PENNY_TICK_BELOW else _NICKEL
    # Integer arithmetic on the tick grid — floating-point division here
    # produces 2.9499999 for a clean 2.95 often enough to matter.
    steps = int(round(price / tick, 6))
    if mode == "down" and steps * tick > price + 1e-9:
        steps -= 1
    return round(steps * tick, 2)


def protective_stop_levels(
    *,
    entry_premium: float,
    stop_loss_pct: float,
    slippage_pct: float,
    trail_line_pct: float | None = None,
) -> ProtectiveStopLevels | None:
    """The level a resting broker stop should hold for this position.

    Returns ``None`` when no stop should rest at the broker — an
    unusable entry premium, a stop configured to zero (the documented way
    to disable a side), or a level that rounds into the sub-nickel dust
    where a stop-limit could never fill. ``None`` is not a failure: the
    software stop in ``position_manager`` is unaffected and still runs.

    The level is the TIGHTER of two candidates, which is what makes
    re-placing it safe to do repeatedly:

      * the fixed stop, ``-abs(stop_loss_pct)``;
      * the ratchet's ``trail_line_pct``, once armed.

    Because ``RatchetOutcome``'s peak is monotone, its trail line only
    ever rises, so the value this returns is monotone too. A caller that
    cancel-replaces whenever the level rises can never loosen protection
    it already has — the same one-way property the in-process ratchet
    relies on.
    """
    if entry_premium <= 0:
        return None
    stop_magnitude = abs(stop_loss_pct)

    basis: float | None = None
    from_trail = False
    if stop_magnitude > 0:
        basis = -stop_magnitude
    if trail_line_pct is not None and (basis is None or trail_line_pct > basis):
        basis = trail_line_pct
        from_trail = True
    if basis is None:
        # Fixed stop explicitly disabled (0) and the trail not yet armed.
        return None

    stop_price = round_to_option_tick(entry_premium * (1.0 + basis / 100.0))
    if stop_price < _MIN_STOPPABLE_PREMIUM:
        return None

    # A stop-MARKET on a thin contract can fill anywhere; the repo's
    # standing rule (docs/OPTIONS_PLAN.md) is never to send a market
    # order on an option priced off a delayed indicative feed. So this is
    # a stop-LIMIT, and the slippage band is how much worse than the
    # trigger we will still accept.
    limit_price = round_to_option_tick(stop_price * (1.0 - abs(slippage_pct) / 100.0))
    if limit_price <= 0:
        # Band wider than the premium itself. Fall back to one tick under
        # the trigger rather than dropping protection entirely.
        tick = _PENNY if stop_price < _PENNY_TICK_BELOW else _NICKEL
        limit_price = round(max(stop_price - tick, _PENNY), 2)
    if limit_price >= stop_price:
        # Can happen when both snap to the same tick on a cheap contract.
        tick = _PENNY if stop_price < _PENNY_TICK_BELOW else _NICKEL
        limit_price = round(max(stop_price - tick, _PENNY), 2)
        if limit_price >= stop_price:
            return None

    return ProtectiveStopLevels(
        stop_price=stop_price,
        limit_price=limit_price,
        basis_pl_pct=basis,
        from_trail=from_trail,
    )


def should_replace(
    *, current_basis_pl_pct: float | None, new_basis_pl_pct: float, min_step_pct: float
) -> bool:
    """Is the new level far enough above the resting one to be worth a
    cancel-replace round trip?

    Monotone by construction: a level at or below what is already resting
    is never replaced, so a transient bad mark cannot loosen a stop that
    the ratchet already tightened. ``min_step_pct`` suppresses the churn
    of cancelling and re-placing an order to move it a few cents — each
    replace is two broker calls and a window (however brief) in which the
    position has NO resting stop at all.
    """
    if current_basis_pl_pct is None:
        return True
    return new_basis_pl_pct - current_basis_pl_pct >= abs(min_step_pct)
