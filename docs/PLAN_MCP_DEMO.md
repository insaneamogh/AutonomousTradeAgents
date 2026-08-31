# Plan J — Alpaca's MCP server, consumed and demoable

**Status:** plan, not built. Written 2026-08-31 by `ID:MODEL1REAL`.
**Deliverable 2 of 3.**

> **The verification gate that `PLAN_ALPACA_MCP.md` §0 demanded is now PASSED.**
> Everything in §1 was fetched from the real README on 2026-08-31, not recalled.
> You do not need to re-run the gate for the MCP half — only for anything §1
> does not cover.

---

## 1. ✅ Verified spec — fetched 2026-08-31

**Entry point:** `uvx alpaca-mcp-server`

**Transport:** **stdio** by default. HTTP available via
`--transport streamable-http --port <n>`.

**Environment variables (quoted from the README):**

| Variable | Required | Default | Description |
|---|---|---|---|
| `ALPACA_API_KEY` | Yes | — | Your Alpaca API key |
| `ALPACA_SECRET_KEY` | Yes | — | Your Alpaca secret key |
| `ALPACA_PAPER_TRADE` | No | `true` | Set to `false` for live trading |
| `ALPACA_TOOLSETS` | No | all | Comma-separated list of toolsets to enable |

> 🎯 **The env var trap I predicted does NOT apply.** `PLAN_ALPACA_MCP.md` §0 warned
> the CLI/MCP might want `APCA_API_KEY_ID`. **It does not** — it reads
> `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`, the exact names this repo already uses.
> No mapping needed. Prediction was wrong; the fetched README is right.

**`ALPACA_TOOLSETS` legal values — the complete list:**

```
account · trading · watchlists · assets · stock-data · crypto-data
options-data · corporate-actions · news · fixed-income-data · locates
```

**Tools we want** (all in `options-data` / `stock-data`, all non-transactional):

```
get_option_contracts   get_option_chain      get_option_snapshot
get_option_bars        get_option_latest_quote
get_stock_bars         get_stock_snapshot    get_stock_latest_quote
```

**Tools we must NOT mount** (they live in `trading`):

```
place_option_order     exercise_options_position
```

**⚠️ `ALPACA_TOOLSETS` defaults to ALL toolsets when unset.** Omitting it mounts
`place_option_order` and `exercise_options_position`. **The variable is not optional
for us — it is the security control.**

---

## 2. The design — one read-only session, no exceptions

```
ALPACA_TOOLSETS=options-data,stock-data,assets,news
ALPACA_PAPER_TRADE=true
```

**No `trading`. No `account`. Ever.**

The claim this buys, and it is the strongest one available:

> *"There is exactly one Alpaca MCP session in this system, and `place_option_order`
> is not in it. Not because we told the model not to call it — because the tool is
> not loaded. Execution never touches MCP at all: it goes
> `engine.risk` → `packages/broker` → Alpaca REST, deterministic Python end to end."*

That is a **capability boundary with no carve-out**. Five competing entries claim
propose/dispose as *prompt policy*; this is the version a judge can verify by reading
one environment variable.

> **Do not add `trading` "just for the executor".** The executor does not need MCP —
> it already places orders through `packages/broker`. Mounting it would trade the
> only hard claim in this section for nothing.

---

## 3. Where to plug it in

`apps/agents/trading_agents/mcp_client/alpaca_mcp.py` — a thin stdio MCP client.

**Exactly one call site**, behind `USE_ALPACA_MCP=1` (default off):

> The option-chain fetch in `drafter._fetch_option_candidates`, falling back to the
> existing `engine.options.contracts` direct-SDK path on any failure.

Why this call site: it is the one place options data is already fetched, the fallback
is a one-liner, and a failure degrades to today's behaviour rather than breaking a
pass. **Never make MCP the only path to anything.**

The `mcp` SDK is already a workspace dependency (`mcp==2.1.1`, used by
`apps/mcp_server`). Client API is `mcp.client.Client` + `mcp.StdioServerParameters` —
`apps/mcp_server/README.md` documents a working round-trip against exactly this
version. Reuse that knowledge; do not rediscover it.

**Latency warning:** `uvx alpaca-mcp-server` downloads from PyPI on first invocation.
Pre-install the package in the Dockerfile's `deps` stage rather than paying a cold
download inside a live council pass.

---

## 4. The demo — two different things, labelled honestly

| What | Satisfies the hackathon rule? | Demo value |
|---|---|---|
| **Alpaca's MCP server** (this plan) | **Yes** | Medium — a config line |
| **Alpaca CLI** ([`PLAN_CLI_SURFACE.md`](PLAN_CLI_SURFACE.md)) | **Yes** | Medium — visible in System Health |
| **Our own `apps/mcp_server/`** | **No — wrong direction** | **Highest** |

### The 30 seconds that will actually land

`run_council_pass("NVDA")` in **Claude Desktop**, against our own MCP server, returning
the full council rationale — regime, analyst scores, selected strategy, risk verdict,
the contract funnel. It exists, it works, it needs no new code.

**Say what it is in the same breath:** *"Our agent is itself MCP-addressable — that's
this. Separately, we consume Alpaca's own MCP server, read-only, for the option chain
— that's the requirement."*

> 🚨 **Never present `apps/mcp_server/` as satisfying the Alpaca MCP requirement.**
> That exact error already cost this repo a day. It exposes *our council to* Claude;
> the rule asks us to *consume Alpaca's*. Both are now true — say which is which.

### Judge-visible evidence checklist

- [ ] Claude Desktop calling `run_council_pass("NVDA")` — screen recording
- [ ] The `ALPACA_TOOLSETS` line, on screen, with `trading` visibly absent
- [ ] `marketOpenSource: "alpaca_cli"` in System Health
- [ ] Terminal: `alpaca clock` against the submitted account

---

## 5. Tests

| Test | Break this to make it fail |
|---|---|
| **`test_toolsets_never_include_trading`** | Add `trading` to the toolset constant. **The most important test here** — it is the entire security claim, as a unit test. |
| `test_toolsets_are_explicit_not_defaulted` | Omit `ALPACA_TOOLSETS` → the server mounts everything |
| `test_chain_fetch_falls_back_when_mcp_is_down` | Let the MCP error propagate |
| `test_mcp_disabled_by_default` | Default `USE_ALPACA_MCP` to on |

---

## 6. Where you will go wrong

1. **Leaving `ALPACA_TOOLSETS` unset.** It defaults to *all* toolsets — you would mount
   `place_option_order` into an LLM's tool list. The variable is the control.
2. **Mounting `trading` for the executor.** It does not need it.
3. **Making MCP the only chain-fetch path.** Always fall back.
4. **Claiming our own MCP server meets the requirement.**
5. **Re-running the §0 verification gate.** For the MCP half it is done — §1 is the
   fetched README. Spend the time on the build.
6. **Paying the `uvx` cold download inside a live pass.** Pre-install in `deps`.

---

*Related: [`PLAN_CLI_SURFACE.md`](PLAN_CLI_SURFACE.md) · [`PLAN_OPTIONS_AGENTS.md`](PLAN_OPTIONS_AGENTS.md) ·
[`PLAN_ALPACA_MCP.md`](PLAN_ALPACA_MCP.md) · [`../apps/mcp_server/README.md`](../apps/mcp_server/README.md)*
