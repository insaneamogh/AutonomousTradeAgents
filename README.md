# The Refusal Ledger

**An autonomous options-trading agent that measures, in dollars, what its own refusals
were worth.**

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).
Runs on Alpaca paper trading. No real capital.

---

## The claim

Every trading agent can show you what it bought.

This one shows you **what it refused** — and then marks every refused trade against
real forward prices, so the risk engine's value is a number on a screen instead of an
assertion in a README.

> `max_total_premium_pct` fired 4 times this week.
> Click one: here is the exact NVDA call it refused, the thesis behind it,
> the named rule that stopped it, and the **$340 it would have lost.**

That is the product. The architecture below exists to make that number trustworthy.

---

## How it works

```
                        ┌─ deterministic, zero LLM ─┐
watchlist → strategy_fit ─→ router → 3 analysts → drafter → contract selection → risk engine → broker
              │                (Haiku)   (Haiku/Sonnet)  (Sonnet)   (6 named stages)   (16 named rules)
              │
              └─ no setup? HOLD here, for zero LLM calls
```

**Both gates that can say no are LLM-free.** The model proposes; named Python rules
dispose. It cannot be argued out of a limit, because the limit is not in a prompt.

- **`strategy_fit`** scores five strategies' preconditions and holds before spending a
  single token when nothing fits.
- **Contract selection** filters the option chain through six stages, each of which
  records why it rejected what it rejected — *4,128 contracts → 1*.
- **The risk engine** runs 21 equity + 13 options named rules, first-veto-wins, with
  every rule that *passed* recorded too. Long options only: loss is bounded by the
  premium, capped at 1% of equity per position and 5% across the book.
- **Ghost P&L** then marks everything that got refused against real forward prices.
  That is the Refusal Ledger.

The exact rules the options side plays by — every threshold, every veto, every
exit, with the reasoning: **[`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md)**

Full architecture, module map, and setup: **[`docs/README.md`](docs/README.md)**

---

## Alpaca integration

| Surface | Use |
|---|---|
| **Trading API** | Orders, positions, account, options trading level |
| **Market Data API** | Daily/intraday bars, option chain snapshots with Greeks + IV, `/v2/options/contracts` for open interest |
| **Alpaca MCP server** | *Planned, not shipped* — see [`docs/PLAN_ALPACA_MCP.md`](docs/PLAN_ALPACA_MCP.md). Intended as a **read-only** session only: there is to be exactly one Alpaca MCP session in this system and no order-placing tool in it. |
| **Alpaca CLI** | *Planned, not shipped* — the market-hours gate. Today that gate is `engine.features.market_calendar` (a holiday table) with an unwired `/v2/clock` client beside it. |

We also ship **our own** MCP server (`apps/mcp_server/`) exposing the council's
read/propose-only surface, so the agent is itself addressable from Claude, Cursor or
VS Code.

---

## Honest limitations

Stated plainly, because a risk system that oversells itself is not a risk system.

- **Paper trading only.** Hypothetical results; no real capital, no real fills.
- **Market data is a 15-minute-delayed indicative feed** (Alpaca free tier), not
  consolidated OPRA. Adequate for daily-bar decisions; not a basis for any claim about
  execution quality.
- **`earnings_blackout` is wired and permanently inert.** Alpaca publishes no earnings
  calendar, so the rule has no data source. It is named and disclosed rather than
  quietly dropped — and rather than filled with a fabricated date.
- **No auto-exercise or assignment handling.** Phase A is long calls/puts only; a
  sweep force-closes any position within 2 days of expiry.
- **P&L over a 4-session contest window is dominated by variance, not skill.** The
  ledger is the contribution; the return is a sample size of one.

---

## Quick start

```bash
make install
make dev-api          # FastAPI on :8000
make dev-mobile       # Expo on :8081
.venv/bin/python -m pytest apps/agents apps/api packages/ -q   # 757 passing
```

Run the council headlessly against the live chain:

```bash
ALLOW_OPTIONS=1 uv run --package agents python -m trading_agents.jobs.daily_cron --force
```

Environment variables, deployment, and the full module map:
**[`docs/README.md`](docs/README.md)**

---

## For AI models working on this repo

Read, in order:

1. **[`CLAUDE.md`](CLAUDE.md)** — §0 assigns your commit identity trailer; §4 is the
   engineering standard (every rule there exists because a real bug shipped past a
   green test suite).
2. **[`docs/HACKATHON.md`](docs/HACKATHON.md)** — the current mission, hard
   requirements, and an explicit "do not do these" list.
3. **[`fable5findings.md`](fable5findings.md)** — the running build log. Newest first.

Two models alternate on this repo across session limits and cannot see each other's
conversations. The git log and the build log are the handover channel — write them for
the model that comes after you.

---

## Disclosure

Paper-trading results are hypothetical and do not represent actual trading. Options
trading carries substantial risk. Nothing in this repository is investment advice.
