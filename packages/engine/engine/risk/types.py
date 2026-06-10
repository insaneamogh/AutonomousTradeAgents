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

    # Long-only in Phase 0/1 — shorting requires margin + borrow handling.
    forbid_short_phase_0: bool = True

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
