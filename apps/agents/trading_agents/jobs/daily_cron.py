"""Daily council cron — runs the agent council across a watchlist.

Phase 4 paper-trading kickoff. PLAN.md §11 calls for daily decisions
across a small watchlist (start with 10 names: SPY, QQQ, AAPL, NVDA,
MSFT, GOOG, AMZN, META, TSLA, JPM). This script is the entry point.

Idempotency:
  - One ``DecisionEntry`` per (user, date_utc, symbol).
  - Re-runs within the same day are a no-op for symbols already
    decided that day. This makes the script safe for both
    cron-on-clock scheduling AND ad-hoc operator-fired retries.

In production this is invoked by ``CouncilScheduler``
(``apps/api/app/services/council/scheduler.py``), an in-process asyncio
background task started from the FastAPI lifespan — not an external
cron. It's off by default (``COUNCIL_SCHEDULER_ENABLED=0``); once
enabled it runs a baseline sweep of the full watchlist at fixed times
(``COUNCIL_SCAN_TIMES_UTC``) plus an optional trigger loop
(``SCANNER_ENABLED=1``) that wakes the council only when a cheap
deterministic scan trips a named rule. See that module's docstring for
the full config surface.

This script itself remains useful for manual/ad-hoc runs. See
``docs/README.md`` for deploy + operational context.

Usage:

    PYTHONPATH=apps/agents:packages/engine:packages/broker:apps/api \\
    USE_POSTGRES=1 \\
    uv run python -m trading_agents.jobs.daily_cron \\
        --user-id 00000000-0000-0000-0000-000000000001 \\
        --watchlist SPY,QQQ,AAPL,NVDA,MSFT,GOOG,AMZN,META,TSLA,JPM
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from engine.env import env_flag
from engine.scanner import ScanSignal
from trading_agents.features import resolve_feature_provider
from trading_agents.llm import LLM
from trading_agents.memory import get_confidence_store, get_decision_log
from trading_agents.runtime import run_council

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s — %(message)s",
)
log = logging.getLogger("agents.cron.daily")


# Default watchlist — kept short on purpose. PLAN.md §11 hardens this
# to a per-user persisted list later; for kickoff a static set is fine.
DEFAULT_WATCHLIST: tuple[str, ...] = (
    "SPY", "QQQ", "AAPL", "NVDA", "MSFT",
    "GOOG", "AMZN", "META", "TSLA", "JPM",
)


@dataclass(frozen=True)
class SymbolScanContext:
    """Why the scanner woke the council for one symbol, and where it ranks.

    Passed straight through to the analysts' feature dict so a triggered
    run can say WHICH deterministic condition fired rather than arriving
    with no more context than a scheduled sweep. Typed rather than a loose
    dict because this crosses the scanner → agents boundary.
    """

    signals: tuple[ScanSignal, ...] = ()
    relative_strength_rank: float | None = None


def _with_scan_context(
    provider: Any, scan_context: Mapping[str, SymbolScanContext]
) -> Any:
    """Wrap a feature provider so triggered runs carry their trigger reason.

    Wrapping rather than threading a parameter through ``run_council`` keeps
    the council's contract intact: agents still receive one pre-computed
    feature dict and still never fetch anything themselves.
    """

    async def _provider(symbol: str, horizon: str = "short") -> dict[str, Any]:
        features = provider(symbol, horizon)
        if inspect.isawaitable(features):
            features = await features
        ctx = scan_context.get(symbol.upper())
        if ctx is None:
            return features
        if ctx.signals:
            features["scan_triggers"] = [s.as_dict() for s in ctx.signals]
        quant = features.get("quant")
        if ctx.relative_strength_rank is not None and isinstance(quant, dict):
            quant["relative_strength_rank"] = ctx.relative_strength_rank
        return features

    return _provider


def _today_utc() -> str:
    """Date stamp for idempotency. UTC date is fine — the cron runs at a
    market-aligned hour, so different cron invocations on the same NYSE
    day map to the same UTC date in 99% of cases. Phase 1.5 swaps to NY
    business days via ``pandas_market_calendars``.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def _already_decided_today(
    user_id: str, symbol: str
) -> bool:
    """Has the council already produced a row for this (user, symbol) today?

    Uses the DecisionLog's indexed ``has_decision_today`` (an existence
    query on (user_id, symbol, triggered_at)) — NOT a full-history scan,
    so cron latency stays flat as the decision history grows.
    """
    return await get_decision_log().has_decision_today(
        user_id=user_id, symbol=symbol, day_utc=_today_utc()
    )


_DEFAULT_OPTIONS_RESCAN_COOLDOWN_MIN = 45


def _options_rescan_cooldown_minutes() -> int:
    """Minutes an OPTION symbol must wait before the council looks again.

    Once-per-day is the right cadence for a swing equity position and the
    wrong one for options. Options are a timing instrument: a symbol with
    no setup at 14:00 can be a clean one at 15:30, and the daily dedup
    silently capped every underlying at a single look per session — which
    made "continuous options scanning" impossible no matter how often the
    deterministic scanner ran.

    So options get a COOLDOWN instead of a daily lock. The deterministic
    scan still runs every SCANNER_INTERVAL_MINUTES and still only wakes the
    council on a named trigger; this bounds how often that wake can spend
    LLM budget on the same underlying. Malformed input keeps the default —
    a typo must never remove the bound.
    """
    raw = os.environ.get("OPTIONS_RESCAN_COOLDOWN_MINUTES", "").strip()
    if not raw:
        return _DEFAULT_OPTIONS_RESCAN_COOLDOWN_MIN
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "ignoring malformed OPTIONS_RESCAN_COOLDOWN_MINUTES=%r — keeping %d",
            raw, _DEFAULT_OPTIONS_RESCAN_COOLDOWN_MIN,
        )
        return _DEFAULT_OPTIONS_RESCAN_COOLDOWN_MIN
    return max(0, value)


_DEFAULT_MAX_DAILY_LLM_SPEND_USD = 3.0
"""Hard ceiling, real dollars off ``trading_agents.cost_ledger``, on
Anthropic spend SINCE MIDNIGHT UTC TODAY — a calendar day, not a rolling
24h window. Once crossed, every remaining LLM-eligible candidate HOLDs
uncosted until UTC midnight rolls the window over — checked LIVE right
before each one actually runs (not just once at the top of a sweep), so
it can trip PARTWAY through a sweep that started under budget.

Deliberately calendar-day, not rolling-24h: a rolling window would keep
counting a historical spike (e.g. today's 638-call, ~$10 drain) against
the ceiling for a full 24 hours after it happened — so an operator who
tops up credits at 19:00 UTC expecting to resume soon would find every
candidate still HOLDing uncosted until ~19:00 UTC the NEXT day, long
after the real Anthropic balance was healthy again. Midnight UTC is also
comfortably before the 13:30 UTC US market open, so "today's budget"
still means the whole trading day.

$3.00 is derived from the account's own numbers, not guessed: a $10
top-up meant to last the ~3 remaining contest days (docs/HACKATHON.md)
divides to ~$3.33/day, rounded down for margin.

Known gap: ``InMemoryCostLedger`` (the fallback when Postgres is
unreachable) resets on every process restart, and a redeploy IS a process
restart — so this can only bound spend within one continuously-running
process's view of "since midnight," not truly cross-restart. Say so
plainly rather than overclaiming "cannot exceed $3/day" — that is what
``MAX_LLM_SYMBOLS_PER_SWEEP`` below is for: it does not depend on the
ledger at all, so it still bounds a single sweep's worst case even right
after the ledger's memory has been reset.
"""


def _seconds_since_midnight_utc() -> timedelta:
    """How far into today (UTC) we are, for a calendar-day spend window —
    see ``_DEFAULT_MAX_DAILY_LLM_SPEND_USD`` for why this is midnight-UTC
    rather than a rolling 24h lookback."""
    now = datetime.now(UTC)
    return now - datetime.combine(now.date(), time.min, tzinfo=UTC)

_DEFAULT_MAX_LLM_SYMBOLS_PER_SWEEP = 15
"""Ledger-independent hard cap: at most this many symbols may clear
``strategy_fit`` and reach a real LLM call in ONE call to ``main()`` —
regardless of how many the watchlist holds or how many clear
``MIN_FIT_TO_TRADE``.

Before this existed, a baseline sweep had NO ceiling of its own (unlike
the trigger loop's ``SCANNER_MAX_COUNCIL_RUNS``): ``strategy_fit`` blocked
symbols with no real setup, but on a trending day most of the rest
cleared the floor and EVERY one of them got a full paid council pass.
That is what spent the entire $10 Anthropic balance in one ~90-minute
sweep on 2026-09-01 (638 real LLM calls, confirmed live in Railway logs)
once the watchlist had grown to 123+ symbols.

Ranked, not first-N-in-watchlist-order: ``_score_candidates_for_sweep``
runs strategy_fit's own scoring (zero LLM cost) for every symbol first,
so the admitted set is the BEST-scoring setups this sweep found, not
whichever symbols happen to sort alphabetically first.
"""


def _max_daily_llm_spend_usd() -> float:
    raw = os.environ.get("MAX_DAILY_LLM_SPEND_USD", "").strip()
    if not raw:
        return _DEFAULT_MAX_DAILY_LLM_SPEND_USD
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "ignoring malformed MAX_DAILY_LLM_SPEND_USD=%r — keeping $%.2f",
            raw, _DEFAULT_MAX_DAILY_LLM_SPEND_USD,
        )
        return _DEFAULT_MAX_DAILY_LLM_SPEND_USD
    return value if value > 0 else _DEFAULT_MAX_DAILY_LLM_SPEND_USD


_DEFAULT_MAX_LLM_SYMBOLS_PER_DAY = 20
"""Ledger-backed hard cap on paid council passes per UTC DAY, across every
caller — the ceiling ``MAX_LLM_SYMBOLS_PER_SWEEP`` cannot provide.

The gap this closes, measured 2026-09-01: the per-SWEEP cap is re-armed on
every call to ``main()``, and the trigger loop calls ``main()`` every
``SCANNER_INTERVAL_MINUTES`` (2 at the time). So "15 per sweep" was really
"15 every two minutes" — 450/hour of headroom — and the only thing
actually standing between that and the credit balance was
``MAX_DAILY_LLM_SPEND_USD``. The afternoon ran **267 paid council passes
across 134 distinct symbols** before the balance hit zero.

Counted in RUNS, not dollars, because that is the unit the operator
reasons about ("debate at most 10-20 symbols a day") and because it holds
even when the dollar ledger cannot be trusted — see
``_DEFAULT_MAX_DAILY_LLM_SPEND_USD``'s note about the in-memory fallback
resetting on redeploy. The two gates are deliberately independent: this
one bounds VOLUME, that one bounds COST, and either alone stops a runaway.

Deterministic work is unaffected. ``strategy_fit`` still scores every
symbol on the watchlist for free, and a symbol that would HOLD anyway
never counts against this budget — only passes that actually reach a paid
model call do. That is the whole shape the operator asked for: screen
everything with maths, debate only the best handful.
"""


_DEFAULT_MAX_LLM_SYMBOLS_PER_HOUR = 4
"""Paces ``MAX_LLM_SYMBOLS_PER_DAY`` across the session instead of letting
it burn at the open.

A daily cap alone is first-come-first-served: the trigger loop runs every
``SCANNER_INTERVAL_MINUTES``, so 20 paid passes would be spent within
~15 minutes of the 13:30 UTC open and the desk would then HOLD everything
uncosted for the remaining six hours. That is the same failure the daily
cap was added to prevent, re-expressed in symbols instead of dollars — and
it is worse than it sounds, because the best setups of a session are not
reliably its first ones.

4/hour x the ~6.5h US session is ~26, so the DAILY cap (20) stays the
binding constraint over a full day while this one stops any single hour
from consuming it. Both are enforced; neither replaces the other.
"""


def _max_llm_symbols_per_hour() -> int:
    raw = os.environ.get("MAX_LLM_SYMBOLS_PER_HOUR", "").strip()
    if not raw:
        return _DEFAULT_MAX_LLM_SYMBOLS_PER_HOUR
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "ignoring malformed MAX_LLM_SYMBOLS_PER_HOUR=%r — keeping %d",
            raw, _DEFAULT_MAX_LLM_SYMBOLS_PER_HOUR,
        )
        return _DEFAULT_MAX_LLM_SYMBOLS_PER_HOUR
    return value if value >= 1 else _DEFAULT_MAX_LLM_SYMBOLS_PER_HOUR


_DEFAULT_MIN_LLM_SCORE = 0.0
"""Deterministic strategy-fit score a symbol must clear before it may
consume a PAID council pass. 0.0 = off, which is the default.

Distinct from ``strategies.fit.MIN_FIT_TO_TRADE`` (0.45), which asks "is
this tradeable at all" — a symbol below that already scores ``None`` here
and never reaches an LLM. This asks the different question "is this good
enough to spend money on", and exists because the day/hour caps are
first-come-first-served ACROSS sweeps: the scanner runs every 2 minutes,
so a mediocre setup at 14:02 can consume an hourly slot that a much
better one at 14:40 then cannot have. A floor is the cheap, deterministic
way to stop that, and it is exactly the "only fire when something good
comes up" gate the scanner is supposed to have.

Defaults to OFF, and **measurement now says it probably cannot be turned
on usefully** — read this before reaching for it.

``apps/agents/tests/eval`` measured the score distribution on 2026-09-02
across 300 symbols from the repo's own synthetic feature generator:
every symbol that clears ``MIN_FIT_TO_TRADE`` scores between **0.6075
and 0.6107** — 18 distinct values inside a 0.3% band. There is a cliff
between 0.60 and 0.65 and nothing inside it, so any floor set here either
admits every passing candidate or none of them. It is a switch, not a
dial.

That is measured on SYNTHETIC features, which are derived from one hash
seed per symbol and are low-variance by construction, so the real
distribution may be wider — but it is unmeasured, and setting this on
unmeasured data is the exact mistake this repo keeps writing post-mortems
about. Run ``tests/eval/run_eval.py`` against the live feature provider
first; if the spread is still ~0.003, this knob is the wrong tool and the
right fix is a scoring signal with real dynamic range.
"""


def _min_llm_score() -> float:
    raw = os.environ.get("MIN_LLM_SCORE", "").strip()
    if not raw:
        return _DEFAULT_MIN_LLM_SCORE
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "ignoring malformed MIN_LLM_SCORE=%r — keeping %.2f",
            raw, _DEFAULT_MIN_LLM_SCORE,
        )
        return _DEFAULT_MIN_LLM_SCORE
    return value if 0.0 <= value <= 1.0 else _DEFAULT_MIN_LLM_SCORE


def _max_llm_symbols_per_day() -> int:
    raw = os.environ.get("MAX_LLM_SYMBOLS_PER_DAY", "").strip()
    if not raw:
        return _DEFAULT_MAX_LLM_SYMBOLS_PER_DAY
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "ignoring malformed MAX_LLM_SYMBOLS_PER_DAY=%r — keeping %d",
            raw, _DEFAULT_MAX_LLM_SYMBOLS_PER_DAY,
        )
        return _DEFAULT_MAX_LLM_SYMBOLS_PER_DAY
    return value if value >= 1 else _DEFAULT_MAX_LLM_SYMBOLS_PER_DAY


def _max_llm_symbols_per_sweep() -> int:
    raw = os.environ.get("MAX_LLM_SYMBOLS_PER_SWEEP", "").strip()
    if not raw:
        return _DEFAULT_MAX_LLM_SYMBOLS_PER_SWEEP
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "ignoring malformed MAX_LLM_SYMBOLS_PER_SWEEP=%r — keeping %d",
            raw, _DEFAULT_MAX_LLM_SYMBOLS_PER_SWEEP,
        )
        return _DEFAULT_MAX_LLM_SYMBOLS_PER_SWEEP
    return value if value >= 1 else _DEFAULT_MAX_LLM_SYMBOLS_PER_SWEEP


async def _score_candidates_for_sweep(
    watchlist: list[str],
    instrument_by_symbol: Mapping[str, str],
    feature_provider: Any,
    priors: Mapping[str, float],
) -> tuple[dict[str, float | None], dict[str, float]]:
    """Score every symbol's deterministic strategy fit, spending ZERO LLM
    calls, so the real (paid) pass in ``main`` can ration a limited
    per-sweep budget to the best setups first.

    Returns ``{symbol: score}`` — ``None`` for a symbol that will
    legitimately HOLD before any LLM call regardless of budget (mirrors
    ``strategy_fit_node``'s own None-winner outcome exactly, via the same
    ``best_strategy`` call this repo already uses for the real gate), a
    float for one that WOULD reach a real LLM call.

    A per-symbol feature-fetch or scoring failure scores as ``None``
    (treated as free) rather than aborting the whole sweep — the real run
    below hits the identical failure and is already handled there (see
    ``main``'s per-symbol try/except), so nothing is silently swallowed,
    just deferred to the path that already logs it.

    Costs one extra feature fetch (Alpaca/FRED, not Anthropic) per
    watchlist symbol versus today — a deliberate trade of deterministic
    API load for LLM budget control, not free, but the resource this
    exists to protect is dollars spent on Claude, not fetch count.
    """
    from trading_agents.strategies import best_strategy

    allow_shorts_env = env_flag("ALLOW_SHORTS")
    options_flag = env_flag("ALLOW_OPTIONS")
    out: dict[str, float | None] = {}
    convictions: dict[str, float] = {}
    for symbol in watchlist:
        instrument = (instrument_by_symbol or {}).get(symbol, "equity")
        options_eligible = options_flag and instrument == "option"
        try:
            features = feature_provider(symbol.upper(), "short")
            if inspect.isawaitable(features):
                features = await features
            winner, _ranked = best_strategy(
                features, priors=priors, allow_shorts=allow_shorts_env or options_eligible
            )
        except Exception:
            log.warning(
                "pre-pass scoring failed for %s — treating as free (real run will "
                "hit and log the same failure if it recurs)", symbol, exc_info=True,
            )
            winner = None
        out[symbol] = winner.score if winner is not None else None
        # Read defensively: test doubles for `best_strategy` predate this
        # field, and a missing conviction must degrade to "rank last among
        # equals", never raise into the sweep.
        convictions[symbol] = (
            float(getattr(winner, "conviction", 0.0) or 0.0) if winner is not None else 0.0
        )
    return out, convictions


async def _should_skip(user_id: str, symbol: str, instrument: str) -> bool:
    """Dedup gate. Equities: once per UTC day. Options: a cooldown."""
    if instrument != "option":
        if await _already_decided_today(user_id, symbol):
            log.info("skip %s — already decided today", symbol)
            return True
        return False

    cooldown = _options_rescan_cooldown_minutes()
    if cooldown <= 0:
        return False
    since = await get_decision_log().minutes_since_last_decision(
        user_id=user_id, symbol=symbol
    )
    if since is not None and since < cooldown:
        log.info(
            "skip %s — options cooldown (%.0f min since last look, need %d)",
            symbol, since, cooldown,
        )
        return True
    return False


def _equity_resolver(user_id: str):
    """Latest reconciler-snapshot equity for the cron user (Postgres only).
    The sizer needs REAL equity — synthetic 100k sizing against a real
    account was audit finding §5."""

    async def _resolve() -> float | None:
        if not env_flag("USE_POSTGRES"):
            return None
        import uuid as _uuid

        from sqlalchemy import desc, select

        from engine.db.models import PositionsSnapshot
        from engine.db.session import async_session_factory

        factory = async_session_factory()
        async with factory() as session:
            stmt = (
                select(PositionsSnapshot.account_equity)
                .where(PositionsSnapshot.user_id == _uuid.UUID(user_id))
                .order_by(desc(PositionsSnapshot.captured_at))
                .limit(1)
            )
            equity = (await session.execute(stmt)).scalar_one_or_none()
        return float(equity) if equity is not None else None

    return _resolve


def _notify_proposal(user_id: str, proposal: dict, push_tasks: list) -> None:
    """Fan out the 'new proposal' push. The audit's Break 4: cron proposals
    never notified anyone and expired unseen. Failure never fails the cron."""
    try:
        from app.services.notifications import schedule_proposal_pending_notification

        push_tasks.append(
            schedule_proposal_pending_notification(user_id=user_id, proposal=proposal)
        )
    except Exception:  # push is best-effort, council result is already durable
        log.exception("proposal push fan-out failed — continuing")


async def _run_one(
    user_id: str,
    symbol: str,
    llm: LLM,
    *,
    force: bool,
    feature_provider,
    push_tasks: list,
    instrument: str = "equity",
) -> dict:
    """Run the council for a single symbol. Skips if already decided
    today unless ``force=True``.

    ``instrument`` comes from the watchlist row's ``asset_class``. It is the
    only thing that lets a scheduled pass draft an option: ``strategy_fit``
    requires BOTH ``ALLOW_OPTIONS`` and an "option" preference before it
    will set ``instrument`` on the state.
    """
    if not force and await _should_skip(user_id, symbol, instrument):
        return {"symbol": symbol, "skipped": True}

    result = await run_council(
        symbol=symbol,
        user_id=user_id,
        llm=llm,
        feature_provider=feature_provider,
        decision_log=get_decision_log(),
        confidence_store=get_confidence_store(),
        instrument_preference=("option" if instrument == "option" else "equity"),
    )
    log.info(
        "%s: final_action=%s strategy=%s confidence=%.2f decision_id=%s",
        symbol,
        result.get("final_action"),
        result.get("selected_strategy"),
        result.get("selector_confidence", 0.0),
        result.get("decision_id"),
    )
    if result.get("proposal") is not None:
        _notify_proposal(user_id, result["proposal"], push_tasks)
    return {
        "symbol": symbol,
        "skipped": False,
        "final_action": result.get("final_action"),
        "selected_strategy": result.get("selected_strategy"),
        "decision_id": result.get("decision_id"),
    }


async def main(
    user_id: str,
    watchlist: list[str],
    *,
    force: bool,
    skip_calendar_gate: bool = False,
    skip_ghost_eval: bool = False,
    skip_reflect: bool = False,
    scan_context: Mapping[str, SymbolScanContext] | None = None,
    instrument_by_symbol: Mapping[str, str] | None = None,
) -> int:
    """Run the council across ``watchlist``. Returns a process exit code.

    ``scan_context`` is set by the continuous scanner: a triggered pass
    covers only the symbols that tripped a deterministic rule and forwards
    the rule identifiers to the analysts. A scheduled full sweep passes
    None and behaves exactly as before.

    ``instrument_by_symbol`` maps a symbol to its watchlist ``asset_class``
    ("equity" | "option"); anything absent defaults to equity. Threaded as a
    side mapping rather than by changing ``watchlist``'s element type,
    exactly like ``scan_context`` above — every existing caller that passes
    a plain ``list[str]`` keeps working untouched.

    **Deterministic pre-pass, then a budget-gated paid pass.** Before any
    LLM is spent, every symbol is scored via the same zero-cost
    ``best_strategy`` call ``strategy_fit_node`` itself uses (see
    ``_score_candidates_for_sweep``). A symbol that would legitimately
    HOLD always runs (costs nothing). Among the rest, only the top
    ``MAX_LLM_SYMBOLS_PER_SWEEP`` by score are admitted to the real
    (paid) path, and even an admitted symbol HOLDs uncosted once
    ``MAX_DAILY_LLM_SPEND_USD`` (real dollars, since midnight UTC) is
    reached — checked live, so it can trip mid-sweep. Neither gate is
    bypassed by ``force``: that flag overrides ONLY the dedup check
    below, on purpose — see the 2026-09-01 credit-exhaustion post-mortem
    (fable5findings.md) for why a budget escape hatch must not ride
    along with the operator's rerun flag.

    ``force`` and ``skip_calendar_gate`` are deliberately independent
    knobs, not two spellings of the same thing:

    - ``force`` is the operator's "run it anyway" — it bypasses BOTH the
      market-calendar gate below AND the per-(user, symbol, day) dedup
      check in ``_run_one``. That is the CLI/human-operator contract
      (``--force``: "rerun this symbol right now, I know it already ran")
      and it must keep bypassing dedup.
    - ``skip_calendar_gate`` bypasses ONLY the calendar gate, for a caller
      that has already established the market is open by other means (the
      scanner's trigger loop checks ``ScanResult.market_open`` itself) and
      just wants this redundant check skipped. It does NOT touch the dedup
      check — a symbol already decided today, whether by the baseline
      sweep or an earlier trigger, is still suppressed. That is what keeps
      the once-per-symbol-per-day cap ``engine.scanner.cooldown``'s
      docstring promises true regardless of which loop woke the council.
    """
    log.info(
        "daily cron start — user=%s symbols=%s use_postgres=%s triggered=%s",
        user_id,
        ",".join(watchlist),
        env_flag("USE_POSTGRES"),
        bool(scan_context),
    )

    # Market-calendar gate: no NYSE close today → nothing to decide. The
    # GitHub Actions schedule fires Mon-Fri regardless of holidays; this is
    # the deterministic gate the audit asked for. --force overrides it (and
    # the dedup check below); skip_calendar_gate overrides ONLY this gate,
    # for a caller that already knows the market is open.
    today = datetime.now(UTC).date()
    from engine.features import is_us_trading_day

    if not (force or skip_calendar_gate) and not is_us_trading_day(today):
        log.info("US market closed on %s — skipping council run", today)
        return 0

    # Both constructors hard-fail under the REQUIRE flags — a misconfigured
    # production cron must crash loudly, never degrade to mock/synthetic.
    try:
        llm = LLM()
        feature_provider = resolve_feature_provider(
            equity_resolver=_equity_resolver(user_id)
        )
    except RuntimeError:
        log.exception("daily cron refused to start (REQUIRE flag failed)")
        return 2
    log.info("LLM mode: %s", "MOCK" if llm.mock else "REAL")

    if scan_context:
        feature_provider = _with_scan_context(feature_provider, scan_context)

    # Deterministic pre-pass — see the docstring above and
    # _score_candidates_for_sweep. Priors fetched once, matching what
    # run_council itself will do per-admitted-symbol below (same store,
    # same shape) so pre-pass scores agree with the real ones that follow.
    priors: dict[str, float] = {}
    try:
        priors = {
            row.strategy_id: row.confidence for row in await get_confidence_store().all()
        }
    except Exception:
        log.warning("could not load strategy priors for pre-pass scoring", exc_info=True)

    scores, convictions = await _score_candidates_for_sweep(
        watchlist, instrument_by_symbol or {}, feature_provider, priors
    )
    # Ranked by CONVICTION first, then score, then symbol.
    #
    # `score` is a weighted MEAN of ~9 bounded components and therefore a
    # central statistic, which compresses: measured across 300 synthetic
    # symbols it spans 0.6075-0.6107, so sorting by it alone is decided by
    # the symbol tie-break rather than by quality. `conviction` measures
    # only the POSITIVE evidence and how far above neutral it sits, which
    # gives 5.6x the dispersion on that same set (0.0179 vs 0.0032) and 2x
    # on the eval archetypes. Score stays as the secondary key so the
    # ordering remains total and deterministic.
    #
    # UNVERIFIED on live features — both datasets measured here are
    # synthetic. See apps/agents/tests/eval/README.md.
    candidates = sorted(
        (sym for sym, score in scores.items() if score is not None),
        key=lambda sym: (-convictions.get(sym, 0.0), -scores[sym], sym),
    )
    # Quality floor, applied BEFORE the per-sweep cap so a weak setup
    # cannot occupy a slot that the caps then deny to a better one later
    # in the session. A filtered symbol is treated exactly like a
    # never-scored one: a free, uncosted HOLD.
    min_score = _min_llm_score()
    below_floor: set[str] = set()
    if min_score > 0.0:
        below_floor = {s_ for s_ in candidates if (scores[s_] or 0.0) < min_score}
        if below_floor:
            log.info(
                "MIN_LLM_SCORE=%.2f filtered %d of %d candidates before the "
                "sweep cap: %s", min_score, len(below_floor), len(candidates),
                ",".join(sorted(below_floor)),
            )
        candidates = [s_ for s_ in candidates if s_ not in below_floor]

    max_symbols = _max_llm_symbols_per_sweep()
    admitted = set(candidates[:max_symbols])
    # Score order for the paid loop below, best first. `admitted` is
    # already the top-N BY SCORE, but the loop used to walk `watchlist`
    # order, so which admitted symbols actually spent the day/hour budget
    # depended on their position in the watchlist rather than on how good
    # they were. With an 86-symbol options watchlist and 4 paid passes an
    # hour, that meant whichever names sat early in the list reliably got
    # the money.
    #
    # HONEST LIMIT, measured after this shipped (apps/agents/tests/eval):
    # passing scores cluster inside a 0.3% band (0.6075-0.6107 across 300
    # synthetic symbols), so candidates tie to three decimal places and
    # the sort is effectively decided by the symbol tie-break. This is
    # still strictly better than walking watchlist order — it is
    # deterministic and independent of list POSITION — but it does not
    # yet deliver "the caps ration to the best setups". It will only do
    # that once the score has real dynamic range; see
    # `_DEFAULT_MIN_LLM_SCORE` for the measurement and what it implies.
    admitted_rank = {sym: i for i, sym in enumerate(candidates[:max_symbols])}
    if len(candidates) > max_symbols:
        log.warning(
            "%d symbols cleared strategy_fit this sweep — admitting only the "
            "top %d by score (MAX_LLM_SYMBOLS_PER_SWEEP=%d); %d HOLD uncosted "
            "this sweep: %s",
            len(candidates), max_symbols, max_symbols,
            len(candidates) - max_symbols,
            ",".join(candidates[max_symbols:]),
        )

    max_spend = _max_daily_llm_spend_usd()
    max_runs_per_day = _max_llm_symbols_per_day()
    max_runs_per_hour = _max_llm_symbols_per_hour()
    from trading_agents.cost_ledger import get_cost_ledger

    ledger = get_cost_ledger()
    budget_tripped = False
    day_cap_tripped = False
    hour_cap_tripped = False
    # Paid passes already spent TODAY, before this sweep adds any. Read
    # once here and incremented locally per admitted candidate rather than
    # re-queried every symbol: the ledger write for a pass lands during
    # that pass, so re-reading mid-loop would race its own writes and
    # under-count. Re-read fresh on the NEXT call to main(), which is what
    # makes this a real cross-invocation daily cap and not a per-sweep one.
    try:
        runs_today = await ledger.count_runs_since(_seconds_since_midnight_utc())
    except Exception:
        # Start from zero rather than bail. The local increment below still
        # applies, so a ledger outage DEGRADES this from a per-day cap to a
        # per-sweep one (bounded at max_runs_per_day for this invocation)
        # instead of removing it — strictly safer than not enforcing, and
        # honest about what it can still promise: across sweeps it is
        # unenforceable without the ledger, within one it still holds.
        log.exception(
            "count_runs_since failed — MAX_LLM_SYMBOLS_PER_DAY degrades to a "
            "per-sweep cap of %d for this invocation", max_runs_per_day,
        )
        runs_today = 0
    try:
        runs_this_hour = await ledger.count_runs_since(timedelta(hours=1))
    except Exception:
        log.exception(
            "count_runs_since(1h) failed — hourly pacing degrades to "
            "per-sweep for this invocation"
        )
        runs_this_hour = 0

    push_tasks: list = []
    rolled_up: list[dict] = []
    # Sequential — Anthropic prompt-caching benefits from steady cadence
    # within ~30s windows. Parallel would burn separate cache entries.
    # Admitted symbols first, best score first; everything else after, in
    # watchlist order. Only the admitted ones can consume budget, so this
    # ordering is what decides where the money goes when a cap trips
    # mid-loop. The others still run — they HOLD for free — so no symbol
    # is dropped, only re-ordered.
    sweep_order = sorted(
        watchlist,
        key=lambda sym: (admitted_rank.get(sym, len(admitted_rank)), watchlist.index(sym)),
    )
    for symbol in sweep_order:
        try:
            score = scores.get(symbol)
            if symbol in below_floor:
                # Its own reason, not the cap's. "We ran out of budget" and
                # "this setup was not good enough to pay for" are different
                # facts about the desk, and the Refusal Ledger is the whole
                # differentiator here — collapsing them would misreport a
                # deliberate quality decision as a resource limit.
                rolled_up.append({
                    "symbol": symbol, "skipped": True,
                    "skip_reason": "below_min_llm_score",
                })
                continue
            if score is not None and symbol not in admitted:
                rolled_up.append({
                    "symbol": symbol, "skipped": True,
                    "skip_reason": "llm_symbol_cap_reached",
                })
                continue
            if score is not None:
                # Only an admitted, LLM-eligible symbol needs a live budget
                # check — a free HOLD never reaches this branch at all.
                if not day_cap_tripped and runs_today >= max_runs_per_day:
                    day_cap_tripped = True
                    log.warning(
                        "MAX_LLM_SYMBOLS_PER_DAY=%d reached (%d paid council "
                        "passes since midnight UTC) at %s — every remaining "
                        "candidate HOLDs uncosted until tomorrow",
                        max_runs_per_day, runs_today, symbol,
                    )
                if day_cap_tripped:
                    rolled_up.append({
                        "symbol": symbol, "skipped": True,
                        "skip_reason": "llm_daily_symbol_cap_reached",
                    })
                    continue
                if not hour_cap_tripped and runs_this_hour >= max_runs_per_hour:
                    hour_cap_tripped = True
                    log.info(
                        "MAX_LLM_SYMBOLS_PER_HOUR=%d reached (%d paid passes in "
                        "the last hour) at %s — pacing the daily budget; "
                        "candidates resume next hour",
                        max_runs_per_hour, runs_this_hour, symbol,
                    )
                if hour_cap_tripped:
                    rolled_up.append({
                        "symbol": symbol, "skipped": True,
                        "skip_reason": "llm_hourly_symbol_cap_reached",
                    })
                    continue
                if not budget_tripped:
                    spent, _ = await ledger.sum_cost_since(_seconds_since_midnight_utc())
                    if spent >= max_spend:
                        budget_tripped = True
                        log.warning(
                            "MAX_DAILY_LLM_SPEND_USD=$%.2f reached ($%.2f spent "
                            "since midnight UTC) at %s — every remaining admitted "
                            "candidate this sweep HOLDs uncosted",
                            max_spend, spent, symbol,
                        )
                if budget_tripped:
                    rolled_up.append({
                        "symbol": symbol, "skipped": True,
                        "skip_reason": "llm_daily_budget_exhausted",
                    })
                    continue
            if score is not None:
                # Counts the pass we are ABOUT to spend. Incremented before
                # the await, not after, so a pass that raises still counts —
                # it consumed real model calls either way.
                runs_today += 1
                runs_this_hour += 1
            rolled_up.append(
                await _run_one(
                    user_id, symbol, llm,
                    force=force,
                    feature_provider=feature_provider,
                    push_tasks=push_tasks,
                    instrument=(instrument_by_symbol or {}).get(symbol, "equity"),
                )
            )
        except Exception as exc:
            log.exception("council failed for %s — continuing", symbol)
            rolled_up.append({"symbol": symbol, "skipped": False, "error": str(exc)})

    # Push fan-outs are fire-and-forget tasks — drain them before the
    # process exits or the notifications die with the event loop.
    if push_tasks:
        results = await asyncio.gather(*push_tasks, return_exceptions=True)
        sent = sum(1 for r in results if not isinstance(r, Exception))
        log.info("proposal pushes drained — %d/%d ok", sent, len(push_tasks))

    processed = sum(1 for r in rolled_up if not r.get("skipped") and "error" not in r)
    skipped = sum(1 for r in rolled_up if r.get("skipped"))
    failed = sum(1 for r in rolled_up if "error" in r)
    budget_capped = sum(
        1 for r in rolled_up if r.get("skip_reason") == "llm_daily_budget_exhausted"
    )
    symbol_capped = sum(
        1 for r in rolled_up if r.get("skip_reason") == "llm_symbol_cap_reached"
    )
    log.info(
        "daily cron done — processed=%d skipped=%d failed=%d "
        "(budget_capped=%d symbol_capped=%d)",
        processed, skipped, failed, budget_capped, symbol_capped,
    )

    # Ghost P&L pass — marks vetoed/declined picks against daily closes.
    # Postgres only (the ghost_outcomes table); failure never fails cron.
    if not skip_ghost_eval and env_flag("USE_POSTGRES"):
        try:
            from trading_agents.jobs.ghost_eval import evaluate_ghosts

            await evaluate_ghosts()
        except Exception:
            log.exception("ghost_eval pass failed — continuing")

    # Reflection pass — the worker existed but was never scheduled, so
    # strategy-confidence priors never moved off 0.5 (audit roadmap P1).
    # Runs after ghost eval on the same closed-trade window; failure never
    # fails the cron. Skipped with --no-reflect.
    #
    # since=30d, not the tighter 24h this used to hardcode: this job isn't
    # actually scheduled in production today (COUNCIL_SCHEDULER_ENABLED),
    # so a run only happens when someone invokes this by hand — sometimes
    # days apart. A 24h window combined with that made every real close
    # permanently unreachable the moment it happened outside that one-day
    # gap; reviewed_at IS NULL (in list_pending_reflection) is what
    # actually prevents re-grading, not this window — see that function's
    # own docstring in memory/postgres.py for the live numbers that proved
    # it.
    if not skip_reflect:
        try:
            from trading_agents.nodes import reflection_agent_run

            summary = await reflection_agent_run(
                llm=llm,
                decision_log=get_decision_log(),
                confidence_store=get_confidence_store(),
                since=timedelta(days=30),
            )
            log.info("reflection pass — reviewed=%d", summary.get("reviewed", 0))
        except Exception:
            log.exception("reflection pass failed — continuing")

    # Kick the Langfuse export before this short-lived process exits.
    try:
        from trading_agents.tracing import flush as _trace_flush

        _trace_flush()
    except Exception:
        log.debug("trace flush failed", exc_info=True)

    return 1 if failed else 0


def cli() -> int:
    """Parse argv and run the daily pass. Returns a process exit code.

    Non-zero only when a symbol actually failed — a weekend/holiday
    short-circuit is a successful no-op, so cron does not alert on it.
    """
    parser = argparse.ArgumentParser(description="Daily council cron.")
    parser.add_argument(
        "--user-id",
        default=os.environ.get(
            "AGENT_CRON_USER_ID",
            "00000000-0000-0000-0000-000000000001",
        ),
        help="User ID to attribute decisions to. Defaults to the fixture user.",
    )
    parser.add_argument(
        "--watchlist",
        default=os.environ.get("AGENT_CRON_WATCHLIST", ",".join(DEFAULT_WATCHLIST)),
        help="Comma-separated tickers.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even when a decision already exists for (user, symbol) today.",
    )
    parser.add_argument(
        "--skip-ghost-eval",
        action="store_true",
        help="Skip the ghost-P&L marking pass after the council loop.",
    )
    parser.add_argument(
        "--no-reflect",
        action="store_true",
        help="Skip the EOD reflection pass (strategy-confidence update).",
    )
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    if not symbols:
        log.error("empty watchlist — pass --watchlist or set AGENT_CRON_WATCHLIST")
        return 2

    return asyncio.run(
        _run_cli(
            args.user_id,
            symbols,
            force=args.force,
            skip_ghost_eval=args.skip_ghost_eval,
            skip_reflect=args.no_reflect,
        )
    )


async def _run_cli(
    user_id: str,
    symbols: list[str],
    *,
    force: bool,
    skip_ghost_eval: bool,
    skip_reflect: bool,
) -> int:
    """Load the watchlist and run the pass on ONE event loop.

    These were two ``asyncio.run`` calls. ``engine.db.session`` caches a
    single AsyncEngine per process, so the first call bound its asyncpg
    connections to a loop that the second call had already closed — every
    subsequent pool checkout raised "attached to a different loop". The
    visible damage was the equity resolver: it failed on every run, was
    caught by its own fallback, and sized the whole sweep against the
    100k fixture instead of real account equity. That is the exact
    failure the resolver was written to prevent.
    """
    # The user's curated watchlist (user_watchlist table) overrides the
    # static default when it exists — that's the product: "tell the agent
    # what you're interested in, it tracks those."
    instrument_by_symbol: dict[str, str] = {}
    if env_flag("USE_POSTGRES"):
        try:
            curated = await _load_user_watchlist(user_id)
        except Exception:  # fall back to the CLI/default list
            log.exception("user watchlist load failed — using default list")
            curated = []
        if curated:
            symbols = [sym for sym, _ in curated]
            instrument_by_symbol = {sym: ac for sym, ac in curated}
            n_opt = sum(1 for ac in instrument_by_symbol.values() if ac == "option")
            log.info(
                "using user watchlist (%d symbols, %d options): %s",
                len(symbols), n_opt, ",".join(symbols),
            )

    return await main(
        user_id,
        symbols,
        force=force,
        skip_ghost_eval=skip_ghost_eval,
        skip_reflect=skip_reflect,
        instrument_by_symbol=instrument_by_symbol,
    )


async def _load_user_watchlist(user_id: str) -> list[tuple[str, str]]:
    """Active ``(symbol, asset_class)`` pairs, alphabetical. Empty when
    uncurated.

    ``asset_class`` was persisted and surfaced in the UI from the start but
    never read back here, so a watchlist row marked 'option' still produced
    an equity council pass. It is the seam that makes options reachable from
    the scheduler at all.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from engine.db.models import UserWatchlistItem
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        stmt = (
            select(UserWatchlistItem.symbol, UserWatchlistItem.asset_class)
            .where(UserWatchlistItem.user_id == _uuid.UUID(user_id))
            .where(UserWatchlistItem.active.is_(True))
            .order_by(UserWatchlistItem.symbol)
        )
        rows = (await session.execute(stmt)).all()
    return [(str(sym).upper(), str(ac or "equity")) for sym, ac in rows]


if __name__ == "__main__":
    sys.exit(cli())
