"""Risk-engine wire types.

Every wire surface that flows between the agent council, the risk engine,
and the executor is typed here. Pydantic-free on purpose — these are
zero-dep dataclasses so the risk layer stays usable from non-FastAPI
contexts (CLI, backtester, batch jobs).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Literal

from engine.env import env_flag


def _env_int(name: str, default: int) -> int:
    """Env override for a data-quality floor. Malformed input keeps the
    default — a typo must never silently widen a gate."""
    import logging
    import os

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.getLogger("engine.risk").warning(
            "ignoring malformed %s=%r — keeping %r", name, raw, default
        )
        return default


def _env_float(name: str, default: float) -> float:
    """Float twin of ``_env_int``. Same fail-to-default contract."""
    import logging
    import os

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logging.getLogger("engine.risk").warning(
            "ignoring malformed %s=%r — keeping %r", name, raw, default
        )
        return default


_KNOWN_RISK_PROFILES = ("conservative", "aggressive_paper")


def _select_risk_profile(raw: str) -> str:
    """Env override for which REVIEWED ``RiskCaps`` profile applies.

    Same fail-to-default contract as ``_env_int``/``_env_float``: an unset
    or unrecognised value keeps ``"conservative"`` — a typo in a Railway
    variable must never silently select the wider profile.

    This selects between two profiles that are themselves reviewed, in
    git, and diffable (a bare ``RiskCaps()`` vs. ``RiskCaps.aggressive_paper()``)
    — it never supplies a widened number directly. See
    ``docs/PLAN_AGGRESSIVE_PROFILE.md`` §3 for why that distinction is the
    whole point: a risk cap that can be widened by an env var nobody
    reviews is not a risk cap, but an env var that only ever picks between
    two reviewed profiles cannot express a number nobody looked at.
    """
    import logging

    name = raw.strip().lower()
    if not name:
        return "conservative"
    if name in _KNOWN_RISK_PROFILES:
        return name
    logging.getLogger("engine.risk").warning(
        "ignoring unknown RISK_PROFILE=%r — keeping 'conservative'", raw
    )
    return "conservative"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


# ─────────────────────────────────────────────────────────────────────
# Caps — per-strategy / per-user policy
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskCaps:
    """Conservative defaults aligned with PLAN.md §6.2.

    Production callers override these per user / per strategy.
    """

    # Sizing
    max_position_pct: float = 5.0          # single position ≤ 5% of equity
    max_single_name_pct: float = 8.0       # absolute single-name ceiling
    max_sector_pct: float = 25.0           # all positions in one sector
    min_qty: int = 1

    # Portfolio shape
    max_open_positions: int = 15
    max_correlation_cluster: int = 3
    """Max distinct held names in the same correlation cluster.
    Cluster membership is resolved via ``engine.risk.assets.cluster_for``
    (megacap_tech / ai_capex / money_center_banks / oil_majors / …).
    Symbols not in the map fall through — no cluster, no rule.
    """

    # Drawdown — non-negotiable per PLAN.md §12
    daily_drawdown_halt_pct: float = -3.0  # halt at -3% intraday

    # PDT (US <$25K accounts: max 3 day-trades per rolling 5 business days)
    pdt_account_threshold: float = 25_000.0
    pdt_max_day_trades_5d: int = 3

    # Confidence + agreement floors (from the council)
    min_council_confidence: float = 0.50
    min_specialist_avg_score: float = 45.0

    # Long-only unless explicitly opted in. See ``RiskCaps.from_env`` —
    # ALLOW_SHORTS=1 flips this, and nothing else does.
    forbid_short_phase_0: bool = True

    # ── Short-side caps (only read when shorts are enabled) ──────────
    max_short_position_pct: float = 2.0
    """Notional ceiling for a single SHORT, as a % of equity.

    Deliberately 2.5x tighter than ``max_position_pct`` (5%), and the ratio
    is the whole argument. A long's loss is bounded: the stock goes to zero
    and you lose 100% of the notional, so a 5% position caps the damage at
    -5% of equity. A short's loss is unbounded — the position grows against
    you as it moves, which is the opposite of a long, where the position
    shrinks as it loses.

    We size the cap off the adverse move a stop cannot protect against: an
    overnight or halt-reopen gap. Single-name squeezes that gap +100-150%
    through any resting stop are not hypothetical (VW 2008, GME 2021, and
    a long tail of small-cap borrow squeezes). Take +150% as the planning
    scenario — a 2.5x move against the entry:

        worst-case loss = notional x 1.5
        cap the loss at the SAME -5% of equity a long can produce
        =>  notional_pct x 1.5 <= 5%   =>  notional_pct <= 3.33%

    3.33% is the break-even; 2.0% is that with a margin of safety, and it
    also keeps the maintenance-margin call one gap further away. The
    ``short_unbounded_loss_cap`` rule trims to this number rather than
    rejecting — a smaller short is still a valid expression of the thesis.
    """

    max_short_gross_pct: float = 10.0
    """Ceiling on TOTAL short notional across the book, as a % of equity.

    Five 2%-shorts that all gap together is one 10% loss event, and
    correlated squeezes are exactly how short books die. Enforced by the
    same rule, after the per-position trim."""

    require_stop_on_short: bool = True
    """No short opens without a protective stop leg. Non-negotiable while
    shorts are enabled; exposed as a cap so a backtest that models its own
    exits can turn it off explicitly rather than by accident."""

    # ── Options caps (Phase A: long calls/puts only, no spreads/assignment) ──
    options_disabled: bool = True
    """Master switch. See ``RiskCaps.from_env`` — ``ALLOW_OPTIONS=1`` flips
    this, and nothing else does. Entry-only: a closing SELL_TO_CLOSE is
    never blocked by this flag, mirroring ``forbid_short_phase_0``'s own
    de-risking-is-always-permitted carve-out."""

    options_min_trading_level: int = 2
    """Alpaca's own tiering is counter-intuitive: level 1 = assignment-
    bearing structures (covered call / cash-secured put, Phase C); level 2
    = long call/put (Phase A, what this gates); level 3 = spreads/
    straddles (Phase B)."""

    options_max_premium_pct: float = 1.0
    """Single option position's premium-at-risk, as % of equity. The
    options analogue of ``max_position_pct`` — the entire premium is the
    max loss on a long option, so this number IS the position-size cap."""

    options_max_total_premium_pct: float = 5.0
    """All open long option premium combined, as % of equity. Phase A is
    long-only/single-leg/bounded-loss by construction, so this one number
    already caps the whole options book's worst case (100% of premium to
    zero) — see ``docs/OPTIONS_PLAN.md`` for why portfolio greek caps are
    deferred to Phase B/C rather than added here."""

    options_min_dte: int = 7
    options_max_dte: int = 60
    """Days-to-expiry window for a new entry. Below 7: 0DTE/weekly gamma
    risk. Above 60: capital parked too long in a decaying asset."""

    options_min_open_interest: int = 100
    options_min_volume: int = 1
    """NOT a daily-volume floor — 0 disables it. ``OptionLegDetails.volume``
    carries the snapshot's LAST TRADE SIZE (one print, typically 1-5 lots),
    because alpaca-py's ``OptionsSnapshot`` model drops the ``dailyBar``
    block that holds real volume. A floor of 10 rejected 16 of 18 live SPY
    contracts that had already cleared DTE, delta and IV. Open interest above
    is the real liquidity gate; this only asserts the contract has traded."""
    options_max_pct_of_open_interest: float = 1.0
    """A new option position may not exceed this percent of the CONTRACT'S
    OWN open interest. Sizing's liquidity dimension — the one the -$1,200
    CME loss exposed by not existing.

    ``options_position_size`` is ``floor(budget / cost)`` and takes no
    liquidity input at all, so on ``CME261016P00270000`` (open interest
    167, stamped 4 days stale) it sized 5 contracts purely because $2,300
    of dollar budget was available. Open interest is the whole gate today
    (see ``options_min_open_interest``), and it is a PASS/FAIL gate — a
    contract at 167 and one at 28,000 size identically.

    Open interest, not volume: ``options_min_volume``'s docstring above
    records that ``OptionLegDetails.volume`` carries a last-trade SIZE,
    not a daily total, so it cannot support a "percent of average daily
    volume" cap. Open interest is the only honest liquidity denominator
    this feed gives us.

    At 1.0: open interest 167 sizes to 1 contract (CME would have lost
    ~$240, not $1,200); SPY at 2,841 sizes to 28, far above what the
    dollar budget allows, so on genuinely liquid contracts this cap never
    binds and the premium budget stays the operative constraint. That
    asymmetry is the point — it shrinks exactly the positions whose exit
    is doubtful and leaves every other one alone.

    Never rounds a viable trade to zero: the floor is 1 contract when the
    dollar budget affords one, so this cap TRIMS, it does not veto. A
    contract too thin to hold even one lot is refused earlier, by
    ``options_min_open_interest`` and the chain-depth gate."""

    options_max_relative_spread_pct: float = 12.0
    """Liquidity floor read by ``illiquid_contract``: ``(ask-bid)/mid`` as
    a percentage. On a 15-min-delayed indicative feed this is the single
    most important number in the options rule set — and the delay is also
    why it is 12 rather than 8: the quoted book reads wider than the one an
    order would actually fill against."""

    options_earnings_blackout_days: int = 2
    """No new options entry within this many days of the underlying's next
    earnings — IV crush around a known event."""

    options_expiry_sweep_dte: int = 2
    """Read by the position manager's expiry sweep, not a risk-gate veto:
    an open option position is force-closed at this DTE. Non-negotiable
    per ``docs/OPTIONS_PLAN.md`` §2.6."""

    options_take_profit_pct: float = 60.0
    """Close a long option once its PREMIUM is up this much (percent).

    Read by the position manager's exit sweep, not by any risk-gate veto —
    see ``engine.options.exits`` for why a price-based exit has to live in
    our own sweep at all (Alpaca cannot bracket a single-leg option, so
    the broker-side target that protects every equity entry does not
    exist here)."""

    max_tolerated_book_drawdown_pct: float | None = None
    """The single-session options-book loss this profile ACCEPTS, in percent
    of equity. ``None`` means "no more than the daily halt allows" —
    ``abs(daily_drawdown_halt_pct)``.

    Exists so a profile that deliberately takes a wider tail has to SAY SO,
    as a reviewed number in git, instead of the invariant test being deleted
    or quietly loosened. ``respects_halt_coupling`` compares against this,
    not against the halt directly.

    Leave it ``None`` unless there is a dated, specific reason recorded in
    the profile's own docstring. A profile that sets it above
    ``abs(daily_drawdown_halt_pct)`` is stating that the halt no longer
    bounds its worst session — which is true of any options book, but should
    never be true by accident."""

    options_stop_loss_pct: float = 50.0
    """Close a long option once its PREMIUM has lost this much (positive
    magnitude: 50.0 means "down 50%").

    Tighter than the take-profit is wide, on purpose: a long option that
    has not worked bleeds theta every day it sits. The measure is the
    premium, not the underlying — on a 0.5-delta call this is roughly a 5%
    adverse move in the stock."""

    options_ratchet_enabled: bool = True
    """Master switch for ``engine.options.exits.option_ratchet_signal``.
    True is the default — the trailing ratchet REPLACES the flat
    ``options_take_profit_pct`` ceiling above for options exits when this
    is on; flipping it off reverts every open option to exactly the flat
    take-profit/stop-loss behavior, unconditionally. See
    ``docs/PLAN_EXIT_AGENT.md`` §2 for why the whole feature is designed
    to be a single-flag revert."""

    options_trail_arm_pct: float = 35.0
    """The trail arms once the position's peak premium gain reaches this
    percent. Below this, only the hard stop/take-profit can close the
    position — there is no trail line yet to give back from."""

    options_trail_giveback_pct: float = 30.0
    """Percent OF THE PEAK GAIN the trail gives back before closing —
    NOT a percentage-point giveback. A peak of +80% draws the trail line
    at +80 × (1 - 0.30) = +56%; a peak of +200% draws it at +140%. This is
    30, not 10, because the mark is a 15-minute-delayed indicative quote
    on a contract we permit up to a 12% relative spread — a 10% giveback
    would fire on quote noise, not on an actual reversal. See
    ``docs/OPTIONS_PLAYBOOK.md`` §6 for this disclosed as a limitation."""

    options_hard_take_profit_pct: float = 150.0
    """A backstop ceiling far above the arm point. The trail is expected
    to catch almost every real winner before this fires; this exists only
    for a single-tick gap that jumps from below the arm point straight
    past it. This is NOT the old fixed take-profit (that was 60.0, tight
    enough to cut winners short — see ``docs/PLAN_EXIT_AGENT.md`` §1) —
    it is set deliberately high because the trail, not this ceiling, is
    now the mechanism that locks in ordinary gains."""

    options_max_quote_age_seconds: float = 0.0
    """Refuse to SIZE an option on a quote older than this. **0 = OFF.**

    An option's value changes second to second, and until 2026-09-02
    nothing measured this at all: neither ``ChainQuote`` nor
    ``ContractQuote`` carried a timestamp, so no downstream code could
    apply a staleness rule even in principle — while the EQUITY path has
    had a 1800s gate since it was written.

    The timestamp is now plumbed (Alpaca snapshot ``latest_quote.timestamp``
    -> ``ChainQuote.quote_ts`` -> ``ContractQuote.quote_ts``) and
    ``select_contract`` has a freshness stage. This cap is what switches
    that stage on, and it ships **off**.

    Off, deliberately, and this is a judgement call worth stating. The
    gate refuses a quote whose age is UNKNOWN as well as one that is old,
    because an unknown age is exactly where a stale price is most likely
    and least detectable. That is the right rule and it has a bad failure
    mode: if the timestamp plumbing does not deliver in production, every
    option refuses and the desk trades nothing. Turning that on
    unverified, the night before a submission judged on P&L, risks the
    whole entry to fix a correctness issue that has been latent for
    weeks.

    **What to set it to depends entirely on the feed.** The default
    options feed is INDICATIVE (``_default_options_feed``): derived
    quotes on a documented ~15-minute delay. Every quote is then ~900s
    old as a PROPERTY OF THE TIER, not a fault, so:

      * INDICATIVE feed -> 1800 (catches "this contract has not quoted in
        half an hour" while passing the baseline delay);
      * OPRA feed (real-time) -> 300.

    Setting 300 on the indicative feed refuses 100% of options trades.
    Verify with the ``quote_ts`` check in ``docs/RAILWAY_CHECKS.md``
    before enabling either."""

    options_protective_stop_enabled: bool = True
    """Place a RESTING stop-limit at the broker after an option entry fills.

    Until 2026-09-02 the options stop lived only in our own 30-second
    polling loop, on the belief that a broker-side stop was structurally
    impossible. That belief was half right and is now out of date: a
    BRACKET is still impossible for a single-leg option (``OrderClass``
    allows only simple/mleg for ``us_option``), but Alpaca's options docs
    state that an options order's ``type`` may be ``stop`` or
    ``stop_limit`` for single-leg orders. So a STANDALONE protective stop
    is placeable, and this switch turns it on.

    It does not replace the software stop — both run, and the software
    one still owns the time stop, the signal exit and the expiry sweep.
    What this covers is what polling structurally cannot: our process
    down or redeploying, a broker read that errors and holds every option
    un-stopped for a tick, the gap between ticks, and overnight/weekend.

    It is NOT a fix for gap risk. A resting stop elects on a print exactly
    like ours does; a contract that jumps -26% → -52% in one print fills
    at the bad level either way. Gap risk is addressed at entry, by chain
    depth and by liquidity-relative sizing."""

    options_stop_limit_slippage_pct: float = 12.0
    """How far below the trigger the protective stop's LIMIT sits.

    A stop-MARKET on a thin option can fill anywhere, and this repo's
    standing rule is never to send a market order on a premium quoted off
    a delayed indicative feed — so the resting order is a stop-LIMIT and
    this is its band. Matched to ``options_max_relative_spread_pct`` (12)
    rather than picked independently: that is the widest book we let
    ourselves enter, so it is the width a fill has to cross in the worst
    contract we hold. Too tight and the stop elects but never fills,
    which is the failure mode a stop-limit must not have."""

    options_protective_stop_min_step_pct: float = 5.0
    """Percentage POINTS of P&L the ratchet's trail line must advance
    before the resting stop is cancel-replaced at the tighter level.

    Every replace is two broker calls and a brief window with no resting
    stop at all, so moving the order a few cents is a bad trade. 5 points
    matches the granularity the trail actually moves at (it arms at +35%
    and gives back 30% of the peak)."""

    # Wash-sale (US tax informational warning)
    wash_sale_lookback_days: int = 30
    """IRS rule: closing at a loss + re-entering within 30 calendar days
    disallows the loss. The ``wash_sale`` rule reads this. Informational
    only — never vetoes. Phase 0/1 uses calendar days; Phase 1.5 swaps
    to NY business days via ``pandas_market_calendars``."""

    # ── India (NSE/BSE/NFO) — read by the IN-market rules ────────────
    lot_sizes: tuple[tuple[str, int], ...] = (
        ("MIDCPNIFTY", 120),
        ("BANKNIFTY", 35),
        ("FINNIFTY", 65),
        ("NIFTY", 75),
        ("SENSEX", 20),
    )
    """NSE/BSE F&O contract lot sizes, longest-prefix-matched against the
    tradingsymbol (so BANKNIFTY must sort before NIFTY). Exchanges revise
    these — production callers override per the latest circular. Tuple of
    pairs (not a dict) because the dataclass is frozen/hashable."""

    max_derivative_notional_pct: float = 20.0
    """A single derivative (NFO/BFO/MCX/CDS) order's notional may not exceed
    this % of account equity. Derivatives are margin-traded, so the plain
    position-size cap understates true exposure."""

    mis_entry_cutoff_hour_ist: int = 15
    mis_entry_cutoff_minute_ist: int = 0
    """Indian brokers force-square-off MIS (intraday) positions ~15:20 IST.
    New intraday entries after this cutoff have no time to work — blocked."""

    @classmethod
    def aggressive_paper(cls, **overrides: object) -> RiskCaps:
        """Paper-account profile for the fixed-window contest.

        See ``docs/PLAN_AGGRESSIVE_PROFILE.md`` for the full reasoning —
        this is the reviewed profile it specifies, nothing more. Every
        number below is a diffable delta against the conservative default
        (a bare ``RiskCaps()``); dispatched via ``RISK_PROFILE=
        aggressive_paper`` (see ``from_env`` and ``_select_risk_profile``),
        never via an env var that supplies a number directly — that
        distinction is the whole point (see ``_select_risk_profile``'s
        docstring).

        Widens exactly six numbers:
          - ``options_max_premium_pct`` 1.0 -> 1.5 — the plan's §1 finding
            is that 1.0% was not really "small risk", it was a silent
            sizing-floor bug: at $100k equity, ANY contract priced above
            $10.00 floored to zero contracts and the pass became an
            un-ledgered HOLD. 1.5 clears that (contracts to $15.00 size at
            $100k) while keeping the book WIDE rather than deep.

            This was 2.5 on 2026-09-01 and is now 1.5, and the reason is
            book WIDTH, not appetite. The aggregate cap is pinned at 7.5
            by the halt coupling below and cannot rise, so per-position
            size is the only lever on how many positions the book holds:

                7.5 / 2.5 = 3 concurrent positions
                7.5 / 1.5 = 5 concurrent positions

            At 2.5 the measured consequence was a desk that stopped
            trading: 293 options runs produced 7 trades, and 48 of the
            refusals were ``max_total_premium_pct`` — the book filled at
            15:00 UTC on 2026-09-01 and stayed full for the rest of the
            session. Three positions is also a concentration the rest of
            this class works hard to avoid everywhere else (``max_open_
            positions``, ``correlation_cap``, ``sector_concentration``).

            Aggregate risk is UNCHANGED — the same 7.5% of equity is at
            work either way, and ``max_options_book_drawdown_pct`` is
            untouched at 3.0%. What changes is that it is spread over
            five independent theses instead of three, which lowers
            single-name variance without lowering exposure. That cuts
            both ways over a short window and is stated here so the
            trade-off is not rediscovered later: five smaller positions
            land nearer the strategy's expected value, so a book that is
            RIGHT wins less on its best name than three would have.

          - ``options_max_total_premium_pct`` 5.0 -> 11.0, with
            ``max_tolerated_book_drawdown_pct`` declared at 4.4.

            **2026-09-04, submission day, operator decision.** Raised from
            7.5 to 11.0 so the book could open positions during the final
            90-minute session: it sat at 7.21% of a 7.5% cap, ~$286 of
            headroom — not one contract. 11.0% frees ~$3,750, about three
            positions.

            This DELIBERATELY exceeds the halt ceiling. 11.0 x 40% = 4.40%
            of equity is reachable before a single stop fires, against a
            -3.00% daily halt, so the halt no longer bounds the worst
            session (``exceeds_halt_ceiling`` is True for this profile and
            False for conservative). The tail is declared as a reviewed
            number rather than the invariant being deleted — I advised
            against the widening, the operator confirmed it knowing the
            trade-off, and this records what was accepted.

            ``options_stop_loss_pct`` stays 40: below ~35 it fires on quote
            noise against a 12%-spread delayed mark, which is exactly how
            the CME position behaved on 2026-09-01 (mark frozen 2h16m, then
            a 26-point gap in one print). Tightening the stop to "pay for"
            the wider cap would have traded a known tail for a known
            failure mode.

            **Revert to 7.5 and drop ``max_tolerated_book_drawdown_pct``**
            to restore the original halt-bounded invariant exactly.

            The pre-2026-09-04 derivation, which still holds for any profile
            that does NOT declare a wider tail:
            ``|daily_drawdown_halt_pct| / (options_stop_loss_pct/100)``
            and NOT a number chosen for appetite. See
            ``max_options_book_drawdown_pct`` for the invariant and
            ``test_every_reviewed_profile_respects_the_halt_coupling``
            for its enforcement.

            This number was 12.0, was briefly raised to 18.0 on
            2026-09-01, and is now 7.5. The reasoning used to justify 18.0
            was WRONG, and it is written out here because the same
            argument will otherwise be made again:

              *"the halt bounds the SINGLE-DAY loss at -3% whatever the
              book's size, so raising the aggregate premium widens only
              the multi-day tail."*

            ``drawdown_halt`` blocks new ENTRIES. It does not close a
            single open position — ``position_manager`` explicitly allows
            closes to continue under a halt precisely because the halt
            itself de-risks nothing. So the halt does not bound the day's
            loss at all; it bounds the day's new RISK-TAKING. An open
            options book keeps falling straight through it.

            Measured the same morning the number was raised: six long
            calls opened 2026-08-31 inside ten minutes, $11,481 of premium
            = 11.45% of a $100,297 account. They gapped down overnight.
            The breaker tripped at 13:30:18 UTC — eighteen seconds after
            the bell — at -3.56%, and the account settled -3.67% with
            **not one stop fired**: the positions were between -20% and
            -33% against a -40% stop. Every rule behaved exactly as
            written and the account still lost more than its own halt
            threshold, because 11.45% x 40% = -4.58% of equity was always
            reachable before the first stop could trigger.

            The real coupling is therefore between the BOOK SIZE and the
            STOP, with the halt as the ceiling those two must multiply
            under — not between the book size and the halt directly.
          - ``min_council_confidence`` 0.50 -> 0.42 and
            ``min_specialist_avg_score`` 45.0 -> 40.0 — opens marginal
            setups the conservative floors would refuse outright.
          - ``options_stop_loss_pct`` 50.0 -> 40.0 — "cut losers early".
          - ``max_correlation_cluster`` 3 -> 4 — binds later as more
            capital deploys.

        Deliberately UNCHANGED — see the two ``test_aggressive_profile_
        leaves_*_alone`` tests, which pin exactly this:
          - ``max_position_pct`` (equity). A long option's max loss is the
            premium; an equity position's max loss is the notional. The
            aggression is concentrated where the loss is bounded by
            construction.
          - ``daily_drawdown_halt_pct`` (-3.0). This is the one that
            actually matters most: "the whole options book to zero costs
            12% of equity" is only tolerable as a MULTI-DAY worst case.
            What keeps it from being a single-day worst case is this halt.
            Widening the premium cap and holding the halt fixed is one
            coupled decision, not two independent ones.

        The take-profit row in the plan's own numbers table (a trailing
        ratchet replacing the fixed +60%) is deliberately NOT here — that
        is ``docs/PLAN_EXIT_AGENT.md``'s ratchet knobs, a separate
        workstream landing its own fields on this class.
        """
        # Merged as a dict (not passed as sibling keyword args) so an
        # explicit override of one of THESE SAME six fields replaces the
        # profile's value instead of colliding with it as a duplicate
        # keyword argument.
        values: dict[str, object] = {
            "options_max_premium_pct": 1.5,
            "options_max_total_premium_pct": 11.0,
            "max_tolerated_book_drawdown_pct": 4.4,
            "min_council_confidence": 0.42,
            "min_specialist_avg_score": 40.0,
            "options_stop_loss_pct": 40.0,
            "max_correlation_cluster": 4,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, **overrides: object) -> RiskCaps:
        """Default caps with the environment-configurable switches applied.

        ``RISK_PROFILE`` (default ``"conservative"``) selects the BASE
        profile — ``"conservative"`` (a bare ``RiskCaps()``) or
        ``"aggressive_paper"`` (``RiskCaps.aggressive_paper()``), via
        ``_select_risk_profile``. This is a choice between two REVIEWED,
        in-git, diffable profiles, never a raw number: setting
        ``RISK_PROFILE`` cannot express a cap nobody looked at, which is
        exactly what the "loss limits stay code-level" paragraph below is
        protecting against. An unrecognised value falls back to
        conservative and logs a warning — the same fail-to-default
        contract ``_env_int``/``_env_float`` already use.

        On top of whichever base profile applies, two switches: ``ALLOW_SHORTS``
        and ``ALLOW_OPTIONS``. Both are **off unless truthy** — an unset,
        empty, or typo'd value leaves ``forbid_short_phase_0=True`` /
        ``options_disabled=True``, because ``env_flag`` fails closed on
        anything it doesn't recognise, which is the direction that cannot
        lose money by accident.

        Three DATA-QUALITY floors are also env-tunable:
        ``OPTIONS_MIN_OPEN_INTEREST``, ``OPTIONS_MIN_VOLUME``,
        ``OPTIONS_MAX_SPREAD_PCT``. These describe how good a quote has to be
        before we will trade against it — they are calibration against a
        15-minute-delayed indicative feed, not limits on how much can be
        lost, and getting them wrong means the agent refuses everything
        rather than risking too much.

        **The loss limits stay code-level and are deliberately NOT
        env-tunable**: ``options_max_premium_pct``,
        ``options_max_total_premium_pct``, ``max_position_pct``,
        ``daily_drawdown_halt_pct``. A risk cap that can be widened by an
        env var nobody reviews is not a risk cap — and these are exactly the
        ones there is a live incentive to quietly widen. Changing them
        requires a reviewed code change (a new/edited profile classmethod
        above, not an env-supplied number).

        The ratchet knobs (``options_ratchet_enabled`` and its three
        thresholds) are exit thresholds too, same reasoning as
        ``OPTIONS_TAKE_PROFIT_PCT``/``OPTIONS_STOP_LOSS_PCT`` above, so they
        get the same env-tunable treatment. ``OPTIONS_RATCHET_ENABLED``
        defaults to **on** — the opposite polarity from
        ``ALLOW_SHORTS``/``ALLOW_OPTIONS``, which fail closed. An unset or
        malformed value here keeps the ratchet active; the one way back to
        the old flat take-profit is an explicit falsy value.
        """
        import os

        profile = _select_risk_profile(os.environ.get("RISK_PROFILE", ""))
        base = cls.aggressive_paper() if profile == "aggressive_paper" else cls()
        return replace(
            base,
            forbid_short_phase_0=not env_flag("ALLOW_SHORTS"),
            options_disabled=not env_flag("ALLOW_OPTIONS"),
            options_min_open_interest=_env_int(
                "OPTIONS_MIN_OPEN_INTEREST", base.options_min_open_interest
            ),
            options_min_volume=_env_int("OPTIONS_MIN_VOLUME", base.options_min_volume),
            options_max_relative_spread_pct=_env_float(
                "OPTIONS_MAX_SPREAD_PCT", base.options_max_relative_spread_pct
            ),
            # Env-tunable, unlike the premium CAPS above. An exit threshold
            # only decides when to realize a position whose size was
            # already bounded by those caps — it cannot increase maximum
            # loss beyond the premium already paid. A cap can.
            options_take_profit_pct=_env_float(
                "OPTIONS_TAKE_PROFIT_PCT", base.options_take_profit_pct
            ),
            options_stop_loss_pct=_env_float(
                "OPTIONS_STOP_LOSS_PCT", base.options_stop_loss_pct
            ),
            options_max_quote_age_seconds=_env_float(
                "OPTIONS_MAX_QUOTE_AGE_SECONDS", base.options_max_quote_age_seconds
            ),
            options_ratchet_enabled=env_flag(
                "OPTIONS_RATCHET_ENABLED", default=base.options_ratchet_enabled
            ),
            options_trail_arm_pct=_env_float(
                "OPTIONS_TRAIL_ARM_PCT", base.options_trail_arm_pct
            ),
            options_trail_giveback_pct=_env_float(
                "OPTIONS_TRAIL_GIVEBACK_PCT", base.options_trail_giveback_pct
            ),
            options_hard_take_profit_pct=_env_float(
                "OPTIONS_HARD_TAKE_PROFIT_PCT", base.options_hard_take_profit_pct
            ),
            **overrides,  # type: ignore[arg-type]
        )

    @property
    def max_options_book_drawdown_pct(self) -> float:
        """Worst-case equity loss, in percent, from the options book alone
        BEFORE a single ``options_stop_loss_pct`` stop can fire.

        A full book is ``options_max_total_premium_pct`` of equity, and
        every contract in it can fall to the stop without triggering it.
        So the reachable loss is the product, and it is reachable in ONE
        session — an overnight gap moves the whole book at once, and a
        stop is an intraday mechanism that cannot act on a gap.

        This must stay at or below ``abs(daily_drawdown_halt_pct)``.
        ``drawdown_halt`` does not enforce that for you: it blocks new
        ENTRIES and closes nothing (``position_manager`` deliberately
        keeps closes legal under a halt, because the halt de-risks
        nothing). An account can therefore lose well past its own halt
        threshold with every rule behaving exactly as written — which is
        what happened on 2026-09-01: -3.67% against a -3.00% halt, zero
        stops fired, book at 11.45% x 40% = -4.58% reachable.

        Pinned by ``test_every_reviewed_profile_respects_the_halt_coupling``
        for both reviewed profiles, so a future widening of either factor
        has to move the other or fail the suite.
        """
        return self.options_max_total_premium_pct * self.options_stop_loss_pct / 100.0

    @property
    def tolerated_book_drawdown_pct(self) -> float:
        """The single-session options-book loss this profile accepts.

        Defaults to ``abs(daily_drawdown_halt_pct)`` — i.e. "the halt is the
        ceiling" — unless the profile explicitly declares a wider tail via
        ``max_tolerated_book_drawdown_pct``."""
        if self.max_tolerated_book_drawdown_pct is not None:
            return self.max_tolerated_book_drawdown_pct
        return abs(self.daily_drawdown_halt_pct)

    @property
    def respects_halt_coupling(self) -> bool:
        """Whether this profile's options book stays inside the tail it
        DECLARED. False is a misconfiguration.

        Compares against ``tolerated_book_drawdown_pct``, not the halt
        directly, so a profile that knowingly accepts a wider tail records
        that as a reviewed number rather than by weakening this check. Use
        ``exceeds_halt_ceiling`` to ask the separate question of whether the
        halt still bounds the worst session at all."""
        return self.max_options_book_drawdown_pct <= self.tolerated_book_drawdown_pct

    @property
    def exceeds_halt_ceiling(self) -> bool:
        """True when this profile's own declared tail is wider than the
        daily halt — i.e. the halt no longer bounds its worst session.

        Deliberately separate from ``respects_halt_coupling``: a profile can
        be correctly configured (inside its declared tail) AND still be
        taking more single-session risk than the halt would stop. Both facts
        are worth being able to state."""
        return self.tolerated_book_drawdown_pct > abs(self.daily_drawdown_halt_pct)

    @property
    def shorts_enabled(self) -> bool:
        """Readable inverse of ``forbid_short_phase_0`` for call sites and logs."""
        return not self.forbid_short_phase_0

    @property
    def options_enabled(self) -> bool:
        """Readable inverse of ``options_disabled`` for call sites and logs."""
        return not self.options_disabled


# ─────────────────────────────────────────────────────────────────────
# Portfolio snapshot — what the risk engine reads
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float
    sector: str | None = None
    is_option: bool = False
    multiplier: int = 1
    """1 for equities, 100 for standard US equity options. ``market_value``
    is already correctly scaled — this is for callers that need to convert
    ``avg_entry_price`` into a notional themselves."""
    unrealized_pl: float | None = None
    """The broker's OWN unrealized P&L for this position, already correctly
    scaled and signed (a loss is negative for either a long or a short) —
    see ``broker.types.Position.unrealized_pl``. ``None`` when the broker
    didn't report one (a non-Alpaca broker, or a stale pre-migration
    snapshot row) — callers must fall back to deriving it from
    ``market_value``/``avg_entry_price`` themselves in that case, never
    fabricate a zero. Prefer this over re-deriving P&L from ``market_value``
    whenever it is present: a re-derivation can drift from the broker's own
    live mark (confirmed live 2026-09-03 — a short equity position showed
    $0 here-derived against a real -$22 on the broker's own dashboard)."""


@dataclass(frozen=True)
class ClosedTrade:
    """One closed trade — feeds the wash-sale rule.

    ``closed_at`` is a calendar date in Phase 0/1; Phase 1.5 swaps to NY
    business days via ``pandas_market_calendars``.
    """

    symbol: str
    closed_at: date
    realized_pnl: float


@dataclass(frozen=True)
class RiskContext:
    """Per-user portfolio + halt state. Populated by the context provider —
    a MockProvider in Phase 0/1, the real reconciler-backed one in Phase 2.
    """

    account_equity: float
    cash: float
    buying_power: float
    open_positions: tuple[PortfolioPosition, ...] = ()

    # PDT tracking — rolling 5 business days
    day_trades_last_5d: int = 0

    # Recent closes-at-a-loss for wash-sale informational warning.
    recent_losing_closes: tuple[ClosedTrade, ...] = ()

    # Daily P&L (for drawdown breaker)
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0

    # Circuit-breaker state
    drawdown_halted: bool = False
    drawdown_halt_reason: str | None = None
    drawdown_halted_at: date | None = None

    # Evaluation clock — injectable so time-of-day rules (MIS square-off
    # window) are testable. None → rules read the real wall clock.
    now_utc: datetime | None = None

    # Options account gating — read from the broker account, not the
    # positions snapshot (parallels how other account-level state here is
    # populated by the context provider).
    options_trading_level: int | None = None


# ─────────────────────────────────────────────────────────────────────
# Proposal + Decision — input / output of the engine
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptionLegDetails:
    """Option-specific fields on a ``RiskProposal``. Present only when
    ``RiskProposal.is_option`` is True.

    ``dte`` is deliberately NOT a stored field — every rule that needs it
    (``min_dte``/``max_dte``/``expiry_day_entry``) computes it fresh from
    ``expiry`` + ``context.now_utc`` each time. A contract drafted at 8
    DTE can be 3 DTE by the time it's approved and re-risk-checked days
    later; a pre-computed value would silently under-protect exactly the
    case these rules exist for.
    """

    underlying_symbol: str
    occ_symbol: str
    contract_type: Literal["call", "put"]
    strike: float
    expiry: date
    multiplier: int = 100
    action: Literal["buy_to_open", "sell_to_close"] = "buy_to_open"
    """Phase A only — no short legs, so no other action value is ever
    constructed. Belt-and-suspenders with the ``naked_short_forbidden``
    risk rule, which does not depend on this restriction to hold."""
    open_interest: int | None = None
    volume: int | None = None
    bid: float | None = None
    ask: float | None = None
    implied_volatility: float | None = None
    days_to_earnings: int | None = None
    """Computed once by the ``options_context`` feature block and copied
    here at Drafter-time, so ``earnings_blackout`` can re-check it at
    execution-time without a second fetch."""


@dataclass(frozen=True)
class RiskProposal:
    """The slice of an agent's proposal the risk engine reads. We don't pass
    the full ApprovalProposalDto so the engine stays UI-agnostic.
    """

    symbol: str
    side: Side
    qty: int
    estimated_notional: float
    last_price: float

    confidence: float | None
    """The council's confidence (0-1) in this trade, as the Drafter emitted
    it. ``None`` means NOT RECORDED — legacy rows written before the
    approval DTO carried the field, or any path with no decision row.

    ``None`` makes ``min_council_confidence`` self-gate out rather than
    veto. That is deliberate: confidence is fixed at draft-time and the
    council already applied the floor to the real number, so the only
    thing a re-check without it can do is invent a stand-in. It used to
    invent ``conviction_level / 5`` — a DIFFERENT quantity on a different
    scale ("how big a bet", 1-5) — and score it against a floor calibrated
    for confidence ("how likely to work", 0-1). Live: AMZN 2026-08-31
    passed the council at confidence 0.54 and was then refused at approval
    time as "0.40 below floor 0.42", 0.40 being conviction 2 / 5.

    Never substitute a proxy here. Pass the real number or pass None."""

    # ── Options inputs ───────────────────────────────────────────────
    is_option: bool = False
    option: OptionLegDetails | None = None
    """Present only when ``is_option`` is True. Every field an options
    risk rule needs at execution time (premium, multiplier, strike,
    expiry, greeks) must be written here at Drafter-time — the executor's
    re-risk-check reads the *persisted* proposal, not live state, so
    anything missing here is gone by the time the risk gate re-runs."""
    # Whether this would close an existing same-day position (PDT scoring).
    closes_intraday_position: bool = False
    # India: True when the order will be placed as an intraday product
    # (Zerodha MIS) — read by the square-off-window rule.
    is_intraday: bool = False

    # ── Short-side inputs ────────────────────────────────────────────
    stop_price: float | None = None
    """The protective stop the proposal ships with. ``short_requires_stop``
    reads it; for a short the stop must sit ABOVE the entry."""

    shortable: bool | None = None
    """Broker's ``shortable`` flag for the asset. ``None`` = unknown, which
    the short rules treat as a veto — an unverified borrow is not a borrow."""

    easy_to_borrow: bool | None = None
    """Broker's ``easy_to_borrow`` (ETB) flag. Hard-to-borrow names carry
    borrow fees and recall risk that this system does not model."""


@dataclass(frozen=True)
class SpecialistScore:
    """One score per specialist — the council emits these. Risk engine reads
    them for the specialist-average-score floor."""

    name: str
    score: float
    confidence: float


@dataclass(frozen=True)
class RiskDecision:
    """The result of ``engine.risk.evaluate``. Two outcomes:

    - ``approved=True``  : optionally with ``adjusted_qty`` if a rule trimmed.
    - ``approved=False`` : ``veto_rule`` names the first rule that blocked.

    ``informational_flags`` carries non-blocking signals (e.g. wash-sale
    warnings, near-cap warnings) the UI can surface without halting the trade.
    """

    approved: bool
    reason: str
    veto_rule: str | None = None
    adjusted_qty: int | None = None
    informational_flags: tuple[str, ...] = field(default_factory=tuple)
    checks_passed: tuple[str, ...] = field(default_factory=tuple)
    """Named rules that ran and did NOT block, in evaluation order.

    The veto name alone tells a user why a trade was refused but says
    nothing about what an approved trade actually cleared. Recording the
    passes turns "the risk engine approved it" into an enumerable list a
    UI can render and an auditor can check — the same reason ``veto_rule``
    exists at all. Rules that self-gate out (an India rule on a US symbol)
    are not listed: they did not run, so they did not pass.
    """

    trim_rules: tuple[str, ...] = field(default_factory=tuple)
    """Named rules that SHRANK the trade instead of blocking it.

    A trim is a partial refusal, and until this field existed the name of
    the rule responsible was discarded: the engine kept only an anonymous
    ``trimmed:80->37`` informational flag. So the Refusal Ledger could
    report how often risk said "no" but never how often it said "not that
    much" — a materially different and more common intervention.

    Trimming rules name themselves with a ``_trim`` suffix
    (``max_premium_pct_trim``, ``max_position_pct_trim``,
    ``short_unbounded_loss_cap_trim``) so a reader can never mistake a
    trim for a block. A rule lands here ONLY when it actually changed the
    qty; one that ran and left the size alone belongs in ``checks_passed``.
    """
