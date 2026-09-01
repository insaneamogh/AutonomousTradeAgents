# The Refusal Ledger

**An autonomous paper-trading agent — US equities and options — that can now place
a trade with no one watching, and that measures, in dollars, every trade it refused
to make.**

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).
Runs on Alpaca **paper trading only**. No real capital, anywhere in this system, ever.

---

## This changed in the last 24 hours — read this before anything else

This system started as a self-approval product: the agent proposed, and a human
always tapped the button before anything reached the broker. **That is no longer
the whole story.** Unattended entries are live in production now, through two
separate mechanisms described in full below — both off by default, both hard-coded
to paper trading, both gated behind explicit consent. If you have read an older
version of this file, or anything in this repo that describes every trade as
human-approved with no further qualification, that description is now wrong. This
section, and the one right after it, are the correction.

---

## The claim

Every trading agent can show you what it bought. This one also shows you **what it
refused** — equity and options alike — and marks every refusal against real forward
prices, so the risk engine's value is a number on a screen instead of an assertion
in a README.

> `max_total_premium_pct` fired 4 times this week. Click one: here is the exact NVDA
> call it refused, the thesis behind it, the named rule that stopped it, and the
> **$340 it would have lost.**

That is the product. Everything below exists to make that number trustworthy — and
now to make sure it catches a refusal whether it came from a human who was asked, or
an agent that never had to ask.

---

## How autonomous is "autonomous," right now

There are **two separate unattended-execution paths**, each with its own master
switch, neither on by default, and neither a variant of the other. Read both — they
do not work the same way.

### 1. Equity entries — the auto-approve sweeper

The agent has always been able to *draft* a trade. Until this changed, a human tap
was the only way one ever reached the broker. Now
[`apps/api/app/services/orders/auto_approver.py`](apps/api/app/services/orders/auto_approver.py)
can do it instead, subject to **eight gates, every one of them, or nothing
executes**:

1. `AUTO_APPROVE_ENABLED` — the operator's env-level master switch. **Off by
   default.**
2. **Hard-coded paper-only.** Written as a literal boolean expression
   (`trading_mode() == "paper" and not LIVE_TRADING_ENABLED`), never a config
   lookup — deliberately, so it can never be "generalised" into something that also
   reaches a live account.
3. This specific Alpaca connection's own `auto_approve_consent` flag — set from
   *inside the app*, by the account owner, not the operator. The env switch alone
   changes nothing; both keys must be on at once.
4. The regular US market session must be open right now.
5. The proposal must be fresh — younger than `AUTO_APPROVE_MAX_AGE_MIN` (default 60
   minutes). A stale thesis does not get executed blind.
6. No more than `AUTO_APPROVE_MAX_PER_DAY` (default 5) auto-approvals for this user,
   this UTC day.
7. No more than **one** per reconciler tick (~30 seconds) — a bug that mis-reads the
   pending queue places one wrong order, not the whole inbox, and there are 30
   seconds to notice.
8. The account's drawdown circuit breaker must not be tripped.

Clearing all eight does not skip risk management — it calls the exact same
`execute_proposal()` a human's tap would call, which re-runs the **full**
deterministic risk gate against live broker state. This module adds zero new risk
rules; it only decides *when* to ask the gate that already exists to try.

### 2. Options entries — the live Bull/Bear council

A different mechanism, not a variant of the first — there is no pending-approval
step here to auto-approve. When the options path is live (`USE_OPTIONS_AGENT=1` in
production), a trade is attempted, or it isn't, inside a single council pass:

- Two independent agents, **Bull** and **Bear**, read the identical evidence and
  each form a view — direction, strategy, conviction, thesis — in parallel, blind to
  each other's answer. A trade is even considered only if they **independently
  agree** on direction, and it is sized on whichever of the two was **less
  confident**.
- Only then does the winning agent get to call `open_option_trade` — and even that
  call never reaches the broker directly.
  [`ToolGuard`](apps/agents/trading_agents/options/tools/guard.py) runs a fixed
  12-step gate first: the same hard-coded paper-only check as above, market hours,
  one attempt per pass, a thesis that names an actual timeframe, a direction that
  matches what the two agents actually resolved to — and, as its last step, the
  **same risk engine** (13 named options-specific rules — see
  [`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md)) that every other order in
  this system runs through.
- Its own master switch, `AUTO_TRADE_ENABLED`, is independent of
  `AUTO_APPROVE_ENABLED` above — flipping one does not flip the other.

Once a position is open, it is managed unattended too: a deterministic trailing
ratchet checks every open option position on every reconciler tick (stop-loss,
take-profit backstop, trailing stop, time stop, expiry sweep) with no LLM in the
loop at all, and — only as a secondary, later check, on top of a ratchet that keeps
running regardless — a single monotone LLM may tighten a stop, raise a take-profit,
close early, or scale in through the same guard, capped at one such consult per
20-second fleet tick, never able to loosen anything. See
[`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md) §3 for the exit order and the
exact trigger conditions.

**What every one of these paths shares:** paper-only is hard-coded, not a flag, in
every one of them; every one re-runs the full, unmodified risk engine before
anything reaches Alpaca; every one is off unless explicitly turned on; none can
place a trade outside regular market hours; and no LLM output ever substitutes for
the risk engine's own judgement — an agent can request a trade or an adjustment,
never approve one against the risk gate.

**What "risky" actually means here:** the risk is that an order reaches Alpaca with
no human confirming *that specific trade* in the moment — that part is real, and is
exactly what "unattended" means. It is not that the loss is unbounded: a long
option's maximum loss is its own premium, capped per-position and across the whole
book (below); an equity order still sizes through the same position/sector/
correlation caps a manual trade would; and every path above only ever reaches an
Alpaca **paper** account, by a check written so an environment variable cannot talk
it into a live one.

---

## How it works

```
                        ┌─ deterministic, zero LLM ─┐
watchlist → strategy_fit ─→ router → 3 analysts → drafter → contract selection → risk engine (17 rules) ─┬─→ pending → human OR auto-approve sweeper → broker
              │                (Haiku)   (Haiku/Sonnet)  (Sonnet)   (6 named stages)                      └─→ Refusal Ledger
              │
              └─ options-eligible → Bull ⇄ Bear (parallel, blind) → resolve() → ToolGuard (12 steps) → risk engine (13 rules) ─┬─→ broker, no human step
                                     agree on direction, size = min conviction                                                 └─→ Refusal Ledger
```

**Every gate that can say no is LLM-free**, on both branches. The model proposes;
named Python rules dispose. It cannot be argued out of a limit, because the limit is
not in a prompt.

- **`strategy_fit`** scores five strategies' preconditions and holds before spending
  a single token when nothing fits.
- **Contract selection** filters the option chain through six fixed stages, each
  recording why it rejected what it rejected — *4,128 contracts → 2,064 calls → 1
  bought* — and that funnel is persisted to the audit row on every options pass, not
  just the ones that reach a human.
- **The risk engine** runs 17 named equity rules or, once a proposal is an option, a
  parallel 13-rule options sequence — first-veto-wins, every rule that *passed*
  recorded too, not just the one that blocked. A bearish options thesis is expressed
  by **buying a put**, never by selling a call, so a losing options position can
  never lose more than the premium paid, on either side of the book.
- **Ghost P&L** then marks everything that got refused — equity or option, human
  path or agent path — against real forward prices. That is the Refusal Ledger.
  Until this week the entire options half of this was structurally invisible to it;
  it is not anymore.

The exact rules the options side plays by — every threshold, every veto, every
exit, with the reasoning: **[`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md)**

Full architecture, module map, environment variables, and setup:
**[`docs/README.md`](docs/README.md)**

---

## What's actually on screen

- **Picks / Review** — proposals waiting on a human, exactly as before.
- **Decisions** — every council decision, approved, held, or vetoed, with an `AUTO`
  pill on any row a sweeper or an agent executed with nobody watching.
- **Positions — Open and Closed.** Open positions show live unrealized P&L. Closed
  positions are a full history — entry, exit, realized P&L, who closed it (agent
  ratchet, agent signal, expiry sweep, the user, or a close made directly at Alpaca)
  — with an honest **"(est.)"** marker on the handful of rows where the exit price
  is back-solved from realized P&L rather than read off a real fill, so an estimate
  is never presented as a broker fact.
- **Insights** — the veto ledger (named rule, times fired, dollars blocked), the
  contract funnel, and ghost P&L, all now covering options passes as well as equity
  ones.

---

## Alpaca integration

| Surface | Use |
|---|---|
| **Trading API** | Orders, positions, account, options trading level |
| **Market Data API** | Daily/intraday bars, option chain snapshots with Greeks + IV, `/v2/options/contracts` for open interest |
| **Alpaca CLI** | **Live**, not a demo prop — `alpaca clock` (`packages/engine/engine/features/alpaca_cli.py`) is wired directly into the scanner's market-open gate (`engine.scanner.engine.Scanner`), and every scan result records which clock source actually answered. What is still missing is a dedicated on-screen indicator that makes this visible to a judge without reading code — that is the open item, not the integration itself. |
| **Alpaca's own MCP server** | Not yet integrated. The eligibility spec has been fetched and quoted (`docs/PLAN_ALPACA_MCP.md` §0), but no code in this repo calls `alpaca-mcp-server` yet. The Alpaca CLI usage above is what currently satisfies this hackathon's "Alpaca's own MCP server **or** CLI" requirement. |
| **Our own MCP server** | Shipped — `apps/mcp_server/` exposes this council's read/propose-only surface to Claude Desktop, Cursor, or any MCP client. A genuine bonus ("our agent is itself MCP-addressable"); it does not, on its own, satisfy the requirement above. See [`apps/mcp_server/README.md`](apps/mcp_server/README.md). |

---

## Honest limitations

Stated plainly, because a risk system — and a README describing one — that oversells
itself is not a risk system.

- **Paper trading only, everywhere, hard-coded.** No path in this repo, including
  either unattended path above, can reach a live account by setting an environment
  variable. Hypothetical results; no real capital, no real fills.
- **Unattended still depends on the process being alive.** The trailing ratchet, the
  escalation agent, and the auto-approve sweeper all run inside one API process on a
  timer. A stop the loop never gets to check is a stop that does not fire — unlike
  an equity bracket order, which sits at the broker and survives this app's own
  downtime.
- **Market data is a 15-minute-delayed indicative feed** (Alpaca's free tier), not
  consolidated OPRA. Adequate for daily-bar decisions; not a basis for any claim
  about execution quality.
- **`earnings_blackout` is wired and permanently inert.** Alpaca publishes no
  earnings calendar, so the rule has no data source. It is named and disclosed
  rather than quietly dropped, and rather than filled with a fabricated date.
- **No auto-exercise or assignment handling.** A sweep force-closes any option
  position within 2 days of expiry, unconditionally — that sweep, not exercise
  handling, is what keeps an in-the-money long option from becoming a share position
  this account cannot carry.
- **One operator Alpaca paper account, not one per user.** A past version of this
  app silently attached every new signup to the operator's own Alpaca connection
  with write access — that has been fixed (an explicit allowlist now gates it, and
  anyone not on it correctly sees an honest empty/disconnected state instead of a
  fake portfolio) but genuine per-user "bring your own Alpaca account" linking is
  still not built. Every judge looking at this demo is looking at the same account.
- **P&L over a several-session contest window is dominated by variance, not skill.**
  The ledger — what the agent refused, and what that refusal was worth — is the
  actual contribution; the return is a sample size of one.

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

Verified live against this checkout, today: **1308 passed, 11 skipped**
(`apps/agents apps/api packages/`); **1319 passed, 11 skipped** including
`apps/mcp_server` (`apps/ packages/` — that package needs the `uv sync
--all-packages` above first, or it fails collection).

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
