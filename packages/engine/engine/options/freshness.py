"""Is this option quote still worth trading on?

THE PROBLEM THIS EXISTS FOR
---------------------------
An option's value changes second to second, and the path from "we saw a
price" to "we sent an order" is not instant:

    fetch chain  ->  select contract  ->  size it
                 ->  Bull/Bear debate (two model calls, seconds)
                 ->  trade hop (another model call)
                 ->  guard re-runs the risk stack
                 ->  order goes to the broker

Everything downstream of the fetch is reasoning about a price that is
already in the past. On a $4.60 contract with a 12% permitted spread, a
few seconds of real movement is a larger number than the edge most of
these setups claim.

Until now nothing measured this. ``ContractQuote`` carried bid, ask,
open interest, volume, delta and IV — and **no timestamp at all** — so no
code downstream could have applied a staleness rule even if it wanted to.
The equity path has had ``MAX_QUOTE_AGE_SECONDS`` in
``engine.features.microstructure`` since it was written; the options
path, where the instrument decays and the spreads are ten times wider,
had nothing.

WHAT THE PRICE-DRIFT PROBLEM TURNED OUT TO BE
---------------------------------------------
The obvious worry — "the ask moved while the agents debated, so we pay
more than we sized for" — was investigated on 2026-09-02 and **is already
structurally impossible**. Every option entry goes out as a LIMIT at the
guard-selected price (``options/tools/trade.py``: ``OrderType.LIMIT``,
``limit_price`` straight off the guard payload), and a limit order cannot
fill above its limit. Re-fetching a quote to re-check that would have
been a wasted API call guarding a bug that does not exist. It was built,
verified redundant, and removed rather than shipped.

What is NOT protected by the limit order, and is what this module exists
for: **the contract CHOICE**. Selection reads delta, implied volatility
and spread off the same snapshot. A stale snapshot yields stale greeks,
and stale greeks pick the wrong strike — the limit price then faithfully
protects the price of a contract we should never have selected. Paying
exactly what you budgeted for the wrong instrument is not protection.

So this module answers ONE question, and applies it early: is this quote
recent enough that the numbers we are about to select on still describe
the contract? Pure function — no clock read, no I/O, no broker import;
the caller passes ``now``, exactly like ``engine.options.exits`` and
``engine.options.protective_stop``.

WHY REFUSING IS THE RIGHT DEFAULT
---------------------------------
A stale quote is refused, not adjusted. Re-sizing on the new
price sounds helpful and is a trap: the whole decision — direction,
conviction, contract choice, the premium budget it fit inside — was made
against the old number. Silently re-pricing keeps the decision and
changes its basis, which is how you end up holding something nobody
actually chose. Refuse, name the reason, and let the next pass decide
fresh against a price it can see.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MAX_QUOTE_AGE_SECONDS = 300.0
"""Refuse to SIZE on an option quote older than five minutes.

Much tighter than the equity path's 1800s, and deliberately so. An
equity quote thirty minutes old still describes roughly the right price
for a swing entry in a name that moves a few percent a day. An option is
a decaying, leveraged claim quoted with a spread we permit up to 12% —
half an hour is several lifetimes of the thing we are pricing.

Five minutes is a REASONED bound, not a measured one: it is roughly the
scan interval, so a quote from the previous sweep is refused and one from
this sweep is not. It has NOT been calibrated against how fast these
specific contracts actually move — measuring that needs live quote
history and is the honest next step.

Note the interaction with Alpaca's free-tier 15-minute delayed feed: if
the feed itself is delayed, EVERY quote is older than this and the gate
refuses everything. That is the correct behaviour for a gate that means
"do not size on prices you cannot trust" — but it means this number and
the account's data entitlement have to be set together. See
``OPTIONS_MAX_QUOTE_AGE_SECONDS`` to relax it, and read
``docs/OPTIONS_QUOTE_FRESHNESS.md`` before you do.
"""


@dataclass(frozen=True)
class FreshnessVerdict:
    """Whether a quote may be traded on, and why not when it may not."""

    ok: bool
    reason: str | None
    """``stale_quote`` on refusal, ``None`` otherwise. A NAMED reason, so
    it lands in the Refusal Ledger like every other deterministic veto
    rather than as a bare False."""

    detail: str
    """Human-readable arithmetic for the audit row and the log line."""

    age_seconds: float | None = None


def quote_freshness(
    *,
    quote_ts: datetime | None,
    now: datetime,
    max_age_seconds: float = MAX_QUOTE_AGE_SECONDS,
) -> FreshnessVerdict:
    """Is this quote recent enough to size a position on?

    ``quote_ts is None`` — the feed gave us no timestamp — is treated as
    UNKNOWN, not as fresh, and refused. This is the one place where
    failing open would be actively dangerous: an unknown age is exactly
    the case where a stale price is most likely and least detectable, and
    "we could not tell how old it was" is not a reason to trade on it.
    That is the opposite of the fail-open convention used elsewhere in
    this codebase (the pre-flights, the CLI wrapper), and the difference
    is that those degrade to a SLOWER path while this one would degrade
    to a WRONG price.

    A negative age (a timestamp in the future, i.e. clock skew between us
    and the venue) is also refused: it means the two clocks disagree, so
    no age computed from them can be trusted in either direction.
    """
    if quote_ts is None:
        return FreshnessVerdict(
            ok=False,
            reason="stale_quote",
            detail="quote carried no timestamp — age unknown, refusing to size on it",
            age_seconds=None,
        )

    age = (now - quote_ts).total_seconds()
    if age < 0:
        return FreshnessVerdict(
            ok=False,
            reason="stale_quote",
            detail=(
                f"quote timestamp is {abs(age):.1f}s in the FUTURE — clock skew "
                "between us and the venue, so no age is trustworthy"
            ),
            age_seconds=age,
        )
    if age > max_age_seconds:
        return FreshnessVerdict(
            ok=False,
            reason="stale_quote",
            detail=f"quote is {age:.1f}s old, limit {max_age_seconds:.0f}s",
            age_seconds=age,
        )
    return FreshnessVerdict(
        ok=True,
        reason=None,
        detail=f"quote {age:.1f}s old, inside the {max_age_seconds:.0f}s limit",
        age_seconds=age,
    )
