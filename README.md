# The Refusal Ledger

**An autonomous paper-trading agent — US equities and options — that can now place
a trade with no one watching, and that measures, in dollars, every trade it refused
to make.**

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).
Runs on Alpaca **paper trading only**. No real capital, anywhere in this system, ever.

---

## 📋 Judges start here

**→ [`docs/ONE_PAGER.md`](docs/ONE_PAGER.md) — one page: AI logic, risk gates, Alpaca infrastructure.**

Two more, if you want the evidence behind it:

| | |
|---|---|
| [`docs/SUBMISSION_FINDINGS.md`](docs/SUBMISSION_FINDINGS.md) | Every figure measured live from production, with the caveats stated plainly |
| [`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md) | The authoritative options rule set, derived from the code |

**The 20-second version.** An LLM is never in an execution or risk path. Two agents
(Bull and Bear) argue a thesis and call a guarded tool; the guard re-runs every
named deterministic risk rule on the path (18 equity, 16 options) and first veto
wins. Every trade the engine *refuses* is then marked to market against real Alpaca
option quotes — which is how we caught one of our own risk rules costing $6,030 while
another saved $9,956.

---

## What it does

An autonomous agent that trades **US equity and options** on Alpaca paper, and
measures — in dollars — every trade it **refused** to make.

Every trading agent can show you what it bought. This one marks each refusal to
market against real Alpaca option quotes, so the risk engine's value is a number
you can audit instead of an assertion in a README.

> `max_total_premium_pct` fired 51 times. Click one: the exact contract it refused,
> the thesis behind it, the named rule that stopped it, and what it would have made
> or lost.

**That measurement caught one of our own risk rules costing $6,030 while another
saved $9,956.** We could not have learned that from P&L.

---

## The strategy — testable, and tested

Five strategies score every symbol in **both directions** (long and short) as pure
Python, with no model involved. Each returns named components with weights, so a score
is always decomposable into *why*.

| Strategy | Thesis | Primary signals |
|---|---|---|
| `sma_crossover` | Trend continuation | price vs 20/50-DMA, trend regime, not-overextended |
| `rsi_mean_reversion` | Counter-trend snapback | RSI-14 extremes, price z-score, reversal candle |
| `momentum` | 12-1 relative strength | trailing return, Sharpe, trend alignment |
| `breakout` | Donchian channel break | channel position, ATR expansion, volume confirmation |
| `vol_regime_switch` | Compression → expansion | ATR z-score, realized vol, NR7 / inside-bar patterns |

A strategy must clear `MIN_FIT_TO_TRADE` to trade at all. The floor has a **hard
lower bound of 0.41**, enforced by test: below that, `vol_regime_switch` would clear on
direction-blind checks alone, meaning a "long" and a "short" setup would score
identically. That invariant is the difference between a strategy and a coin flip.

**Testable offline** — no keys, no network, under a second:

```bash
.venv/bin/python -m pytest apps/agents/tests/eval -q               # 13 assertions
cd apps/agents && ../../.venv/bin/python -m tests.eval.run_eval    # 100-case scorecard
```

The scorecard runs a **100-case golden dataset of labelled archetypes** through the
real funnel and prints where each one died:

```
scanned          100
refused free      40   ← 40% rejected for ZERO LLM calls
reach an LLM      60

REFUSALS BY NAMED REASON (zero LLM calls spent)
  below_fit_floor_or_thin_evidence   30
  illiquid_chain                      8
  no_liquid_contract                  2

BY ARCHETYPE                    ->LLM  refused
  clean_uptrend                    10        0
  clean_downtrend                   0       10
  choppy_nothing                    0       10
```

It admits clean trends, refuses chop, names every refusal, and rejects 40% of the
universe for zero cost. **To be clear about what this is not:** these are labelled
archetypes, not historical bars. It proves the funnel's *logic* narrows correctly —
it says nothing about whether the strategy makes money. The scorecard prints that
same disclaimer itself.

---

## How the agent runs, end to end

**1. Identifies opportunities.** A deterministic scanner sweeps the watchlist every
2 minutes against **10 named triggers** — `donchian_20_breakout_up`,
`atr_expansion_1_5x`, `zscore_stretch_up`, `gap_up_2pct`, `dma50_cross_down`,
`rsi_enter_overbought` and others. No LLM, no cost. A symbol that fires nothing is
never looked at again that cycle. Alpaca's screener API widens the universe beyond the
static watchlist.

**2. Makes decisions.** Triggered symbols go to `strategy_fit` (still free). Only a
real setup reaches the Bull/Bear council, which argues the thesis and calls a guarded
tool. The guard re-derives contract selection, sizing and every risk rule before
anything reaches Alpaca. **Every decision is persisted with its reasoning** — the
strategy that fit, the components that scored, the funnel counts, the rule that
vetoed — so a HOLD is as auditable as a fill.

**3. Manages positions.** Three independent exit layers, all deterministic:
- **Trailing ratchet** — arms at +35%, gives back 30% of peak, hard stop at −40%,
  ceiling at +150%. Proportional, so a bigger winner gets a wider leash.
- **Broker-side resting STOP_LIMIT** — placed at Alpaca after fill, GTC. Survives an
  overnight gap *and* our own downtime. One fired this session, correctly.
- **DTE≤2 expiry sweep** — unconditional, because we do not handle assignment.

Position sizing is ATR-based and vol-targeted; the options book is additionally
bounded by a per-position and a portfolio premium cap.

**4. Performance.** Measured live and reported without varnish in
[`docs/SUBMISSION_FINDINGS.md`](docs/SUBMISSION_FINDINGS.md): P&L, every closed trade
with the rule that closed it, and the refusal ledger. Two sessions of live P&L is a
sample size of one — we say so there rather than dress it up.

---

## Architecture — an LLM is never in an execution or risk path

```
Scanner (10 named triggers, 2-min sweep)          deterministic · free
   ↓  only symbols that fire
strategy_fit (5 strategies × long/short)          deterministic · free · most die here
   ↓  only symbols with a real setup
Bull agent  ⇄  Bear agent                         2 LLM calls · argue independently
   ↓  only on deterministic agreement
Tool call → ToolGuard → risk engine → Alpaca      guard re-runs every rule
   └─────────────────→ Refusal Ledger             whatever it blocked, priced
```

**Screening is unlimited; thinking is rationed.** Every symbol is scored in Python
for free. Only survivors reach a paid model call, hard-capped at **20/day, 4/hour,
$3.00/day**. A full session costs about **$9.42**.

**The agents cannot place an order.** They emit a tool call; `ToolGuard.before()`
intercepts *every* one and re-derives the whole risk decision from scratch. A refusal
returns a **named rule** the model may adjust against once, bounded at 3 rounds. That
is why the agents can run on Haiku: a weaker model degrades *selection*, never *risk
control*.

**Two-agent council.** Bull and Bear read the same feature dict independently. A
deterministic resolver takes the **minimum** conviction, never the mean; disagreement
or abstention ends the pass with no trade.

---

## Risk gates — 18 equity + 16 options named rules, first veto wins

Every veto is a named, testable Python function. Never a model output.

`drawdown_halt` · `pdt_block` · `single_name_concentration` · `correlation_cap` ·
`position_size_cap` · `sector_concentration` · `shortable_check` ·
`short_requires_stop` · `short_unbounded_loss_cap` · `wash_sale` ·
`options_level_insufficient` · `naked_short_forbidden` · `illiquid_contract` ·
`max_premium_pct` · `max_total_premium_pct` · `min_dte` / `max_dte` · `iv_unavailable`

- **Six-stage contract funnel** narrows a full chain to one contract
  (`contract_type → dte_window → delta_band → liquidity → iv_present → iv_realized_band`).
  Every stage count is persisted, so a HOLD explains itself instead of going silent.
- **Chain-depth gate.** A chain yielding <5 liquid contracts is refused outright,
  added after a stop failed on a contract whose mark sat frozen 2h16m then gapped 26
  points in one print: *a price stop cannot work on a mark that does not print.*
- **Halt coupling, enforced by test.** `book size × stop ≤ declared tolerance`. The
  −3% daily halt blocks new entries but closes nothing, so it never bounded the
  book's worst session. A profile taking a wider tail must now **declare** it.
- **Three exit layers:** trailing ratchet · broker-side resting STOP_LIMIT (survives
  an overnight gap even if our server is down) · DTE≤2 expiry sweep.
- **Long-only on the option itself.** A bearish thesis is a **long put** — loss
  bounded by the premium. Writing options is deliberately out of scope: unbounded
  loss with no assignment handling.

---

## Alpaca integration

| Surface | Use |
|---|---|
| **Trading API** | Bracketed equity orders (incl. shorts), options `buy_to_open`, resting `STOP_LIMIT` GTC exits, positions, account, options trading level |
| **`/v2/options/contracts`** | Chain metadata + open interest. **Paginated** — unpaged returns 100 of 4,674 rows, which silently emptied the liquidity gate on every symbol until fixed |
| **`/v1beta1/options/snapshots`** | Live bid/ask/IV/greeks, merged with OI to build candidates |
| **Market Data API** | Daily/intraday bars, `/v1beta1/news`, `/v1beta1/screener` for universe screening |
| **Alpaca CLI** | **Live, not a prop.** `alpaca clock` runs before every sweep to catch early closes and unscheduled halts a hardcoded calendar misses. `create_subprocess_exec` with an argv list (never a shell string), killed on timeout, `None` on any failure. First link in a **CLI → REST → local-calendar** chain, each reporting its own source. |
| **Our own MCP server** | `apps/mcp_server/` exposes this council read-only *to* Claude Desktop / Cursor. A genuine bonus; the CLI above is what satisfies the hackathon requirement. |

A **30-second reconciler fleet** converges broker truth into Postgres: fills,
external closes, the drawdown breaker, and **account-switch detection** — swapping API
keys retires the previous account's state instead of silently inheriting its halt and
open positions.

---

## What measuring actually bought us

| Found by instrumenting | Fix |
|---|---|
| A stop that could not fire — mark frozen 2h16m, then a 26-point gap | Chain-depth gate, refused for 0 model calls |
| Options council was 84% of a $10 credit burn | Deterministic pre-flights moved *before* the paid debate |
| "15 symbols per sweep" was really 15 every 2 minutes | Hard per-day and per-hour caps |
| A long put's real −$195 loss displayed as **+$195** | P&L sign keyed to broker side, not to the thesis |
| The −3% halt blocks entries but closes nothing | Halt-coupling invariant, enforced by test |

Every one was found by measuring the system's own behaviour, not by reading it.

---

## Honest limitations

Stated plainly, because a risk system that oversells itself is not a risk system.

- **P&L is currently slightly negative and the sample is two sessions.** That is
  noise, not a result, and we do not present it as one. The ledger is the
  contribution; the return is a sample size of one.
- **Ghost marks are mid-flight, not settled.** Real Alpaca prices, but a ghost
  finalizes only after 5 trading days — these are labelled "so far" wherever shown.
- **Paper trading only, everywhere, hard-coded.** No path in this repo can reach a
  live account by setting an environment variable.
- **Market data is a 15-minute-delayed indicative feed** (Alpaca free tier), not
  consolidated OPRA. Adequate for daily-bar decisions; not a basis for any claim
  about execution quality.
- **`earnings_blackout` is wired and permanently inert.** Alpaca publishes no
  earnings calendar. Named and disclosed rather than quietly dropped, or filled with
  a fabricated date.
- **No auto-exercise or assignment handling.** A DTE≤2 sweep force-closes any option
  position unconditionally; that sweep, not exercise handling, is what stops an
  in-the-money long option becoming a share position this account cannot carry.
- **Unattended execution depends on the process being alive.** The ratchet and
  sweeper run on a timer inside one API process. The broker-side resting stop is the
  exception — it lives at Alpaca and survives our downtime.
- **One operator Alpaca paper account, not one per user.** Every judge viewing the
  demo sees the same account; per-user broker linking is not built.

---

## Quick start

```bash
make install
make dev-api          # FastAPI on :8000
make dev-mobile       # Expo on :8081
```

```bash
uv sync --all-packages
uv run pytest apps/agents apps/api packages/ -q
```

Verified against this checkout: **1511 passed, 11 skipped**
(`apps/agents apps/api packages/`). `apps/mcp_server` needs the `uv sync
--all-packages` above first, or it fails collection.

Run the council headlessly against the live chain:

```bash
ALLOW_OPTIONS=1 uv run --package agents python -m trading_agents.jobs.daily_cron --force
```

Environment variables, deployment, and the full module map:
**[`docs/README.md`](docs/README.md)**

---

## Build history — context, not required reading

This system started as a self-approval product: the agent proposed, and a human
always tapped the button before anything reached the broker. **That is no longer
the whole story.** Unattended entries are live in production now, through two
separate mechanisms described in full below — both off by default, both hard-coded
to paper trading, and both bounded by a second, independent gate beyond the
operator's own switch (an explicit owner consent toggle for one, two agents having
to independently agree for the other). If you have read an older
version of this file, or anything in this repo that describes every trade as
human-approved with no further qualification, that description is now wrong. This
section, and the one right after it, are the correction.

---

## For AI models working on this repo

Read, in order:

1. **[`CLAUDE.md`](CLAUDE.md)** — §0 assigns your commit identity trailer; §4 is the
   engineering standard (every rule there exists because a real bug shipped past a
   green test suite).
2. **[`docs/HACKATHON.md`](docs/HACKATHON.md)** — the current mission, hard
   requirements, and an explicit "do not do these" list.
3. **[`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md)** — the authoritative,
   code-derived rule set for the options side, if you're touching any of it.
4. **[`fable5findings.md`](fable5findings.md)** — the running build log. Newest
   first.

Two models alternate on this repo across session limits and cannot see each other's
conversations. The git log and the build log are the handover channel — write them
for the model that comes after you. And per this repo's own repeated experience
(CLAUDE.md §4.2): read the code before trusting a doc, including this one — verify
a path or a flag name before citing it, the same discipline this rewrite itself was
held to.

---

## Disclosure

Paper-trading results are hypothetical and do not represent actual trading. Options
trading carries substantial risk. Nothing in this repository is investment advice.
