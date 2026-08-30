"""Risk-engine wire types.

Every wire surface that flows between the agent council, the risk engine,
and the executor is typed here. Pydantic-free on purpose — these are
zero-dep dataclasses so the risk layer stays usable from non-FastAPI
contexts (CLI, backtester, batch jobs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    def from_env(cls, **overrides: object) -> RiskCaps:
        """Default caps with the environment-configurable switches applied.

        Two switches: ``ALLOW_SHORTS`` and ``ALLOW_OPTIONS``. Both are **off
        unless truthy** — an unset, empty, or typo'd value leaves
        ``forbid_short_phase_0=True`` / ``options_disabled=True``, because
        ``env_flag`` fails closed on anything it doesn't recognise, which is
        the direction that cannot lose money by accident.

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
        requires a reviewed code change.

        The ratchet knobs (``options_ratchet_enabled`` and its three
        thresholds) are exit thresholds too, same reasoning as
        ``OPTIONS_TAKE_PROFIT_PCT``/``OPTIONS_STOP_LOSS_PCT`` above, so they
        get the same env-tunable treatment. ``OPTIONS_RATCHET_ENABLED``
        defaults to **on** — the opposite polarity from
        ``ALLOW_SHORTS``/``ALLOW_OPTIONS``, which fail closed. An unset or
        malformed value here keeps the ratchet active; the one way back to
        the old flat take-profit is an explicit falsy value.
        """
        return cls(
            forbid_short_phase_0=not env_flag("ALLOW_SHORTS"),
            options_disabled=not env_flag("ALLOW_OPTIONS"),
            options_min_open_interest=_env_int(
                "OPTIONS_MIN_OPEN_INTEREST", cls.options_min_open_interest
            ),
            options_min_volume=_env_int("OPTIONS_MIN_VOLUME", cls.options_min_volume),
            options_max_relative_spread_pct=_env_float(
                "OPTIONS_MAX_SPREAD_PCT", cls.options_max_relative_spread_pct
            ),
            # Env-tunable, unlike the premium CAPS above. An exit threshold
            # only decides when to realize a position whose size was
            # already bounded by those caps — it cannot increase maximum
            # loss beyond the premium already paid. A cap can.
            options_take_profit_pct=_env_float(
                "OPTIONS_TAKE_PROFIT_PCT", cls.options_take_profit_pct
            ),
            options_stop_loss_pct=_env_float(
                "OPTIONS_STOP_LOSS_PCT", cls.options_stop_loss_pct
            ),
            options_ratchet_enabled=env_flag(
                "OPTIONS_RATCHET_ENABLED", default=cls.options_ratchet_enabled
            ),
            options_trail_arm_pct=_env_float(
                "OPTIONS_TRAIL_ARM_PCT", cls.options_trail_arm_pct
            ),
            options_trail_giveback_pct=_env_float(
                "OPTIONS_TRAIL_GIVEBACK_PCT", cls.options_trail_giveback_pct
            ),
            options_hard_take_profit_pct=_env_float(
                "OPTIONS_HARD_TAKE_PROFIT_PCT", cls.options_hard_take_profit_pct
            ),
            **overrides,  # type: ignore[arg-type]
        )

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
    confidence: float

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
