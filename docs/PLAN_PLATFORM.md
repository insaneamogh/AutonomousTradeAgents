# Plan: from propose-and-audit demo to autonomous trading platform

> **Status: APPROVED by the operator 2026-09-23. This is the work queue.**
> It supersedes the hackathon-era queue in `CLAUDE.md` and every older
> `PLAN_*.md` where they disagree. Phases run in order; tick them off in
> the build log (`fable5findings.md`), not here.
>
> **Progress (2026-09-23):**
> - Phase 0 is DONE: `posthackathon` merged and pushed, fixtures already
>   fixed by 9120fc854, cap reverted (0281274d4).
> - Phase 1 code is DONE (2de24fd89, f70e80090, 0784e434f). Its live half
>   is the operator's: a pay-as-you-go GLM key, then
>   `provider_check --live --provider glm` must pass BOTH probes before
>   `LLM_PROVIDER=glm`.
> - Phase 2 is DONE (5f1b81342, dfa802d60, 0b485faf7, f35909465). The
>   per-position TP was deliberately left unhonoured (see the build log).
> - Phase 3 is BUILT (9b1893398, 67ab2110d, a99b4704f, and the candidates
>   commit). Its findings: the shipped signal loses 5-8% of premium per
>   trade, while a perfect-direction oracle keeps +37-43%, so the vehicle
>   is fine and the signal is the loss. Two pre-registered price-only
>   candidates also fail. §D P0 data and Phase 5b are now the critical
>   path. **Shadow mode until a candidate clears the bar.**
> - The forecast scorecard is written but has NOT yet been run on
>   production. That needs the operator (`railway run`).
> - Next is Phase 4.


## Context

The hackathon ended Sep 4. Paper P&L history:
- At submission: -$811. Around Sep 6: about -$2.3k.
- Sep 8: the drawdown breaker tripped at -3.01% and latched.
- Sep 11: the Anthropic key was removed. The desk has been 100% HOLD since.
- Lifetime LLM spend is about $10.74. 88% of it went to Sonnet council calls, and $2.89 was spent after the halt, when no trade could open.

The user asked for four things:
- Honest drawbacks, and the gap to a real autonomous platform.
- Why trades lose.
- Which data sources to add.
- A less defensive LLM pass, and a move to z.ai (GLM) for cost.

**Repo fact at planning time.** Branch `posthackathon` held 18 commits (Sep 7-21) that existed only locally. They are now merged into `main` and pushed. The branch already contains:
- Analysis tools: `exit_replay`, `entry_quality`, a 6-year `signal_backtest`, and `cache_opportunity`.
- The `horizon_exceeds_contract` rule, the GLM/z.ai provider, the Jev provider, and the `AlphaModel` seam.
- Deterministic win/loss counts in reflection, and a breaker check before the paid council runs.
- `docs/PLAN_ENTRY_EDGE.md` and `docs/PROVIDERS.md`.

This plan builds on that branch. It does not redo any of it.

**User decisions (2026-09-23):**
1. **Strategy:** trade each thesis in the instrument that matches its horizon. Trend and momentum ideas trade as equity held for weeks. Long options only when the expected-move gate passes, with debit spreads preferred.
2. **Data budget:** about $100/mo, for Alpaca Algo Trader Plus (real-time full-market stock quotes and OPRA options quotes). Everything else from free sources.
3. **LLM role:** event interpreter plus veto. Deterministic, backtested models choose direction.
4. **Branch:** fast-forward `main` to `posthackathon` and push both.

---

## A. Why the trades lose (measured, ranked by cause)

| # | Cause | Evidence |
|---|---|---|
| 1 | **The signal has no edge.** | Production `best_strategy` over 13,403 signals, 58 symbols, 2020-26: hit rate 49.3-50.9% at every horizon from 2 to 60 days, every \|z\|<1. At 20 days the top score quintile does *worse* than the bottom. (commit 17e9296f4) |
| 2 | **A direction signal is traded through an instrument that needs size and speed of move.** | Every trade was long premium. Median best gain while held was +2.9%; median worst was -20.6%. 8 of 21 positions never traded above entry. Correct calls with an underlying move under 2% still lost 10-24% (GILD155 -23.5%, GILD150 -13.8%, AMD -10.1%) to theta and spread. Nothing compares expected move against the move needed to break even. (acb009534) |
| 3 | **Holding horizon doesn't match signal horizon.** | 85% of picks were trend strategies built on 20-252 day windows, held an average of 2.1 days. Time stop is 5 calendar days. A momentum thesis needs about 84 calendar days, but `options_max_dte` is 60. (484a07feb) |
| 4 | **Payoff geometry needs a 62% win rate.** | A -40% stop against a +24.5% minimum trailed win. Replay shows every one of 16 exit ladders losing 12.7-36% per trade, so exits are not the fix. (782dbccd3) |
| 5 | **Data is degraded.** | Options run on the 15-minute-delayed INDICATIVE feed, so stops and trails fire on stale marks (CME froze for 2h16m, then gapped 26 points). Stock data is IEX-only. `iv_rank`, `atm_iv`, `term_structure_slope` and `days_to_earnings` are hardcoded None (`features/provider.py:158-182`), so the earnings blackout never fires. `ret_252d_pct` needs 253 bars but only about 220 are fetched (`features/bars.py:64`, `quant.py:520`), so momentum's 12-month leg always scores neutral. |
| 6 | **The council adds no information.** | Bull and Bear read the same daily-bar features the market has already priced. Final conviction is `min(bull, bear)`, then `min` again with the tool value. Observed range is 0.28-0.62, so the 0.7 delta band is unreachable. Nothing an agent sees is information the tape doesn't already have. |
| 7 | **Book construction.** | 100% long premium and 80% calls (long beta). The 11% premium cap was never reverted. The premium cap is outcome-blind: it cost +$9,581 in blocked winners. Per-position stop and take-profit are chosen but ignored. |
| 8 | **Sample size.** | Only n=21 live positions and 3 closes (-536, -610, +590). Live P&L alone proves nothing either way. The 6-year backtest is the conclusive evidence. |

**Bottom line.** The system loses because there is no edge, and it pays theta and spread for the privilege of having none. Tuning the LLM, the exits or the thresholds cannot fix that. A new signal can, but only one that passes the backtest harness *before* it trades.

---

## B. "The LLM pass is too defensive": what is a bug, and what is protecting you

**Real bugs that cause false HOLDs. Fix these.**
1. **Analysts never learn which direction is being scored** (`technical_analyst.py:108-112`, and the same for macro). A correct bearish read lowers the average score and trips `min_specialist_avg_score` on put and short setups.
2. **The macro prompt is framed only around longs** ("SUPPORTS or HINDERS a long position"). Its "DXY > 105" rule is applied to FRED `DTWEXBGS`, which uses a different scale and sits around 120. That makes "strong dollar headwind" permanently true.
3. **Cap Bug A.** Day and hour slots are counted *before* the duplicate skip (`daily_cron.py:927-933` vs `:597`). Already-decided names use up the 4-per-hour budget every sweep.
4. **Cap Bug B.** Escalation calls use `council_run_id=decision_id` (`escalation.py:720`), so each escalation consumes a symbol slot and spend.
5. **The guard's `select_contract` call omits `realized_vol_pct` and `days_to_earnings`** (`guard.py:788-796`). The IV-vs-realized-vol band and the earnings blackout are dead on the live agent path.
6. **The 0.7 high-conviction delta band is unreachable** under a `min()`-of-two conviction that has never passed 0.62.
7. **The Bull/Bear 500-token cap** is the same budget that truncated 70% of technical replies before it was raised.
8. **`parse_json` only strips fences.** GLM tends to wrap JSON in prose, so a switch to z.ai will *raise* the abstain rate unless extraction is hardened.

**What is protecting you. Keep it.** The veto ledger shows `min_council_confidence` blocked trades that would have lost $7,402 (59 refusals). In a no-edge system, "less defensive" means more coin flips paying theta. Fix the bugs above. Do not lower the floor until the forecast ledger (§E Phase 3) shows the council's confidence is calibrated.

---

## C. How far from a real autonomous trading platform

| Layer | State | Gap |
|---|---|---|
| Risk engine, audit trail, veto ledger | **~80%.** Deterministic named rules, revert-checked tests, Refusal Ledger. | Correlation, sector and single-name rules don't apply to options. There is no portfolio Greeks limit (delta, vega, theta). |
| Execution and reconciliation | **~65%.** Real fills, 30s reconciler, auto-approve, resting broker stop, ratchet. | No assignment, exercise or expiry handling (marked `external_broker`). Partial fills are ignored. A failed DAY close retry reuses its client_order_id and may be rejected. The close ladder runs on weekends. Protective-stop fills are labelled `user_manual`. |
| Orchestration and ops | **~35%.** Single in-process asyncio scheduler. | No alerting: the breaker, a failed stop, a 401 key and mock mode each produce only a log line. No flatten-all kill switch. A restart that spans 14:00 UTC skips the day's sweep. Single-instance only. No end-of-day job. Reflection never effectively ran. |
| Data | **~30%.** Free IEX bars, 15-minute indicative options, FRED x3, Alpaca news. | No real-time feed, IV history, earnings or macro calendar, fundamentals, or breadth/regime data. Feature input snapshots are never persisted. |
| Alpha and research | **~5%.** Backtest harness and `AlphaModel` seam now exist. | **No validated edge.** No options-aware backtest. No forward forecast ledger. No go-live criteria. |
| Learning loop | **~10%.** | Priors stuck at 0.5. Wins and losses were LLM-derived until 546accc6c. No outcome feedback reaches the prompts. |

**Honest estimate.** The plumbing is roughly 2/3 of a real platform. The part that makes money is close to 0. Realistically that means 8-12 weeks of research and forward paper time before real money is even a question, gated by the criteria in Phase 7.

---

## D. Data sources to add (each must feed a gate or a backtested signal, never just the prompt)

| Priority | Source | Cost | Enables |
|---|---|---|---|
| P0 | **Alpaca Algo Trader Plus**: real-time SIP stocks plus OPRA options. Set `ALPACA_OPTIONS_FEED=opra`; the code path already exists. | ~$99/mo | Stops and trails on live marks, fresh entry quotes, and the freshness gate (it ships disabled because the feed is delayed). |
| P0 | **Earnings calendar**: Finnhub or FMP (free tiers exist), cross-checked against SEC EDGAR 8-K. | free-$ | Makes `options_earnings_blackout_days` real, plus an IV-crush guard and event strategies (post-earnings drift). |
| P0 | **Macro event calendar**: FRED `releases/dates` for CPI, NFP and FOMC. The FRED key already exists. | free | Event-day gating and vol regime. |
| P1 | **IV history**: snapshot the ATM IV and 30-day term structure of the whole options watchlist daily into Postgres, starting now, using the existing chain fetch. Buy history (ORATS, or Polygon/Massive options) if it's needed sooner. Alpaca historical option bars go back to about Feb 2024. | free, or paid | IV rank and percentile, term slope, skew, and whether to buy or sell premium. Fills in the `options_context` None fields. |
| P1 | **Regime data**: SPY/QQQ 200-day trend and breadth (% of universe above 50-day, computed from the batched bars already fetched), CBOE VIX, VIX3M and VIX9D term structure, and HY credit spread (FRED `BAMLH0A0HYM2`). | free | A deterministic regime filter. Today VIX only reaches the prompt. |
| P1 | **Fix the bar lookback** to at least 380 calendar days. Evaluate SIP bars for accuracy. | free | Momentum's 12-month leg works. |
| P2 | **Fundamentals and estimates**: SEC EDGAR XBRL companyfacts (free); estimate revisions (FMP or Finnhub). | free-$ | A fundamental analyst that isn't empty (it has run on 0 of 1,903 rows). Revision momentum is a documented factor. |
| P2 | **Positioning**: FINRA short interest (free, twice monthly), SEC Form 4 insider buys (free), CBOE put/call ratio (free daily). | free | Candidate signals for the backtest harness. |
| P3 | **Options flow / unusual activity**: Unusual Whales or Polygon trades. | $$ | Only if the P0-P2 signals show an edge worth amplifying. |

---

## E. Roadmap

**Order of work:** 0 → 1 → 2 → 3 → 4 → 5 → 5b, with 6 running alongside from Phase 2 on, and 7 as the gate before real money.
- Phase 3 must report before anything in Phase 5 turns on live entries. Until then the desk runs in shadow mode.
- One logical commit per fix. Each gets a revert-check (CLAUDE.md §4.1) and a `fable5findings.md` build-log entry.

### Phase 0: Protect the work and clean up (about 1 day)
- Push `posthackathon` to origin. Fast-forward `main` to it (the CLAUDE.md rule is to land on main).
- Fix the 14 fixtures that hard-code `date(2026,9,18)`: they now trip `expiry_day_entry`. Make them clock-relative using the RiskContext clock injection from 9120fc854.
- Revert `options_max_total_premium_pct` 11.0 → 7.5, and drop `max_tolerated_book_drawdown_pct` from the aggressive profile (open since Sep 4). File: `packages/engine/engine/risk/types.py:625-633`.
- Mark CLAUDE.md's queue as superseded by this plan. It still points at hackathon eligibility.
- Verify: `.venv/bin/python -m pytest apps/agents apps/api packages/ -q` is fully green; ruff at baseline.

### Phase 1: z.ai cutover (config plus small code changes)
The existing seam is `posthackathon:apps/agents/trading_agents/llm.py` (`LLM_PROVIDER=glm`, `_GLM_MODEL_MAP`, `GLM_MODEL_*` overrides, `provider_check`).
- **Model map.** Reasoning tier → `glm-5.3` ($1.40 in / $4.40 out per M). Fast tier → `glm-5.3-flash` ($0.15 / $0.50). Add **price rows** for both in `cost_ledger.py`, otherwise they bill at Sonnet rates and trip the $3 cap early. Change the default map away from glm-4.6 / 4.5-air, or keep those and set the env overrides.
- **Remove the hardcoded ids.** The literal `"claude-opus-4-7"` in `options/agents.py:88` becomes `Model.OPUS`. Route the `OPTIONS_AGENT_MODEL` mapping through the tier map.
- **Auth header.** The SDK sends `x-api-key`. z.ai's documented Claude-compatible setup uses `ANTHROPIC_AUTH_TOKEN` (Bearer). If `provider_check --live` returns 401, pass `auth_token=` instead of `api_key=` for GLM.
- **JSON robustness.** Make `parse_json` extract the first balanced `{...}` from prose. Keep the one re-ask. Log a `degraded` flag.
- **Caching.** Z.ai publishes no Anthropic-style `cache_control` pricing. Keep sending it, since it's harmless. Treat cache savings as zero in the ledger.
- **Persist the input feature snapshot hash** on `agent_decisions`. `cache_opportunity` found inputs are stored nowhere, and without them no A/B or cache work is measurable.
- **Plan terms.** Use a pay-as-you-go API key, not the GLM Coding Plan. Check z.ai's terms: the Coding Plan is meant for coding tools.
- **Verify:** `provider_check --live` on Railway returns `mode REAL, abstained False`. After one sweep, ledger rows show GLM model ids with GLM prices. Run the 100-case funnel eval with the GLM mock recording.
- **Cost expectation.** About 2.6x cheaper than Sonnet on glm-5.3, about 24x on flash. That makes experiments cheap. It does not make trading profitable.

### Phase 2: Fix the defensiveness bugs from §B, without lowering the floor
- Pass the resolved `direction` into the technical and macro analyst prompts (`nodes/technical_analyst.py`, `nodes/macro_analyst.py`, `prompts/*`). Make the macro prompt direction-neutral. Replace the "DXY > 105" rule with a z-score of DTWEXBGS against its own 1-year history.
- Cap bugs: move the day and hour increments after the duplicate check (`jobs/daily_cron.py:927-933`). Give escalation its own budget key (`options/escalation.py:720`, `memory/cost_ledger_postgres.py:94-99`).
- Guard: pass `realized_vol_pct` and `days_to_earnings` into `select_contract` (`options/tools/guard.py:788-796`), mirroring `nodes/drafter.py:404-414`.
- Make the delta-band threshold relative to the observed conviction distribution, not a fixed 0.7 (`engine/options/selection.py:150-152`).
- Raise the Bull/Bear `max_tokens` from 500 to 900 (`options/agents.py:110`).
- Make exits read the per-position stop and take-profit, clamped to the caps. Branch commit b28443384 partly does this; confirm, finish, and replay it.
- Verify each fix with a test that is revert-checked per CLAUDE.md §4.1. The 100-case eval's abstain rate and HOLD reasons should shift, and the diff in funnel counts gets recorded.

### Phase 3: Research harness first. Nothing new trades until it passes.
- **Forecast ledger (shadow mode).** Every council or `AlphaModel` output is recorded as a prediction: direction, expected move, horizon. It is scored deterministically against realized returns, whether or not a trade happened. Reuse the ghost_outcomes and `ghost_eval` machinery (`jobs/ghost_eval.py`) and `Signal` (`engine/alpha/base.py`). This finally measures LLM calibration and whether the LLM adds anything over `strategy_fit`.
- **Options-aware backtest.** Extend `signal_backtest.py` with a premium P&L model: Black-Scholes repricing using realized-vol-proxied IV now, and real IV history once §D P1 accumulates. Charge spread and theta. Validate it against the 21 recorded paths in `fixtures/option_paths.json` (it must reproduce the 3 real closes, as `exit_replay` does).
- **Acceptance bar for any signal.** Non-overlapping windows, at least 6 years, hit rate or mean-return z ≥ 2 **after** costs, stable across at least 2 sub-periods, and a holding horizon equal to the signal's own horizon (`strategies/horizon.py`).
- **Candidate signals to test**, cheapest first: post-earnings drift (needs the earnings calendar), short-horizon reversal (1-5 day, matching the hold), 12-1 momentum held at its natural 1-3 month horizon *in equity*, IV-rank mean reversion for premium selling, and regime-conditioned variants.

### Phase 4: Missing entry gates (deterministic named rules)
- `expected_move_below_breakeven`, exactly as specified in `docs/PLAN_ENTRY_EDGE.md`. Required move = (premium + round-trip spread + theta × horizon) / delta. Expected move = the max of the ATR-scaled and IV-implied moves over the horizon. Replay it on the 21 paths: it must refuse GILD155, GILD150 and AMD and keep NVDA and CDNS.
- `iv_rank_regime`: no long premium when IV rank is above X. Needs the IV history from §D.
- `market_regime`: a SPY 200-day trend plus VIX term structure gate on directional long-beta entries.
- `event_blackout`: made real by the earnings and FOMC/CPI calendars.
- Replace the outcome-blind premium cap as the *selection* mechanism with ranking: when the book is full, a new idea must beat the weakest open position's expected value to rotate.

### Phase 5: Match instrument to horizon (DECIDED)
The strategy itself still has to come out of Phase 3. This phase changes where each thesis gets traded.
- **Route by horizon.** Add a deterministic `instrument_router` step after `strategy_fit` that reads `strategies/horizon.py`:
  - Horizon ≥ 20 trading days → equity. This uses the existing ATR bracket path (`nodes/drafter.py:250-312`, `engine/sizing/atr.py`). Set the time stop from the horizon, not the fixed 5 calendar days (`position_manager.py:102,1064`).
  - Short horizon with the expected-move gate passing → options.
- **Debit spreads.** Add a vertical debit spread using Alpaca `mleg` orders in `packages/broker/broker/alpaca.py`. Risk math: max loss is the net debit, and the per-underlying and direction caps use net debit.
  - Its exit logic runs on spread value in `engine/options/exits.py`.
  - Assignment of the short leg is handled in Phase 6.
- **Equity enablement.** Equities barely trade today (6 fills, 0 closes). Confirm that auto-approve and the equity close path work end to end with the existing `scripts/smoke_paper_trade.py` pattern, then turn on the equity leg of the sweep with its own per-day cap.
- Shorts stay off. Bearish theses use puts or put debit spreads.

### Phase 5b: Re-role the LLM (DECIDED: event interpreter + veto)
Move the LLM from *generating direction* to *interpreting events*. It reads news, filings and earnings releases, and emits structured features: surprise sign and size, guidance change, catalyst date, risk flags. Deterministic models consume those, and they get backtested like any other signal.
- Collapse Bull, Bear and the trade hop into one structured `decide()` call per symbol-day on the flash tier. The `LLM.decide()` seam already exists.
- Keep the LLM veto on unmodeled risk.
- **New node, `nodes/event_interpreter.py`.** Input: fenced Alpaca news (`engine/features/news.py`), the earnings or filing text summary, and the corporate-actions block. Output: a strict schema of `catalyst_type`, `surprise_sign` (-1/0/+1), `surprise_magnitude` (0-1), `catalyst_date`, and `risk_flags[]`.
  - Output is validated against an allow-list and cached by input hash (the Phase 1 snapshot hash).
  - It runs on `glm-5.3-flash`.
- **The features become an `AlphaModel` input.** Its `Signal` is scored in the forecast ledger and backtested before any rule consumes it.
- **Veto.** `risk_flags` (for example going-concern, halt, pending M&A or guidance withdrawal) map to a named deterministic rule, `llm_event_veto`. The LLM never originates a trade.
- **Retirement.** Bull/Bear/trade-hop goes behind `USE_OPTIONS_AGENT=0` once the interpreter path is live. The code stays for comparison in the forecast ledger.
- **Cost target.** At most 1 flash call per symbol-day plus 1 reasoning call per admitted trade. Expect under $0.20/day at 20 symbols.

### Phase 6: Autonomy operations
- **Alerting**, reusing the push and notification services: breaker trip, a failed resting stop, an LLM 401 or mock mode in production, a sweep that didn't run, and a daily P&L report. Set `SENTRY_DSN`.
- **Breaker policy.** Keep the latch, but notify on trip and add an explicit acknowledge or auto-reset-next-session option, per the user's choice. Add a **flatten-all kill switch** endpoint.
- **Scheduler.** Catch up a missed baseline sweep on restart (`services/council/scheduler.py:208-215`). Add a Postgres advisory-lock leader election before running more than one instance. Add an end-of-day job: ghost eval, then reflection with deterministic counts, then the ops report.
- **Positions.**
  - Handle assignment, exercise and expiry explicitly (`services/orders/order_sync.py:373-458`).
  - Label protective-stop fills correctly.
  - Give each close retry a unique client_order_id (`position_manager.py:1140`).
  - Gate the close ladder to market hours, keeping the resting broker stop for off-hours.
- **Portfolio Greeks limits**: net delta, vega and theta as a % of equity, as named rules.

### Phase 7: Go-live criteria (paper to real money)
All of the following must hold:
- At least 60 trading days of forward paper time on the final configuration.
- At least 100 closed trades.
- Expectancy after costs above 0, with the 95% CI excluding 0.
- Max drawdown within the declared tolerance.
- Forecast-ledger calibration with Brier score better than the base rate.
- Zero unhandled position states in that window.
- Every alert path fired at least once in a drill.

---

## Verification (end to end)
- Offline: the full pytest suite; `python -m tests.eval.run_eval`; `python -m tests.eval.signal_backtest`, `exit_replay` and `entry_quality` (all on the branch; they run with no keys).
- Provider: `railway run -s AutonomousTradeAgents python -m trading_agents.provider_check --live`.
- Live paper: after acknowledging the breaker and restoring the key, one baseline sweep. Check the ledger model ids and prices, the abstain rate, the funnel counts, and forecast-ledger rows written.
- No trades are placed by the agent in this session. Order approval stays with the user (CLAUDE.md §8).
