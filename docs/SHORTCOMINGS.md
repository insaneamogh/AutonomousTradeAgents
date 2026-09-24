# Shortcomings — what is wrong or missing, verified against the code

> **Audited 2026-09-24 at `262bba9e`** (tip of `main` after PLAN_PLATFORM
> Phases 0–4 and most of 6). Every item cites the code it was checked in.
> Docs, docstrings and commit messages were not trusted (CLAUDE.md §4.2).
>
> **How to read the tags:**
> - **[measured]** — reproduced by running code in this audit; the number is quoted.
> - **[read]** — confirmed by reading the code path end to end, not executed.
> - **[new]** — not listed in `docs/PLAN_PLATFORM.md` or the build log before this audit.
>
> Nothing here was checked against the live Railway deployment or a live
> broker. Where a finding depends on production env vars, it says so.
>
> Baseline at audit time: `pytest apps/agents apps/api packages/` →
> **1833 passed, 11 skipped, 0 failed** [measured].

---

## 0. The ranking, in one table

| # | Shortcoming | Severity | Tag |
|---|---|---|---|
| 1 | No trading signal has an edge | Blocks the product | measured |
| 2 | Live money is reachable through `ALPACA_BASE_URL` alone, bypassing the two-key live gate | Safety | read · new |
| 3 | The kill switch does not stop the options auto-trade path | Safety | read · new |
| 4 | Once acknowledged, the drawdown breaker never re-arms | Safety | read · new |
| 5 | With no LLM key, the equity council emits canned trades that clear the confidence floor | Safety | read · new |
| 6 | Daily bars are not split-adjusted, in the live feature path and in the backtest fixture | Correctness | measured · new |
| 7 | The backtest's t-stats treat same-day signals across 58 correlated names as independent | Research validity | measured · new |
| 8 | `wash_sale` is inert on the Postgres (production) path | Correctness | read · new |
| 9 | The quote-freshness gate's env switch is not wired to anything | Correctness | read · new |
| 10 | No option assignment / exercise / expiry handling; the DTE sweep skips manual positions | Execution | read |
| 11 | Partial fills, `pending_cancel` and `replaced` are mishandled | Execution | read · new |
| 12 | Nothing is multi-instance safe, and nothing fails if you run two | Ops | read |
| 13 | Options skip correlation, sector and single-name rules; no Greeks limits | Risk model | read |
| 14 | The data layer is thin: IEX-only bars, delayed options, no IV rank, no fundamentals, no macro calendar | Data | read |
| 15 | No CI; the README and several docs describe a system that no longer exists | Process | read |

§1–§7 give the evidence. §8 lists what this audit did **not** verify.

---

## 1. Strategy and research

### 1.1 No trading signal has an edge [measured]

`apps/agents/tests/eval/signal_backtest.py` reproduces exactly: 13,403
signals, 58 symbols, 2020-07 → 2026-09, hit rate 49.3–50.9% at every
horizon, every horizon FAILS the acceptance bar. The option backtest
(`option_backtest.py`) shows the shipped signal losing 5–8% of premium per
trade while a perfect-direction oracle keeps +37–43%. Two pre-registered
alternatives (5-day reversal, 12-1 momentum) also fail (`candidates.py`).
This is already the headline of `docs/PLAN_PLATFORM.md`. It is repeated
here because every other item on this list is secondary to it.

### 1.2 The backtest fixture has 13 unadjusted stock splits [measured · new]

`apps/agents/tests/eval/fixtures/bars.json.gz` was fetched with
`feed=iex` and no `adjustment` parameter (`fetch_bars.py:41-43`). Alpaca's
default is raw prices. Scanning consecutive closes for a >40% jump finds
13 discontinuities that are splits, not moves:

```
AAPL 2020-08-31 499.35 → 129.11     NVDA 2021-07-20 751.07 → 186.06
NVDA 2024-06-10 1208.42 → 121.93    AMZN 2022-06-06 2446.41 → 124.79
GOOGL 2022-07-18 2235.49 → 109.05   TSLA 2020-08-31 2215.57 → 498.71
TSLA 2022-08-25 890.91 → 296.11     AVGO 2024-07-15 1698.62 → 171.48
NFLX 2025-11-17 1112.11 → 110.31    WMT 2024-02-26 175.63 → 59.57
GE 2021-08-02 12.96 → 100.58 (reverse)
XLE 2025-12-05 92.24 → 45.93        XLK 2025-12-05 291.01 → 146.58
```

Any signal whose forward window spans one of these dates records a
−50% to −90% "return" (or +680% for GE). SMA, ATR, Donchian and 12-month
momentum features are also wrong for up to a year after each split.
`_load()` (`signal_backtest.py:72-84`) does nothing about it.

**Effect on the headline finding:** see §1.4 for the split-adjusted re-run.

### 1.3 The live feature path fetches unadjusted bars too [read · new]

`packages/engine/engine/features/bars.py:104-110` builds a
`StockBarsRequest` with `feed=DataFeed.IEX` and no `adjustment=`; alpaca-py's
default is `None`, i.e. raw. The 400-day lookback (`DEFAULT_LOOKBACK_DAYS`)
means any name that split in the last ~13 months is scored on a price
series with a cliff in it. The scanner's `gap_up_2pct` / `atr_expansion_1_5x`
triggers fire on the split day itself. `features/corporate_actions.py:13-14`
states "The bars provider back-adjusts history". That is false.

Fix: `adjustment=Adjustment.ALL` (or `SPLIT`) in both `bars.py` and
`fetch_bars.py`, refetch the fixture, re-run every backtest.

### 1.4 The acceptance bar overstates significance [measured · new]

`non_overlapping()` (`signal_backtest.py:164`) removes overlap **per
symbol only**. On a given day the bar still counts up to ~15 signals across
SPY-correlated large caps (plus QQQ, DIA, IWM, XLK, XLF, XLE, XLV) as
independent observations, and `acceptance.t_stat` pools them. Averaging
signals within each day first (a date-clustered t) gives:

| horizon | n pooled | trading days | pooled t (shipped) | date-clustered t |
|---|---|---|---|---|
| 2d | 13,403 | 919 | −4.60 | −2.54 |
| 5d | 7,500 | 669 | −2.38 | −1.97 |
| 10d | 5,203 | 588 | −1.62 | −1.93 |
| 20d | 2,882 | 442 | +0.49 | −1.37 |
| 60d | 1,083 | 200 | +0.81 | +0.48 |

(Net of 10 bps; raw, unadjusted fixture; script in the build log.)

- "No edge" survives: nothing gets closer to a pass.
- "The shipped 2-day signal is *significantly negative*" (the Phase 3
  build-log claim, t = −4.60) does not survive the repo's own Bonferroni
  bar of 2.58 once clustered.
- **The bigger risk is the other direction.** The bar is the gate for
  every future candidate ("any future claim of an edge must come through
  here first"). Pooled t inflates a real-looking edge by roughly √(signals
  per day). A candidate could pass it that would fail a clustered test.
  Fix the bar before running the next candidate through it.

**Does "no edge" survive split adjustment? Yes [measured].** Re-running
the shipped backtest on bars back-adjusted at the 13 discontinuities above
(same code, same acceptance bar, pooled t):

| horizon | raw: hit / pooled t | split-adjusted: hit / pooled t | verdict |
|---|---|---|---|
| 2d | 50.1% / −4.60 | 50.2% / −5.12 | FAIL both |
| 5d | 49.8% / −2.38 | 50.0% / −1.71 | FAIL both |
| 10d | 49.8% / −1.62 | 49.9% / −0.99 | FAIL both |
| 20d | 50.9% / +0.49 | 51.0% / +1.12 | FAIL both |
| 60d | 49.3% / +0.81 | 49.2% / +0.96 | FAIL both |

The conclusion is robust. The individual t-values are not: 20d more than
doubles. The fixture still has to be refetched adjusted before any
*candidate* is judged on it.

### 1.5 The backtest universe is small and survivorship-biased [read]

58 hard-coded symbols (`fetch_bars.py:27-37`), all chosen in 2026 as
today's liquid large caps and ETFs. There are no delisted names and no
mid caps, and the bars are IEX-only (IEX's own trades, a few percent of
consolidated volume). The option backtest prices every contract off
IV = 20-day realized vol × 1.15 at |Δ| 0.45 (`option_backtest.py:17-27`).
By construction it cannot see IV-rich or far-OTM contracts, which is
exactly what the new `expected_move_below_breakeven` gate targets. The
build log already concedes this. It is listed here because it bounds what
any "pass" can mean.

### 1.6 The LLM adds no information the tape doesn't have [read]

Bull and Bear read the same daily-bar feature dict. The options prompts get
news **counts**, never headline text (`options/agents.py:193`). The
fundamental analyst has never had data: `FundamentalsProvider` is a
Protocol with no implementation (`features/provider.py:103`),
`feature_provider_from_env()` is called without `fundamentals=`, and the
router drops the analyst on real data (`nodes/router.py:47-49`). The event
interpreter that PLAN_PLATFORM Phase 5b intends to replace this with does
not exist yet (no `nodes/event_interpreter.py`, no `llm_event_veto` rule).

### 1.7 The learning loop does not close [read]

- Reflection runs only at the tail of the baseline sweep
  (`jobs/daily_cron.py:1074-1086`); triggered runs pass `skip_reflect=True`.
- Priors only scale fit scores within 0.6–1.15 (`strategies/fit.py:90-91`).
  The options agents never see them.
- `lessons` / `notes` are stored (`nodes/reflection.py:123-155`) and read
  by no prompt anywhere.
- The mock reflection returns `confidence_delta +0.04` every time
  (`llm.py:979-993`). Mock runs are not excluded, so since the key was
  removed on 2026-09-11 priors can only drift upward.
- The forecast scorecard (`tests/eval/forecast_scorecard.py`) is written
  but has never been run on production data.

---

## 2. Safety — paths that should be closed and are not

### 2.1 Live money via `ALPACA_BASE_URL`, bypassing the two-key gate [read · new]

The intended design (`services/orders/live_trading_gate.py`): real-money
orders need BOTH `LIVE_TRADING_ENABLED=1` AND a per-connection
`live_trading_consent`, and the gate checks `conn.is_paper`.

The hole:
1. The env-key Alpaca connection row is always created with
   `is_paper=True` (`services/broker/env_bootstrap.py:144`).
2. Every order on that connection builds its client with
   `AlpacaBroker.from_env()` (`services/broker/broker_use.py:154`), which
   decides paper vs live **at call time** from `ALPACA_BASE_URL`
   (`packages/broker/broker/alpaca.py:221-223`).
3. The live gate checks only the stored `is_paper` flag
   (`live_trading_gate.py:43`), so it passes.

So if an existing env-key row is present and the operator swaps in live
keys plus `ALPACA_BASE_URL=https://api.alpaca.markets`, orders go to the
live account with neither `LIVE_TRADING_ENABLED` nor consent.
`env_bootstrap.py:113` only refuses to **create** a row under a live URL.
The options guard has the same shape: `_is_paper_and_safe()`
(`options/tools/guard.py:213`) checks `TRADING_MODE` and
`LIVE_TRADING_ENABLED`, then builds its broker with `from_env()`
(`guard.py:246`).

It needs a deliberate config change (live keys) to trigger. But it
defeats a gate whose whole purpose is catching deliberate-looking config
changes. Fix: derive `paper` from the connection row, not the env, and
refuse in `from_env()` when the URL and the row disagree.

Separately: the old README claim "no path in this repo can reach a live
account by setting an environment variable" was never true. Human-approved
live trading exists by design: a live connection + `LIVE_TRADING_ENABLED=1`
+ per-connection consent. `TRADING_MODE` does not gate that path; with
`TRADING_MODE=paper` the executor still sends to whatever broker is
connected (`services/orders/executor.py:141-146`). The **unattended** paths
(auto-approver, options guard) are the ones that refuse unless
`TRADING_MODE=paper`.

### 2.2 The kill switch does not stop options auto-trading [read · new]

`POST /api/v1/positions/flatten-all` (`services/orders/kill_switch.py:36`)
revokes `auto_approve_consent` and closes every position. But the options
council's auto-trade path never reads consent. `ToolGuard` checks only
`env_flag("AUTO_TRADE_ENABLED")`, paper mode and market hours
(`options/tools/guard.py:567-572`). With `AUTO_TRADE_ENABLED=1`, the next
scheduled or triggered options pass can open a new position minutes after
the book was flattened. The scheduler itself is not paused, and human
approvals still work. There is also no UI button: nothing under
`apps/mobile` calls `flatten-all`.

Fix: a persisted per-user `trading_paused` flag that the kill switch sets
and that the guard, the auto-approver and the scheduler all check.

### 2.3 The drawdown breaker never re-arms after acknowledgement [read · new]

- Acknowledging sets `status="manual_override"`
  (`services/platform/circuit_breaker_service.py:92`).
- The trip check returns early for any status other than `"normal"`
  (`packages/engine/engine/reconciler/breaker.py:61`).
- The only code that sets `"normal"` again is the account-switch path, and
  it only matches `status == "halted"` (`services/orders/account_switch.py:170`).

So after the first acknowledgement the persistent halt, the banner and the
`breaker_tripped` ops alert can never fire again for that user. The
per-proposal `drawdown_halt` rule (`risk/rules/drawdown_halt.py:63`) still
refuses new entries on a −3% day, so entries stay protected. What is lost
is the latch, the alert and the operator's visibility. This matters now:
the breaker latched on 2026-09-08, and the next step in the plan is to
acknowledge it.

Fix: reset `manual_override` → `normal` at the next session open.

### 2.4 No LLM key means canned trades unless one env var is set [read · new]

With no key, a placeholder key or no SDK, `LLM` silently enters MOCK mode
(`llm.py:285-297`). It only hard-fails if `AGENTS_REQUIRE_REAL_LLM=1`,
which `.env.example:114` defaults to `0`. The mock equity drafter returns
confidence **0.58** in the requested direction (`llm.py:906-911`), above the
0.50 `min_council_confidence` floor (`risk/types.py:125`). The
auto-approver has no mock check. On the equity path, a keyless production
box with `AUTO_APPROVE_ENABLED=1` and consent can therefore execute
proposals whose thesis is canned text.

The options path is safe: mock mode never emits a `tool_use` block, so
`open_option_trade` is never called. The build log says the key has been
absent since 2026-09-11. Whether production sets
`AGENTS_REQUIRE_REAL_LLM=1` was **not checked**.

Fix: default the REQUIRE flag on when `ENV=production`, and make the
auto-approver refuse any decision persisted with `llm_mode=MOCK`.

---

## 3. Risk engine

### 3.1 `wash_sale` is inert in production [read · new]

`packages/engine/engine/risk/postgres_context.py:96-101`:
`_recent_losing_closes()` is a `TODO(Phase 1.5)` that returns `()`. The
rule passes every proposal on the Postgres path. Only the mock context in
tests ever exercises it.

### 3.2 Options skip the portfolio-shape rules [read]

- Options proposals return early at `risk/engine.py:123-129`, before
  `correlation_cap`, `sector_concentration` and `single_name_concentration`
  run (`:233-250`).
- The options side has only a per-underlying premium cap and a
  calls-vs-puts cap (`options/rules/concentration.py:106,140`).
- Stock and option exposure on the same name are never added together:
  `single_name.py:21` compares an OCC symbol to a ticker.
- There are no net delta / vega / theta limits anywhere; the aggregate
  premium cap stands in (`options/rules/max_total_premium_pct.py:11-19`).
- The sector and cluster maps are hand-curated, and everything else
  falls into "other" (`risk/assets.py:12-70`).

### 3.3 The earnings blackout is narrow [read]

The Finnhub calendar (`features/earnings.py`) is correctly wired into both
the drafter and the guard paths, and self-disables with no
`FINNHUB_API_KEY`. But:
- The window is ±2 days (`risk/types.py:291`), so a 20–60 DTE option bought
  3 days before a report holds straight through it.
- Equities have no earnings rule at all (`earnings_blackout.py:23`).
- `corporate_actions.py:76` still hard-codes `earnings_date_known=False`,
  and that is what the options agents are shown (`options/agents.py:195`),
  even when Finnhub supplied a date.

### 3.4 The quote-freshness gate cannot be switched on [read · new]

`OPTIONS_MAX_QUOTE_AGE_SECONDS` is parsed into
`RiskCaps.options_max_quote_age_seconds` (`risk/types.py:424,775`). But the
gate in `select_contract` runs only when the **caller** passes
`SelectionInputs.max_quote_age_seconds` (`options/selection.py:448`), and no
production caller does: both `guard.py:823-833` and `drafter.py:406-416`
omit it. Setting the env var does nothing. `selection.py:300` claims
"Production passes `caps.options_max_quote_age_seconds`", which is false.
CLAUDE.md's "ships DISABLED; see RAILWAY_CHECKS §3 before enabling"
implies a switch that is not connected.

### 3.5 Missing gates (known, deferred)

- No `market_regime` gate: no SPY 200-day trend or VIX term structure
  anywhere; VIX is a spot level shown to the LLM (`features/macro.py:255`).
- No FOMC / CPI / NFP calendar: `features/macro.py:49` fetches only
  VIXCLS, DGS10 and DTWEXBGS.
- No `iv_rank_regime`: see §5.2.

PLAN_PLATFORM defers these until a signal has an edge, which is
reasonable. They are listed so nobody mistakes them for built.

---

## 4. Execution and position lifecycle

### 4.1 No assignment, exercise or expiry handling [read]

Nothing reads Alpaca account activities (OPASN / OPEXC / OPEXP). A
position that is assigned, exercised or expires simply disappears and
is stamped `close_reason="external_broker"`, with P&L estimated from the
last snapshot mark (`services/orders/order_sync.py:427,445-453`). The
DTE≤2 expiry sweep is the only defence. **It only covers
`exit_mode == "agent"`** (`position_manager.py:550`), so manual-mode and
unmanaged option positions get no expiry protection at all [new].

### 4.2 Protective-stop fills are labelled `user_manual` [read]

The resting stop is saved as a linked SELL row with no close reason
(`option_stops.py:306-323`). When it fills, `_apply_decision_lifecycle`
defaults to `close_reason = "user_manual"` (`order_sync.py:249-250`).
Stop-outs are therefore misattributed in the closed-trade history and in
any exit analysis built on it.

### 4.3 Partial fills and odd order states [read · new]

- The decision record updates only on `filled` (`order_sync.py:149-150`).
  An entry or exit canceled or expired after a partial fill never reaches
  the decision. Entries are healed only indirectly, by orphan adoption.
- The close retry uses `decision.fill_qty`, not the quantity actually
  held (`position_manager.py:1188`).
- `realized_pnl` uses only the final exit order (`order_sync.py:233-247`).
- Partial external reductions are ignored (`order_sync.py:403-406`).
- `pending_cancel` maps to CANCELED (`broker/alpaca.py:130`). Polling
  stops, so a fill that lands during the cancel is lost.
- `pending_replace` / `replaced` map to ACCEPTED (`alpaca.py:136-137`). A
  replaced order reads as "in flight" forever, which blocks that
  decision's close path (`position_manager.py:970-1000`).
- The PDT ledger uses same-**UTC**-calendar-day, not NYSE trading days
  (`order_sync.py:289-294`).

### 4.4 Zerodha is wired for execution but never reconciled [read]

`packages/broker/broker/zerodha.py` (Kite Connect, India) is live-only
(`routers/broker.py:416,483` set `is_paper=False`) and wired into
connect and execute behind the live gate. The reconciler fleet lists only
Alpaca connections (`reconciler_fleet.py:200`), and order sync is
Alpaca-only. A Zerodha order would be placed and then never tracked.

---

## 5. Data

### 5.1 Feeds [read]

- Stock bars are IEX-only (`features/bars.py:109,183,342`,
  `microstructure.py:159`). There is no SIP option in code, so buying the
  Algo Trader Plus plan (PLAN_PLATFORM §D P0) would not change the bars
  without a code change.
- Options default to the 15-minute-delayed indicative feed
  (`broker/alpaca.py:792-799`).
- Bars are unadjusted (§1.3).

### 5.2 IV history is collected and read by nothing [read]

`jobs/iv_snapshot.py:113` inserts into `iv_history` daily. Nothing selects
from it. `options_context` still returns `iv_rank`, `atm_iv` and
`term_structure_slope` as literal `None` (`features/provider.py:187-189`).
`engine/options/iv_surface.py:103 iv_rank()` is called only from tests.
The `get_iv_rank` agent tool uses a process-local dict that empties on
restart (`options/tools/readonly.py:326`). The plan defers the consumer
until ~60 days exist, which is fine. The IV snapshot loop has no restart
catch-up (`scheduler.py:440-456`), though, so a deploy after 20:15 UTC
loses that day for good, and a lost day can never be backfilled from the
free feed.

---

## 6. Operations

### 6.1 Single-instance only, with nothing enforcing it [read]

- No leader election or advisory lock anywhere
  (`council/scheduler.py:14-19` documents the single-instance assumption).
- `apps/api/scripts/start.sh:92` runs `--workers "${UVICORN_WORKERS:-1}"`,
  and every worker's lifespan starts its own fleet and scheduler
  (`main.py:136-218`). Setting `UVICORN_WORKERS=2` doubles every sweep.
- The daily dedup is check-then-act with no unique constraint behind it
  (`jobs/daily_cron.py:150-161`). Two instances would both run the
  council, doubling decisions and LLM spend.
- Entry orders are protected by a DB compare-and-swap
  (`execution_claim.py`). Closes and the protective stop's
  cancel-then-place (`option_stops.py:250-284`) are not.
- The OAuth pending cache and the auth rate limiter are in-memory
  (`broker_store.py:212`, `auth/rate_limit.py:5`).

### 6.2 Scheduling [read]

- All loops are in-process `asyncio.create_task`. There is no persistent
  job queue. `redis` is a declared dependency in both `apps/api` and
  `apps/agents` and is imported nowhere.
- There is no end-of-day job. Ghost eval and reflection run at the tail
  of the baseline sweep, which defaults to 14:00 UTC (mid-morning ET), not
  after the close.
- There is no daily P&L push. The notification kinds are: proposal
  pending, position events, Zerodha reconnect, and the ops alerts
  (`notifications/notifications.py`).

### 6.3 No CI [read · new]

There is no `.github/workflows` (no `.github` directory at all). The 1833
tests, the ruff baseline, `tsc` and Jest run only when a session runs
them by hand. CLAUDE.md §4.1's standard (revert-check every fix) is
enforced by discipline alone, and one pushed regression reaches Railway
unchecked.

### 6.4 Dependency and config drift [read]

- `litellm` is declared (`apps/agents/pyproject.toml:10`) and locked, and
  imported nowhere.
- `railway.toml` says "See `RAILWAY.md` for the required set". That file
  does not exist; the env reference is `apps/api/.env.example`.
- `.env.example` does not mention `AUTO_TRADE_ENABLED`,
  `USE_OPTIONS_AGENT`, `ALPACA_OPTIONS_FEED`, `OPTIONS_MAX_QUOTE_AGE_SECONDS`
  or `AUTO_APPROVE_ENABLED`, which are the switches that decide whether
  the system trades at all.

---

## 7. Documentation drift

The code is ahead of most docs. The ones most likely to mislead:

| Doc / docstring | Says | Reality |
|---|---|---|
| `README.md` (before this audit) | Hackathon pitch, "1511 passed", "judges start here" | Hackathon ended Sep 4; the work queue is PLAN_PLATFORM; 1833 tests. **Rewritten in this commit.** |
| `README.md` (before) | "`earnings_blackout` is wired and permanently inert" | Finnhub calendar shipped 0a9fe57e (needs `FINNHUB_API_KEY`) |
| `README.md` (before) | "No path can reach a live account by setting an env var" | See §2.1 |
| `README.md` (before) | Scanner "every 2 minutes" | `SCANNER_INTERVAL_MINUTES` default 5 (`scheduler.py:514`) |
| `docs/README.md` | "17 equity / 13 options rules"; "as of the last 24 hours" | Stale counts and dates |
| `CLAUDE.md` §1, §5 | "Competing … deadline Fri Sep 4"; "direct Anthropic SDK", "no provider abstraction" | Hackathon over; `LLM_PROVIDER` = anthropic / glm / jev |
| `fable5findings.md` | Newest entries directly under `# Build log` (CLAUDE.md §6) | The Sep 21–23 entries had been inserted inside the "READ THIS FIRST" callout, so the session-start `sed` returned the Sep 3 entry. **Moved back in c6d54781.** |
| `PLAN_PLATFORM.md` §C | Close retry reuses its id; close ladder runs on weekends | Both fixed (ce8b722e, d9902ed9) |
| `features/corporate_actions.py:13` | "The bars provider back-adjusts history" | It does not (§1.3) |
| `options/selection.py:300` | "Production passes `caps.options_max_quote_age_seconds`" | It does not (§3.4) |
| `options/tools/guard.py:285-288` | `days_to_earnings` "Still None in production" | Wired since 0a9fe57e |
| `options/tools/readonly.py:347` | No IV history exists | `iv_history` table since 069492d0 |

CLAUDE.md is the operator's file and was deliberately **not** edited in
this audit.

---

## 8. What this audit did not verify

- **Anything live.** No Railway access was used. Production env vars
  (`AGENTS_REQUIRE_REAL_LLM`, `AUTO_TRADE_ENABLED`, `ALPACA_BASE_URL`,
  `UVICORN_WORKERS`), the breaker state and the DB contents were not
  inspected.
- **The safety findings in §2 were confirmed by reading, not by
  executing.** None has a failing test yet. Each should get one, and
  per CLAUDE.md §4.1 it must be shown to fail before the fix.
- **The mobile app** (`tsc`, Jest) was not run in this audit.
- **The XLE / XLK 2025-12-05 discontinuities** are listed as splits
  because they are clean ~2:1 ratios on the same day. No corporate-actions
  feed was consulted to confirm them.
