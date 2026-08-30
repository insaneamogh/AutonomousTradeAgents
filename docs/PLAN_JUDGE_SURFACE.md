# Plan G — the judge surface: open access, the "why" page, and demoing CLI/MCP

**Status:** plan, not built. Written 2026-08-30 by `ID:MODEL1REAL`.
**Priority: after [`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md), alongside
[`PLAN_LEDGER_SURFACE.md`](PLAN_LEDGER_SURFACE.md).**

Three things a judge needs that do not exist: they cannot get in, they cannot see
why an options trade was chosen, and there is nothing on screen proving we use
Alpaca's own CLI.

---

## 1. Open the app to judges — without handing them the trade buttons

### The problem

Every visitor hits the magic-link / dev-token login. A judge will not sign up. They
will open the link, see a login wall, and score what they can see — which is nothing.

### ✅ The good news, verified 2026-08-30

**Every money-touching route already refuses the dev bypass.** I enumerated all 24
mutating routes:

| Route | Auth | Bypass? |
|---|---|---|
| `POST /approvals/{id}/decision` | `require_real_auth` | **refused** |
| `POST /orders/execute/{id}` | `require_real_auth` | **refused** |
| `POST /positions/{id}/close` | `require_real_auth` | **refused** |
| `POST/DELETE /broker/connections/*` | `require_real_auth` | **refused** |
| `POST /broker/connections/{id}/auto-approve-consent` | `require_real_auth` | **refused** |
| `POST /circuit-breaker/acknowledge` | `require_real_auth` | **refused** |

So a bypass visitor **cannot approve, execute, close, revoke, arm auto-approve, or
touch the broker connection.** That is the right posture already and it was not an
accident — `approvals.py:63` documents it.

### ⚠️ Three routes that WOULD accept the bypass — close these first

| Route | Risk if a judge (or a crawler) hits it |
|---|---|
| `POST /agent/run`, `POST /agent/run/start` | Triggers a real council pass — **~$0.04 of LLM spend per call, unbounded.** A crawler or an impatient judge clicking twenty times is a real bill and pollutes the decision log during judging. |
| `POST /watchlist`, `DELETE /watchlist/{symbol}` | Changes **what the agent trades**. A judge removing NVDA mid-contest silently changes the P&L being judged. |
| `POST /review/{decision_id}` | Pollutes the reflection/calibration data. |

**Fix:** move `watchlist` mutations and `review` to `require_real_auth`. For
`agent/run`, either require real auth **or** rate-limit the bypass path hard
(1/minute/IP, and refuse when `AUTO_APPROVE_ENABLED=1`). Judges do not need to
trigger runs — the scheduler produces them.

### The second problem — and it kills the naive fix

`DEV_AUTH_BYPASS=1` resolves an unauthenticated caller to
`FIXTURE_USER_ID = 00000000-0000-0000-0000-000000000001`. Every agent decision is
written to `AGENT_CRON_USER_ID = 43221580-…`. So flipping the flag today gives a
judge a **fully working, completely empty app.**

**And a "proper login" does not fix it either.** Verified 2026-08-30:

- `positions_snapshot` rows are only written for users the reconciler fleet
  iterates, and it iterates `list_active_connections_by_broker("alpaca")` — i.e.
  **users who have their own broker-connection row.**
- A newly-signed-up judge has none, so `store.get_account()` finds no snapshot and
  returns the **hardcoded cold-boot fixture** (`postgres_store.py:99`):
  `equity=100_000, cash=100_000, buying_power=200_000, today_pnl=0`.
- The tell is `buying_power=200_000`. The real account reports **400,000**. A judge
  who signs up is looking at a constant in our source code, not at the account.
- Decisions, positions, ghost marks, insights are all scoped by `user_id` too — so
  they see zero of everything.

**So the question "will the P&L stay correct no matter who logs in?" has a firm
answer: no.** Not via the bypass, and not via a normal signup. Whoever the judge is
authenticated as must resolve to the account that owns the data.

### 🔑 Recommended design: a read-only demo session, keeping real login intact

Do **not** turn on a blanket bypass. Add a **signed, long-lived, read-only demo
token** carried in the submission link:

```
https://…up.railway.app/?demo=<signed-token>
```

- On load, the app exchanges `?demo=…` for a session scoped to `DEMO_USER_ID`
  (= the cron user), and stores it like any other session.
- Real magic-link / Google login and logout are **untouched**. You still sign in
  normally; the demo token is an additional, weaker identity.

**The elegant part: the security boundary already exists.** Mint the demo identity
as `AuthedUser(..., is_dev_bypass=True)` — or add an `is_demo` flag that
`require_real_auth` rejects the same way. Then **every money route refuses it with
no new code**, because all 6 of them already call `require_real_auth`:

```
approvals/{id}/decision · orders/execute/{id} · positions/{id}/close
broker/connections/* · broker/…/auto-approve-consent · circuit-breaker/acknowledge
```

Read routes keep working. That is the whole feature: a link that reads everything
and changes nothing, without weakening a single existing check.

Still close the three leaky routes named above (`agent/run`, watchlist mutations,
`review/{id}`) — the demo identity must not be able to spend LLM budget or change
the trading universe either.

Token hygiene: sign it with the existing JWT secret, give it a **fixed expiry past
the submission deadline**, and make it revocable (it is a session — `logout` already
revokes by `sid`). Do not make it a permanent unsigned string.

### Say it on screen

Put a small banner on the demo build: *"Read-only demo. Trading actions are disabled."*
A judge who clicks Approve and gets a 401 with no explanation reads it as a bug. A
judge who sees the banner reads it as deliberate.

> **Do not remove `require_real_auth` from anything to make the demo smoother.** The
> whole point is that a stranger can read everything and change nothing.

---

## 1b. ~~"Couldn't reach the agent server" on the FIRST council run~~ — FIXED 2026-08-30

Reported 2026-08-30: clicking **Run** the first time shows
*"Couldn't reach the agent server — check your connection and try again."*
The second attempt works. A judge's first action is exactly this button, so it
must not fail.

### ✅ Why the symptom looks like "first time only" — verified

`apps/mobile/src/lib/queryClient.ts`:

```
line 17   queries:   retry: 2
line 21   mutations: retry: 0
```

`useStartCouncilRun` is a **mutation**. Every *query* on the page (account,
positions, activity, ghost, vetoes, scanner) silently retries twice and heals
through a transient blip. **The council-run mutation does not retry at all**, so the
same blip that everything else absorbs surfaces as a hard red error on this one
button. That is why it looks like "only the first run, only this control".

### What the message actually means

`runErrorMessage` (`Picks.tsx:188`) returns that string **only when the error has no
`status`** — i.e. `fetch()` itself rejected and no HTTP response was ever received.
Everything with a status gets a specific message. So this is a genuine
transport-level failure, not a server refusal.

**Ruled out by inspection** — do not re-investigate these:
- **Not CORS.** The web build is served from the same origin as the API
  (`resolveBaseUrl` returns `window.location.origin` for a non-dev web build), so
  there is no preflight.
- **Not the base URL.** `EXPO_PUBLIC_API_URL` is unset, which is correct — the
  same-origin branch handles it and survives a domain change with no rebuild.
- **Not the token refresh.** `refresh()` returns `null` rather than throwing, and the
  shared `inFlightRefresh` promise already de-dupes concurrent 401s.
- **Not a 502/429/422** — all of those carry a status and their own message.

### Most likely cause

**Container restarts.** The service was redeployed four times on 2026-08-30; a click
landing in that window gets a connection reset, `fetch` rejects, and the retry
succeeds once the new container is up. Railway cold starts produce the same shape.

⚠️ **Get evidence before assuming that is the whole story.** Reproduce with the
browser devtools **Network** tab open and read the failed row — `net::ERR_CONNECTION_
REFUSED`, `net::ERR_FAILED`, or a stalled request each point somewhere different. One
look settles it; guessing does not.

### ✅ Shipped 2026-08-30

`RequestOptions.retryOnNetworkError` retries once, 1s later, **only** when `fetch`
itself rejected, and **only** on calls that opt in. `useStartCouncilRun` is the sole
caller. The message no longer blames the user's connection.

**The safety property is the tested one:** `orders/execute`, `approvals/decision` and
`positions/close` do not pass the flag, so they are never re-sent. Revert-checked —
making the retry unconditional fails the test that asserts an order-placing call
fetches exactly once.

### The original fix note — kept for context

1. **Retry a status-less failure once, on this mutation only.** A rejected `fetch`
   means no response was received, so a single retry after ~1s is safe here: the
   worst case is a duplicate council run (~$0.04), and `/agent/run/start` places no
   order.
   > 🚨 **Do NOT make this a blanket mutation retry.** `orders/execute`,
   > `approvals/decision` and `positions/close` are mutations too, and auto-retrying
   > those is how you place a trade twice. Scope it to the council-run call, or gate
   > it behind an explicit `retryOnNetworkError: true` request option that only this
   > call passes.
2. **Split the message.** A status-less failure after the retry should read
   *"The server didn't respond — it may still be starting up. Try again in a
   moment."* "Check your connection" blames the user for our cold start.

---

## 2. The "why was this options trade picked" page

### What exists

- `GET /decisions` — the list (already renders, with the new `AUTO` pill).
- `GET /decisions/{id}/timeline` → `build_biography` — reads
  `reasoning.drafter_rationale`, analyst summaries, status.
- `PickDetail.tsx` on desktop.
- **`reasoning.contract_funnel` is persisted on every options pass** — approved and
  refused — as `{counts, rejection_reason, selected_occ}`. **Nothing reads it.**
- `reasoning.risk_checks_passed`, `risk_veto_rule`, `risk_trim_rules`,
  `strategy_fit`, `sizing`, `feature_snapshot` (now including `patterns`) — all
  persisted, none rendered.

### What to build

Extend `build_biography` and the timeline response to carry the full deterministic
record, then render it as a **decision detail page** reachable from every row in the
Decisions list. Sections, in this order — it should read as a narrative:

1. **The setup** — `strategy_fit`: which strategy won, its score, and the named
   components that got it there. Plus the scan trigger that woke it, if any.
2. **The candles** — `feature_snapshot.patterns`: `top_pattern`, its score, and the
   reversal/continuation/compression numbers. This is new and nothing shows it.
3. **What the analysts said** — technical / fundamental / macro scores + theses.
4. **The contract funnel** — the money shot for options:
   *4,128 contracts → 2,064 calls → 1,843 in the DTE window → 130 in the delta band
   → 3 liquid → 1 bought.* On a HOLD, show the **named `rejection_reason`**
   (`no_delta_in_band`, `no_liquid_contract`, `iv_outside_plausible_band`). **This is
   the direct answer to "it just says HOLD and I don't know why"**, which was a real
   complaint and is currently answerable only from the database.
5. **Sizing arithmetic** — budget, ask, multiplier, qty, % of equity.
6. **The risk verdict** — every rule that **passed** (`risk_checks_passed`), not just
   the one that vetoed. "15 named rules cleared this" is a far stronger statement than
   "approved", and the data is already there.
7. **Outcome** — fill, exit reason, realised P&L, and the ghost mark if it was refused.

### Why this matters more than it looks

The hackathon judges Technology Implementation. **This page is the evidence.** Right
now the system computes an enormous, genuinely-good deterministic audit trail and
throws all of it at a JSONB column nobody reads. Rendering it costs no new
computation — it is pure surfacing of work already done.

Make it **linkable** (`/decisions/{id}`) so the write-up and the video can point at a
specific one.

---

## 3. How to actually demo the Alpaca CLI and MCP

### Be honest about what each one is

| | Status | Satisfies the eligibility rule? |
|---|---|---|
| **Alpaca CLI** | ✅ shipped, `USE_ALPACA_CLI=1` | **Yes — this is the one that counts.** |
| **Alpaca MCP server** | ❌ cut (D.4) | Would also count, not built |
| **Our own MCP server** (`apps/mcp_server/`) | ✅ built, 6 tools | **No** — wrong direction. Bonus only. |

The rule is *"Alpaca's **own** MCP server **or** its CLI tools."* The CLI alone
satisfies it. Do not claim the MCP requirement is met by `apps/mcp_server/` — that
mistake has already been made once in this repo and is documented in
[`PLAN_ALPACA_MCP.md`](PLAN_ALPACA_MCP.md).

### The CLI is real but currently invisible — fix that

Today it is one call: the scanner's market clock resolves
**CLI → REST → local calendar**, and `ScanResult.market_open_source` records which
answered. That satisfies eligibility, but a judge cannot see it.

**Three things to build, cheapest first:**

1. **Show `market_open_source` in the UI.** The Settings "System Health" card already
   lists Council / Approvals / Broker / Reconciler. Add
   *"Market clock — `alpaca_cli`"*. ~30 minutes, and it is the screenshot that
   proves the requirement.
2. **A read-only CLI diagnostics panel.** A `GET /api/v1/ops/alpaca-cli` endpoint
   that shells `alpaca clock` (and optionally `alpaca account get`) and returns the
   **raw JSON the binary printed**, rendered verbatim in the UI. Seeing Alpaca's own
   tool output inside our app is far more convincing than a string field.
   - Reuse `engine/features/alpaca_cli.py`'s subprocess discipline: argv list, never
     `shell=True`, `proc.kill()` + `await proc.wait()` on timeout.
   - Read-only subcommands only. **Never expose an argument the caller controls** —
     fixed argv, no user input reaching the command line.
3. **Record the terminal in the video** — `alpaca clock`, `alpaca account get`,
   against the submitted account. Ten seconds, unambiguous.

### The MCP demo — use the one we have, label it correctly

`apps/mcp_server/` is genuinely good and genuinely finished: six read-only tools,
stdio, works in Claude Desktop. **`run_council_pass("NVDA")` returning the full
council rationale live in Claude Desktop is the single most impressive 30 seconds
available**, and it needs no new code.

Demo it — and in the same breath say what it is: *"our agent is itself
MCP-addressable; separately, the Alpaca **CLI** is what satisfies the tooling
requirement."* Claiming otherwise is the exact error that cost this repo a day.

If there is spare time after everything above, build D.4 (read-only Alpaca MCP for
the option-chain fetch). It is the first thing to cut and it should stay that way.

---

## 4. Verification

- **Auth:** with `DEV_AUTH_BYPASS=1` set, open the app in a **private window** (no
  session). Confirm: dashboard, decisions, positions, insights all populate with the
  cron user's data — and that `POST /approvals/{id}/decision` returns 401. Test the
  401 explicitly; do not assume it from reading the code.
- **Detail page:** open a decision that was a HOLD on an options pass and confirm the
  named `rejection_reason` is on screen.
- **CLI:** `curl /api/v1/scanner/status` and confirm `marketOpenSource: "alpaca_cli"`.
  If it says `alpaca_rest` or `local_calendar`, the binary is missing or
  `USE_ALPACA_CLI` is unset — the fallback is silent by design.

**Baseline: 961 passed, 10 skipped.** `git stash` and re-run before blaming your change.

---

## 5. Where you will go wrong

1. **Flipping `DEV_AUTH_BYPASS=1` without `DEMO_USER_ID`.** Judges get a working,
   empty app — worse than a login wall.
2. **Setting `ENV=production` to "make it production-ready".** That force-disables
   the bypass and the demo silently closes.
3. **Relaxing `require_real_auth` anywhere** to make the demo smoother. Read
   everything, change nothing.
4. **Leaving `agent/run` and the watchlist mutations open.** LLM spend and a
   changed trading universe, mid-judging.
5. **Claiming `apps/mcp_server/` satisfies the Alpaca MCP requirement.** It does not.
6. **Passing user input into the `alpaca` subprocess.** Fixed argv only.
7. **Building the counterfactual curve or the candle chart before the decision detail
   page.** The detail page is the Technology Implementation evidence.

---

*Related: [`PLAN_NEXT.md`](PLAN_NEXT.md) · [`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md) ·
[`PLAN_LEDGER_SURFACE.md`](PLAN_LEDGER_SURFACE.md) · [`PLAN_ALPACA_MCP.md`](PLAN_ALPACA_MCP.md) ·
[`../CLAUDE.md`](../CLAUDE.md)*
