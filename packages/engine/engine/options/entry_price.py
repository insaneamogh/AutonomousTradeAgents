"""The limit price we send for a long-option ENTRY.

Until 2026-09-05 every options entry was submitted at the **full ask**
(``limit_price = ask``, three call sites in the guard and one in the
drafter). Alpaca then marks a long option near the **bid**, so a position
opened at the ask reports a loss equal to the whole spread before the
underlying has moved at all, and the ``-40%`` premium stop measures from
that already-impaired mark. Entry spreads on the real 2026-09-04 book ran
0.1%-5.9%, so this was a 1-3% headwind on every round trip -- small per
trade, structural across a book.

Paying mid + one tick keeps the order marketable (it still crosses most of
the spread) while giving back the half-spread that buying at the ask hands
away for free. A limit that does not fill costs nothing: the entry order
is cancelled by ``stale_entries`` at the next session open, and a missed
entry is not a loss.

**Both the real order and the Refusal Ledger's recorded ``limit_price``
must come from here.** They are the same decision: the ghost prices a
counterfactual "what if we had taken it", and if the real path pays
mid+tick while the ghost records the ask, every ghost is systematically
priced worse than the trade it stands in for and the ledger's own
comparison drifts. That is the CLAUDE.md 4.4 trap -- the same number in
two places -- so there is exactly one function.
"""

from __future__ import annotations

_TICK = 0.01
"""US equity options quote in penny increments at these premiums. One tick
past the mid is the smallest step that is still strictly more aggressive
than the mid itself."""


def entry_limit_price(bid: float | None, ask: float | None) -> float | None:
    """Limit price for a buy-to-open, or None when the contract cannot be
    priced.

    Falls back to ``ask`` whenever the bid is unusable (absent, non-positive,
    or crossed above the ask). That fallback is deliberate: with no credible
    bid there is no mid to improve on, and the previous behaviour -- pay the
    ask -- is the safe one. This function never returns a price ABOVE the
    ask; improving on the mid must never become paying more than we did
    before.
    """
    if ask is None or ask <= 0:
        return None
    if bid is None or bid <= 0 or bid >= ask:
        return ask
    mid = (bid + ask) / 2.0
    return round(min(mid + _TICK, ask), 2)
