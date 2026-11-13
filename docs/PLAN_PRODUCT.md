# Plan: one usable product (2026-11-13)

Operator: "I still don't see a complete working autonomous agent system.
The phone UI is very bad. It should be usable as a website and also as an
app. Want an interactive UI like getleft.app."

## 1. Why the desk looks dead (measured on Railway, 2026-11-13)

`railway variables` shows the scheduler, auto-approve and auto-trade all ON,
and **no LLM key of any kind**: no `ANTHROPIC_API_KEY`, no `GLM_API_KEY`, no
`LLM_PROVIDER`, with `AGENTS_REQUIRE_REAL_LLM=1`. Every council pass
therefore raises before it thinks. Add the drawdown breaker latched since
2026-09-08. Nothing in the app says either of these things; the screens
just show old rows. Also `LIVE_TRADING_ENABLED=0` is set, which is the
deploy-level lock: the new admin live switch will answer 409 until that
variable is removed.

Code cannot supply the keys. Code can stop hiding the reason.

## 2. What gets built

**A. Autonomy readiness (API).** `GET /api/v1/agent/autonomy` returns one
verdict (`running` / `idle` / `blocked`), a headline, and a checklist:
LLM usable (not MOCK, key present), fallback armed, scheduler armed, broker
connected and session alive, breaker not latched, auto-approve env +
consent, market open, daily spend under cap, last and next sweep. Each
failing check carries the exact fix ("set GLM_API_KEY on Railway",
"acknowledge the breaker"). Read-only, deterministic, no LLM.

**B. One web UI at every width.** Today the web build shows the DOM
"desktop" tree only on wide screens and falls back to the React Native
screens on a phone browser, which is the bad phone UI. The desktop tree
becomes responsive (bottom tab bar under 768px, single-column tiles,
44px touch targets, safe-area insets) and serves every width. A web
manifest and icons make it installable to a phone home screen.

**C. A Home made of tiles.** Principles taken from the reference site, not
its look: calm, few colours, very large numerals, each fact in its own
tile, dot grids instead of charts where a count is the point, segmented
switches instead of menus. Tiles: Autonomy (verdict + checklist, tap to
expand), Equity today, Open positions, Pending picks (approve from the
tile), Refusal Ledger (dot grid: one dot per refusal, coloured by whether
it would have lost or made money), Next sweep countdown, Trading mode.

**D. Native app.** The same Home tiles and Autonomy checklist in the React
Native Home tab, so the store build is not left behind.

## 3. Not in this round
Groww and INDstocks adapters; new data sources; anything that changes what
trades. The signal still has no edge (PLAN_PLATFORM section A) and the
Phase 7 go-live criteria are unmet.

## 4. Operator steps after deploy
1. Set an LLM key: `GLM_API_KEY` + `LLM_PROVIDER=glm` (and
   `ANTHROPIC_API_KEY` to arm the Sonnet 5.5 fallback).
2. `provider_check --live` on Railway.
3. Acknowledge the breaker in the app.
4. Set `ADMIN_EMAILS`. Remove `LIVE_TRADING_ENABLED=0` only when real
   money is intended.
