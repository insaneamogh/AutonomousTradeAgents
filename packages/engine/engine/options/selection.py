"""Deterministic contract selection — thesis + chain snapshot → one contract.

``docs/OPTIONS_PLAN.md`` §2.2 is the spec this implements. Filter-then-
tiebreak, no LLM, no I/O: every input arrives already fetched
(``ContractSelectionInputs.candidates``) and every output is either exactly
one contract or a named HOLD reason plus the funnel counts that produced it.
Never falls back to an equity path — that decision belongs to the caller
(the Drafter), and it is a HOLD, not a substitution.

Filter stages, in order (each stage's surviving count is recorded in
``ContractSelectionResult.funnel_counts`` for audit, and the FIRST stage
whose count drops to zero names the rejection reason):

  1. ``contract_type`` — a "long" (bullish) thesis wants a CALL; a "short"
     (bearish) thesis wants a PUT. Phase A never sells anything to open
     (see ``engine.risk.types.OptionLegDetails.action`` — always
     ``buy_to_open``); "short" here is only about which contract TYPE
     expresses the bearish view, never about opening a short option leg.
  2. ``dte_window`` — 7-60 DTE is the risk engine's OUTER guardrail
     (``RiskCaps.options_min_dte``/``options_max_dte``, deliberately wider,
     re-checked independently at approval time). This module's own window
     is narrower and for a different reason: 10-45 DTE, deliberately NOT
     0-7 DTE, because theta decay dominates and greeks are frequently
     missing close to expiry, and deliberately not run all the way to 60,
     to leave room above the selection window for the position to roll
     before it re-enters the risk engine's own boundary. The two numbers
     are intentionally NOT the same constant reused twice — one is a
     selection heuristic, the other is an authoritative veto — so this
     module hardcodes its own 10/45, it does not import ``RiskCaps``.

     The floor is 10 rather than the risk engine's own 7 on purpose:
     ``expiry.dte()`` reads ``context.now_utc`` while ``select_contract``
     reads its own ``inputs.now``, so a contract chosen at exactly 7 DTE
     can re-enter the risk engine at 6 across a UTC boundary and be vetoed
     by ``min_dte`` — refused by the layer that just selected it. Three
     days of buffer removes that class of failure entirely.
  3. ``delta_band`` — higher conviction buys closer to the money (higher
     |delta|, more directional exposure per contract); lower conviction
     buys further OTM (lower |delta|, cheaper, more convex). The exact
     bands (conviction >= 0.7 -> |delta| in [0.35, 0.75]; below that ->
     [0.25, 0.65]) are a deliberately simple two-tier judgment call, not a
     continuous function — the task this implements asked for "a
     reasonable banding", not a research result, and a two-tier band is
     easy to audit from a funnel count in a way a continuous formula is
     not. Widened once more 2026-08-30 for the contest window (docs/
     PLAN_AGGRESSIVE_PROFILE.md §2 — "more delta per premium dollar; upper
     strikes are also the more liquid near-ATM ones") and then FROZEN —
     ``docs/HACKATHON.md`` §8 does not permit changing these constants
     again after Monday's open, so funnel counts stay comparable across
     the contest's trading days. A candidate with no reported delta fails
     this stage: the same "a hard filter cannot verify what it cannot see"
     logic as ``iv_present`` below, just not singled out in the module
     docstring because — unlike IV — nothing else in this codebase leads a
     reader to expect a missing delta to pass neutrally.
  4. ``liquidity`` — reject `open_interest < 100`, `volume < 1`, or (when
     both bid and ask are present) relative spread `(ask-bid)/mid > 12%`.
     Open interest is the REAL gate here. ``volume`` carries a floor of
     only 1 ("it has traded at all") because it is NOT daily volume:
     alpaca-py's ``OptionsSnapshot`` drops the ``dailyBar`` block, so the
     available number is the LAST TRADE SIZE — one print, typically 1-5
     lots. Measured live, a floor of 10 against that field rejected 16 of
     18 SPY contracts that had already cleared DTE, delta and IV. The
     spread ceiling is 12% rather than 8% for a related reason: the free
     tier serves a 15-minute-delayed indicative book, which reads wider
     than the one an order would actually fill against.
     Missing OI/volume fails the floor (can't prove liquidity you can't
     see); a missing bid or ask only skips the spread arm of this check
     (there is no mid to compute it from) — OI/volume still apply on
     their own. These numbers intentionally match
     ``RiskCaps.options_min_open_interest`` / ``options_min_volume`` /
     ``options_max_relative_spread_pct`` defaults, because they are the
     same real-world floor; they are hardcoded here rather than imported
     because ``select_contract``'s signature (fixed by spec) takes only
     ``ContractSelectionInputs`` — the risk engine's copy remains the
     authoritative, independently re-verified enforcement point.
  5. ``iv_present`` — missing IV is an outright REJECTION at this stage,
     not a neutral pass-through. This is a DELIBERATE divergence from
     ``trading_agents.strategies.fit``'s "missing feature -> NEUTRAL"
     convention (see that module's ``_Features``/``_ramp`` — a missing
     input there scores 0.5, it never disqualifies). An options Phase-A
     entry is different: buying a contract this system cannot price is not
     a neutral unknown, it is a decision to enter blind, and this module
     refuses to make that decision.
  6. ``iv_realized_vol_band`` — the other half of ``docs/OPTIONS_PLAN.md``
     §2.2's IV-sanity criterion ("outside a plausible band vs the
     underlying's own realised vol"), no longer deferred now that
     ``ContractSelectionInputs.realized_vol_pct`` carries it. Buying rich
     IV into a quiet underlying is a bad trade even when the direction is
     right. Unlike ``iv_present``, a MISSING comparator (``realized_vol_pct
     is None``) is a fact about the analysis environment, not about the
     contract, and is a neutral pass here — matching
     ``trading_agents.strategies.fit``'s general missing-input convention,
     not ``iv_present``'s own stricter one. **Unit landmine, worth stating
     plainly**: ``ContractQuote.implied_volatility`` is a decimal fraction
     (e.g. ``0.28``) while ``realized_vol_pct`` is already in percent units
     (e.g. ``25.0``) — the comparison multiplies IV by 100 first. Getting
     this wrong makes every real contract look ~100x mispriced and would
     silently re-disable this stage the same way the chain-fetch bug this
     package's sibling commits fixed did, just one filter later.

Tie-break among whatever survives all six stages: tightest relative
spread first, then highest open interest. A candidate with no bid/ask (so
no computable spread) sorts last on the spread key — never preferred over
one with a verified tight market.

``ContractSelectionInputs.days_to_earnings`` is NOT a filter input here —
nothing in the five stages above reads it. It exists on this dataclass
purely so ``select_contract`` (the only place ``OptionLegDetails`` gets
constructed) can copy it straight into the ``OptionLegDetails`` it returns,
for the ``earnings_blackout`` risk rule to re-check later. The value
itself is computed once by the ``options_context`` feature block and
carried in by the Drafter — see ``trading_agents.nodes.drafter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from engine.risk.types import OptionLegDetails

Direction = Literal["long", "short"]
ContractType = Literal["call", "put"]

# ── Selection-specific DTE window (see module docstring §2 for why this is
# NOT RiskCaps.options_min_dte/options_max_dte) ────────────────────────────
# Floor is 10, not the 7 of ``RiskCaps.options_min_dte``: ``min_dte`` measures
# from ``context.now_utc`` while selection measures from its own
# ``datetime.now(UTC)``, so a UTC-boundary crossing between the two gives an
# off-by-one veto at exactly 7. 10 leaves a 3-day buffer, and still clears the
# ``options_expiry_sweep_dte = 2`` sweep for a contract opened late in the week.
_DTE_MIN = 10
_DTE_MAX = 45

# ── Delta bands by conviction (see module docstring §3) ───────────────────
# The bands OVERLAP deliberately. They used to be disjoint at 0.45, which made
# a delta-0.50 contract — at the money, and therefore the deepest open interest
# and tightest book on the board — reachable only when the Drafter's confidence
# cleared 0.7. It usually doesn't, so the single most liquid strike was the one
# routinely excluded. Conviction still widens the band; neither tier can now
# exclude ATM.
#
# Widened once more 2026-08-30 (was [0.40,0.70]/[0.25,0.55]) for the contest
# window — docs/PLAN_AGGRESSIVE_PROFILE.md §2: more delta per premium dollar,
# and the upper strikes this reaches are also the more liquid near-ATM ones.
# FROZEN after this: docs/HACKATHON.md §8 does not permit touching these
# constants again once Monday's open has happened, so funnel counts stay
# comparable day over day across the contest window.
_HIGH_CONVICTION_THRESHOLD = 0.7
_HIGH_CONVICTION_DELTA_BAND = (0.35, 0.75)
_LOW_CONVICTION_DELTA_BAND = (0.25, 0.65)

# ── Liquidity floor (mirrors RiskCaps.options_* defaults — see module
# docstring §4 for why they are hardcoded here rather than imported) ──────
_MIN_OPEN_INTEREST = 100
# NOT a daily-volume floor. ``ContractQuote.volume`` is populated from the
# snapshot's LAST TRADE SIZE (see contracts.py) — the size of one print,
# typically 1-5 lots — because the alpaca-py ``OptionsSnapshot`` model drops
# the ``dailyBar`` block that carries real volume. Measured against the live
# SPY chain, a floor of 10 rejected 16 of 18 contracts that had already
# cleared DTE, delta and IV; real daily volume would have kept all 18. So this
# now means only "the contract has traded at all", and open interest (which IS
# real, from /v2/options/contracts) carries the liquidity judgment.
_MIN_VOLUME = 1
# Widened from 8.0: the free tier's indicative feed is 15 minutes delayed, so
# the quoted book reads wider than the one you would actually fill against.
_MAX_RELATIVE_SPREAD_PCT = 12.0

# ── IV-vs-realized-vol plausibility band (see module docstring §6) ───────
# Provisional judgment calls, not derived from data — this is paper-only
# (TRADING_MODE=paper) for the foreseeable future; revisit before any real
# money depends on this stage.
_IV_REALIZED_VOL_FLOOR_MULT = 0.3
_IV_REALIZED_VOL_CEIL_MULT = 3.0

# Named reasons, in filter order — the FIRST of these whose stage emptied
# the funnel is the rejection reason. Order matters: it is also the order
# the stages actually run in.
_STAGE_REJECTION_REASONS: dict[str, str] = {
    "contract_type": "no_matching_contract_type",
    "dte_window": "no_expiry_in_window",
    "delta_band": "no_delta_in_band",
    "liquidity": "no_liquid_contract",
    "iv_present": "no_iv",
    "iv_realized_vol_band": "iv_outside_plausible_band",
}


@dataclass(frozen=True)
class ContractQuote:
    """One candidate contract's market snapshot — whatever the chain-fetch
    step (``trading_agents.nodes.drafter``'s chain adapter, pending the
    options broker track) got back for one strike/expiry combination."""

    occ_symbol: str
    contract_type: ContractType
    strike: float
    expiry: date
    bid: float | None
    ask: float | None
    open_interest: int | None
    volume: int | None
    delta: float | None
    implied_volatility: float | None


@dataclass(frozen=True)
class ContractSelectionInputs:
    underlying_symbol: str
    direction: Direction
    """"short" here means a PUT is wanted for a bearish thesis — Phase A is
    still long-only on the OPTION itself (buy_to_open); this is about which
    contract TYPE to buy, never about opening a short option leg."""
    conviction: float
    """0..1, from the council's confidence."""
    candidates: tuple[ContractQuote, ...]
    now: datetime
    """Injectable clock, matching this repo's ``now_utc``-injection
    convention elsewhere (e.g. ``engine.risk.rules.mis_square_off``) — this
    function never calls ``datetime.now()`` itself."""
    days_to_earnings: int | None = None
    realized_vol_pct: float | None = None
    """Underlying's own realized vol, ANNUALIZED PERCENT units (e.g. 25.0
    == 25%) — same convention as ``engine.features.quant.compute_quant``'s
    ``realized_vol_pct`` (lives under ``ctx["quant"]``, not
    ``ctx["options_context"]`` — a distinct dict). ``None`` skips the
    ``iv_realized_vol_band`` stage (neutral pass) — see module docstring
    §6 for why this differs from ``iv_present``'s own stricter handling of
    a missing IV."""


@dataclass(frozen=True)
class ContractSelectionResult:
    selected: OptionLegDetails | None
    """None = no viable contract; HOLD with ``rejection_reason`` named."""
    rejection_reason: str | None = None
    """Set when ``selected`` is None — one of the named reasons in
    ``_STAGE_REJECTION_REASONS`` (or ``"no_candidates"`` when the chain
    fetch returned nothing at all). Never a bare "failed"."""
    funnel_counts: dict[str, int] = field(default_factory=dict)
    """Candidates remaining after each filter stage, keyed by stage name,
    for auditability — e.g. ``{"total": 40, "contract_type": 22,
    "dte_window": 12, "delta_band": 5, "liquidity": 2, "iv_present": 2}``."""


def _dte(expiry: date, now: datetime) -> int:
    return (expiry - now.date()).days


def _delta_band(conviction: float) -> tuple[float, float]:
    if conviction >= _HIGH_CONVICTION_THRESHOLD:
        return _HIGH_CONVICTION_DELTA_BAND
    return _LOW_CONVICTION_DELTA_BAND


def _relative_spread_pct(bid: float, ask: float) -> float | None:
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100.0


def _iv_within_plausible_band(iv: float, realized_vol_pct: float) -> bool:
    """``iv`` is a decimal fraction (e.g. 0.28); ``realized_vol_pct`` is
    already in percent units (e.g. 25.0) — see module docstring §6's
    "unit landmine" note for why the ``* 100.0`` below is not optional."""
    if realized_vol_pct <= 0:
        return True  # nothing sane to compare against — don't reject on a degenerate input
    ratio = (iv * 100.0) / realized_vol_pct
    return _IV_REALIZED_VOL_FLOOR_MULT <= ratio <= _IV_REALIZED_VOL_CEIL_MULT


def _passes_liquidity(quote: ContractQuote) -> bool:
    if quote.open_interest is None or quote.open_interest < _MIN_OPEN_INTEREST:
        return False
    # Guarded on ``> 0`` so the floor can be switched off entirely. Without
    # it, setting _MIN_VOLUME to 0 would still hard-fail every contract whose
    # volume is None, which is the common case on a thin last-trade proxy.
    if _MIN_VOLUME > 0 and (quote.volume is None or quote.volume < _MIN_VOLUME):
        return False
    if quote.bid is not None and quote.ask is not None:
        spread_pct = _relative_spread_pct(quote.bid, quote.ask)
        if spread_pct is None or spread_pct > _MAX_RELATIVE_SPREAD_PCT:
            return False
    return True


def _tie_break_key(quote: ContractQuote) -> tuple[float, int]:
    """Tightest relative spread first, then highest OI.

    A missing bid/ask (no computable spread) sorts as worst (``inf``) —
    never preferred over a candidate with a verified tight market. OI sorts
    descending, so it is negated (the key as a whole sorts ascending).
    """
    spread_pct = (
        _relative_spread_pct(quote.bid, quote.ask)
        if quote.bid is not None and quote.ask is not None
        else None
    )
    oi = quote.open_interest or 0
    return (spread_pct if spread_pct is not None else float("inf"), -oi)


def _tie_break(candidates: list[ContractQuote]) -> ContractQuote:
    return min(candidates, key=_tie_break_key)


def select_contract(inputs: ContractSelectionInputs) -> ContractSelectionResult:
    """Filter ``inputs.candidates`` down to one contract, or a named HOLD.

    Pure function of its inputs — no I/O, no wall-clock read (``inputs.now``
    is the clock). See the module docstring for the five filter stages and
    the tie-break.
    """
    funnel: dict[str, int] = {"total": len(inputs.candidates)}
    remaining: list[ContractQuote] = list(inputs.candidates)

    if not remaining:
        return ContractSelectionResult(
            selected=None, rejection_reason="no_candidates", funnel_counts=funnel
        )

    wanted_type: ContractType = "call" if inputs.direction == "long" else "put"
    remaining = [c for c in remaining if c.contract_type == wanted_type]
    funnel["contract_type"] = len(remaining)

    if remaining:
        remaining = [
            c for c in remaining if _DTE_MIN <= _dte(c.expiry, inputs.now) <= _DTE_MAX
        ]
    funnel["dte_window"] = len(remaining)

    if remaining:
        lo, hi = _delta_band(inputs.conviction)
        remaining = [
            c for c in remaining if c.delta is not None and lo <= abs(c.delta) <= hi
        ]
    funnel["delta_band"] = len(remaining)

    if remaining:
        remaining = [c for c in remaining if _passes_liquidity(c)]
    funnel["liquidity"] = len(remaining)

    if remaining:
        remaining = [c for c in remaining if c.implied_volatility is not None]
    funnel["iv_present"] = len(remaining)

    if remaining and inputs.realized_vol_pct is not None:
        realized_vol_pct = inputs.realized_vol_pct
        remaining = [
            c
            for c in remaining
            if c.implied_volatility is not None
            and _iv_within_plausible_band(c.implied_volatility, realized_vol_pct)
        ]
    funnel["iv_realized_vol_band"] = len(remaining)

    if not remaining:
        for stage, reason in _STAGE_REJECTION_REASONS.items():
            if funnel.get(stage) == 0:
                return ContractSelectionResult(
                    selected=None, rejection_reason=reason, funnel_counts=funnel
                )
        # Unreachable given the stages above always run in this fixed order
        # and each is recorded — but an empty result must never surface
        # with no name attached, so fall back to the most common cause.
        return ContractSelectionResult(
            selected=None, rejection_reason="no_liquid_contract", funnel_counts=funnel
        )

    winner = _tie_break(remaining)
    selected = OptionLegDetails(
        underlying_symbol=inputs.underlying_symbol,
        occ_symbol=winner.occ_symbol,
        contract_type=winner.contract_type,
        strike=winner.strike,
        expiry=winner.expiry,
        multiplier=100,
        action="buy_to_open",
        open_interest=winner.open_interest,
        volume=winner.volume,
        bid=winner.bid,
        ask=winner.ask,
        implied_volatility=winner.implied_volatility,
        days_to_earnings=inputs.days_to_earnings,
    )
    return ContractSelectionResult(selected=selected, rejection_reason=None, funnel_counts=funnel)
