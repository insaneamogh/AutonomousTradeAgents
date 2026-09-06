"""Per-underlying and per-direction concentration caps for the options book.

Why these exist as SEPARATE rules rather than a widened
``max_total_premium_pct``: that rule bounds the whole book and is
outcome-blind about its shape. A book can sit comfortably inside it and
still be one bet. On 2026-09-04 the live book was::

    NVDA  4 contracts across 230C/235C/245C, 3 expiries   $4,840  (5.0% eq)
    GILD  2 contracts across 150C/155C                    $2,285  (2.3% eq)
    ...
    long calls $10,060  vs  long puts $2,513              (80% one side)

Every aggregate cap passed. The equity path would have caught the first
half of that (``engine.risk.rules.single_name_concentration``), but
``engine.risk.engine.evaluate`` early-returns into ``evaluate_option`` for
option proposals and that sequence never re-added it — so the options book
had no single-name control of any kind.

**The OCC trap.** ``single_name_concentration`` compares
``p.symbol == proposal.symbol``. On the options path ``symbol`` is the OCC
string, so ``NVDA261002C00230000`` and ``NVDA261009C00245000`` are two
different "names" and the rule can never fire however it is configured.
Grouping has to happen on the underlying root, and for positions already
held that root only exists inside the OCC — ``PortfolioPosition`` carries
no ``underlying`` field. Hence ``occ_root`` below, which is the ONE place
in this package that takes an OCC apart.
"""

from __future__ import annotations

from engine.risk.types import (
    RiskCaps,
    RiskContext,
    RiskDecision,
    RiskProposal,
)

_MIN_BOOK_FOR_DIRECTION_CAP = 3
"""``options_direction_cap`` does not bind until this many option positions
are already open.

A percentage-of-book ratio is meaningless on a book too small to have one.
With an empty book the first position is 100% of its own side, with two
positions any split is 50% or 100% — so a 65% cap applied from the first
trade refuses *every* trade and the desk never opens a position. That is
not a hypothetical: it was the first behaviour this rule produced, and the
replay against the real 2026-09-04 book admitted 0 of 11.

Three is chosen against the book this profile can actually hold:
``options_max_total_premium_pct`` (11%) over ``options_max_premium_pct``
(1.5%) is ~7 concurrent positions, and 65% of 7 is ~4.5. Letting the first
three land unconstrained and binding from the fourth keeps the cap
meaningful without ever blocking a book from forming."""

_OCC_TAIL = 15
"""An OCC-21 symbol ends in a fixed 15-char tail: YYMMDD (6) + C/P (1) +
strike in thousandths, zero-padded (8). Everything before it is the
underlying root, which is variable length (1-6 chars) — which is exactly
why the root has to be found by measuring from the RIGHT."""


def occ_root(symbol: str) -> str | None:
    """The underlying root of an OCC-21 option symbol, or None if ``symbol``
    is not one (an equity ticker, or anything malformed).

    Returns None rather than guessing: a caller that cannot identify the
    underlying must skip the position, never fold it into some other name's
    bucket. ``NVDA261002C00230000`` -> ``NVDA``.
    """
    if len(symbol) <= _OCC_TAIL:
        return None
    tail = symbol[-_OCC_TAIL:]
    if not tail[:6].isdigit():
        return None
    if tail[6] not in ("C", "P"):
        return None
    if not tail[7:].isdigit():
        return None
    root = symbol[:-_OCC_TAIL]
    return root if root.isalpha() else None


def occ_contract_type(symbol: str) -> str | None:
    """``"call"`` / ``"put"`` for an OCC symbol, else None. Same parse as
    ``occ_root`` — kept beside it so the two can never drift."""
    if occ_root(symbol) is None:
        return None
    return "call" if symbol[-_OCC_TAIL + 6] == "C" else "put"


def _open_option_premium_by_root(context: RiskContext) -> dict[str, float]:
    """Open option premium grouped by underlying root. Positions whose OCC
    will not parse are dropped, not bucketed under a fallback key — see
    ``occ_root``."""
    by_root: dict[str, float] = {}
    for p in context.open_positions:
        if not p.is_option:
            continue
        root = occ_root(p.symbol)
        if root is None:
            continue
        by_root[root] = by_root.get(root, 0.0) + p.market_value
    return by_root


def options_single_underlying_cap(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    """Refuse a BUY that would push total open premium on ONE underlying
    past ``caps.options_max_single_underlying_pct`` of equity."""
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    if context.account_equity <= 0:
        return None  # max_total_premium_pct already fails closed on this

    root = option.underlying_symbol.upper()
    existing = _open_option_premium_by_root(context).get(root, 0.0)
    this_premium = proposal.qty * proposal.last_price * option.multiplier
    combined = existing + this_premium
    pct = (combined / context.account_equity) * 100.0

    if pct <= caps.options_max_single_underlying_pct:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"Open option premium on {root} would reach {pct:.2f}% of equity "
            f"(cap {caps.options_max_single_underlying_pct:.2f}%). "
            f"${existing:,.0f} is already open on this underlying across "
            "every strike and expiry — stacking another contract on the same "
            "name is one conviction sized larger, not a second position."
        ),
        veto_rule="options_single_underlying_cap",
    )


def options_direction_cap(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    """Refuse a BUY that would push the option book past
    ``caps.options_max_direction_pct`` on one side (calls or puts).

    Does not bind until ``_MIN_BOOK_FOR_DIRECTION_CAP`` positions are
    already open — see that constant for why a ratio cap has to have a
    floor on book size to be safe.
    """
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None

    this_premium = proposal.qty * proposal.last_price * option.multiplier
    side_totals = {"call": 0.0, "put": 0.0}
    open_options = 0
    for p in context.open_positions:
        if not p.is_option:
            continue
        kind = occ_contract_type(p.symbol)
        if kind is None:
            continue
        side_totals[kind] += p.market_value
        open_options += 1

    if open_options < _MIN_BOOK_FOR_DIRECTION_CAP:
        return None

    this_side = option.contract_type
    total = side_totals["call"] + side_totals["put"] + this_premium
    if total <= 0:
        return None
    same_side = side_totals[this_side] + this_premium
    pct = (same_side / total) * 100.0

    if pct <= caps.options_max_direction_pct:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"{this_side.capitalize()}s would be {pct:.1f}% of open option "
            f"premium (cap {caps.options_max_direction_pct:.1f}%). A book "
            "this one-sided is a single market view expressed repeatedly — "
            "every position needs the same tape to be right."
        ),
        veto_rule="options_direction_cap",
    )
