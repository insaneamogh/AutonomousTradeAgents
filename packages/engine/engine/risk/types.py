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

from engine.env import env_flag


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

        Only ONE switch is environment-driven today: ``ALLOW_SHORTS``.
        Everything else stays a code-level default that a caller overrides
        explicitly, because a risk cap that can be widened by an env var
        nobody reviews is not a risk cap.

        Shorts are **off unless ALLOW_SHORTS is truthy**. An unset, empty,
        or typo'd value leaves ``forbid_short_phase_0=True`` — ``env_flag``
        fails closed on anything it doesn't recognise, which is the
        direction that cannot lose money by accident.
        """
        return cls(forbid_short_phase_0=not env_flag("ALLOW_SHORTS"), **overrides)  # type: ignore[arg-type]

    @property
    def shorts_enabled(self) -> bool:
        """Readable inverse of ``forbid_short_phase_0`` for call sites and logs."""
        return not self.forbid_short_phase_0


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


# ─────────────────────────────────────────────────────────────────────
# Proposal + Decision — input / output of the engine
# ─────────────────────────────────────────────────────────────────────


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
