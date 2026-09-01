# `mcp_server` — read/propose-only MCP server for the trading agent council

An MCP (Model Context Protocol) server exposing **six deliberately
read-only / propose-only tools**, each a thin adapter over a function
that already exists in this codebase. No new business logic beyond
return-shape flattening.

This is **our own** MCP server, wrapping the existing safe pipeline. It
upholds the codebase's one architectural rule (see the root
[`CLAUDE.md`](../../CLAUDE.md)):

> **Agents propose, deterministic code disposes.** Agents never call
> broker APIs directly — every order routes through
> `packages/engine/risk` → `packages/broker`.

**Scope note, since the wider app changed this week:** the sentence above is still
completely true of every tool *in this server* — nothing here ever reaches
`packages/broker`, full stop, see "Will never build" below. It is no longer true of
the app as a whole, and this file describes only itself, not the rest of the repo.
Two separate, unattended-execution paths now exist elsewhere in this codebase — an
equity auto-approve sweeper (`apps/api/app/services/orders/auto_approver.py`) and a
live options Bull/Bear council whose winning agent can call a real, guarded trade
tool (`apps/agents/trading_agents/options/tools/guard.py`) — both off by default,
both hard-coded to paper trading, both gated behind an explicit consent step, and
both described in full in the root [`README.md`](../../README.md)'s "How autonomous
is 'autonomous'" section. Neither is reachable from this MCP server, through any
tool below, under any config — the architecture rule quoted above still holds
end-to-end, it just no longer implies "and therefore a human always approves it,"
which used to be true of the whole app and now isn't.

---

## ⚠️ This does NOT satisfy the hackathon's MCP requirement

**Read this before assuming the requirement is covered.**

The rule is: *"projects must utilize either **Alpaca's** MCP server or its
CLI tools."* This server points the other way — it exposes **our council
TO** an MCP client. Necessary direction for our product; wrong direction
for the requirement. Shipping only this risks eligibility.

An earlier version of this README argued that adopting Alpaca's MCP server
would violate the propose/dispose rule, because it "exposes real
order-placement tools directly to an LLM." **That premise is incorrect.**
Alpaca's server supports `ALPACA_TOOLSETS` filtering (verified live against
`github.com/alpacahq/alpaca-mcp-server`'s own README — see
[`docs/PLAN_ALPACA_MCP.md`](../../docs/PLAN_ALPACA_MCP.md) §0 and the build
log in [`fable5findings.md`](../../fable5findings.md) for the verbatim
quote), so a session can be mounted with market-data toolsets and **no
`trading` toolset at all**. Legal `ALPACA_TOOLSETS` values, verbatim from
that README: `account`, `trading`, `watchlists`, `assets`, `stock-data`,
`crypto-data`, `options-data`, `corporate-actions`, `news`,
`fixed-income-data`, `locates`.

**A previous draft of this section went on to propose two sessions** — a
`research` session on read-only toolsets, alongside a second `execution`
session mounting `trading`/`account`, on the theory that gating its calls
downstream through `engine.risk` would keep it safe. **That table has been
deleted.** It wasn't just risky, it was a strictly weaker claim than not
building it: "we mounted an execution-capable toolset into an LLM tool
loop and trust ourselves to only invoke it from the right place" is a
*policy*, and policy is exactly what several competing entries already
claim (see `docs/HACKATHON.md` §4). It is also exactly what this file's
own "Will never build" section below already says would violate the
architecture rule — the two sections contradicted each other, and
read-only-only is the version that survives.

**The resolution: exactly ONE Alpaca MCP session, ever, read-only, with no
execution tool in it.** `place_option_order` and every other `trading`-
toolset tool is simply never mounted, in any session — not gated by
policy, not downstream of a risk check, not behind a flag. That is a
**capability boundary with no exception**, which is a stronger claim than
one with a carve-out:

> "There is exactly one Alpaca MCP session in this system and
> `place_option_order` is not in it. Execution never touches MCP at all —
> it goes `engine.risk` → `packages/broker` → Alpaca REST, deterministic
> Python end to end."

Far from violating the rule, mounting only read-only toolsets *strengthens*
it: the boundary stops being a prompt instruction and becomes a capability
boundary enforced by the vendor's own config. "The analyst agents cannot
place an order, because `place_option_order` is not in their tool list" is
a materially stronger claim than "we told them not to."

**So: keep this server** (an agent that is itself MCP-addressable is a
genuine bonus), **and additionally** consume Alpaca's own MCP server (one
read-only session) and/or CLI. See
[`docs/HACKATHON.md`](../../docs/HACKATHON.md) §5 for the integration
design and plug points.

**The generalisable lesson:** when a requirement comes from an external
spec, open the spec and quote it before building against it. This was a
coherent argument built on an unverified premise about a third-party
tool's capabilities — cheap to check, expensive to get wrong.

## Will never build

`place_order`, `approve_proposal`, `execute_trade`, `cancel_order`,
`close_position` — anything that mutates broker or portfolio state. No
tool, code path, or flag in this server may reach
`packages/engine/risk` → `packages/broker`. Every tool below either reads
existing state, or runs the deterministic council read-only (a council
pass writes an audit row — never an order).

## The six tools

| Tool | Wraps | Notes |
|---|---|---|
| `run_council_pass(symbol, horizon="short")` | `trading_agents.runtime.run_council()` | The centerpiece — full council rationale (regime, analyst scores, selected strategy, risk verdict, proposal). Writes one audit row, same as a real pass. Never approves/executes/notifies. |
| `list_positions()` | `positions_service.list_open_positions()` | Open agent-managed positions + live marks. Postgres-only; honest `[]` otherwise. |
| `list_recent_decisions(symbol=None, action=None, limit=20, offset=0)` | `decisions_list.list_decisions()` | Browsable decision history. Postgres-only; returns `postgres_backed: false` otherwise instead of erroring. |
| `get_scanner_status()` | `scanner_status.build_scanner_status_report()` | Trigger-loop state — armed / off / what it last saw. Self-guarding; works with no Postgres. |
| `get_veto_ledger(window_days=30)` | `ghost_service.build_veto_ledger()` | "Here's why risk said no" — named `veto_rule` scorecard (`pdt_block`, `daily_drawdown_halt`, etc.), not paraphrased. Postgres-only; `postgres_backed: false` otherwise. |
| `list_watchlist()` | `watchlist_store.get_watchlist_store().list_items()` | Symbols the demo user is tracking. Works with zero Postgres setup (in-memory fallback). |

All six tools always act as one fixed demo identity
(`app.services.auth.auth_store.FIXTURE_USER_ID`, overridable via
`MCP_DEMO_USER_ID`) — there's no per-caller session to authenticate here,
the caller is Claude Desktop / the Inspector, not a mobile-app user.

## Setup

From the repo root:

```bash
uv sync --all-packages
```

Runs entirely in-memory by default (`USE_POSTGRES` unset) — every tool
returns an honest empty/off result where Postgres would normally back it.
For full-fidelity mode (real decision history, real positions):

```bash
USE_POSTGRES=1 uv sync --all-packages   # then apply migrations, see root Makefile's `migrate` target
```

## Standalone smoke test

```bash
uv run --directory <repo-root> --package mcp_server python -m mcp_server
```

Starts the server over stdio and blocks waiting for JSON-RPC input — that
hang (not an immediate exit) is the expected "it's alive" signal for a
stdio server with no client attached yet. Ctrl-C to stop.

To actually exercise it without a full MCP client, run the test suite
instead (below) — every tool is a plain `async def` in `tools.py`, directly
awaitable with no transport involved.

## MCP Inspector

```bash
npx @modelcontextprotocol/inspector uv run --directory <repo-root> --package mcp_server python -m mcp_server
```

This opens the Inspector's web UI, which is the intended interactive way
to browse the six tools and call them by hand. Honest caveat on
verification: this sandbox has no browser to click through, and the
Inspector's own `--cli` mode (its non-interactive flag) hit argument-
parsing friction in testing whenever the target command carried its own
flags (e.g. `--directory`, `--package`) — it appears to re-parse the
forwarded command line for its own named options rather than treating
everything after the target strictly as opaque argv, so tokens like
`--directory <path>` in the target got reinterpreted as inspector-cli's
own options. That looks like a limitation in that npm package version,
not in this server.

What was actually verified, directly, in place of a working Inspector
round trip: a real JSON-RPC client session over stdio using the `mcp`
SDK's own reference client (`mcp.client.Client` +
`mcp.StdioServerParameters`) — the same transport machinery Inspector
itself uses underneath — spawning the real `python -m mcp_server` entry
point as a subprocess, calling `tools/list` (all six names came back) and
`tools/call` against `list_watchlist` / `get_scanner_status` (both
returned correct, JSON-safe, camelCased payloads). See "SDK spike"
below for how this was pinned down.

## Tests

```bash
uv run --package mcp_server pytest apps/mcp_server/tests -v
```

Every tool is a plain `async def` — tests `await` them directly, no MCP
client/transport needed. Covers: `run_council_pass` under a forced mock
LLM (symbol/horizon validation included — a malformed symbol must raise,
never silently reach an LLM prompt); the honest-empty + `postgres_backed`
shapes for `list_positions` / `list_recent_decisions` / `get_veto_ledger`
in mock-store mode; `get_scanner_status`'s off-report shape; an in-memory
`list_watchlist` add+list round trip; and a regression guard constructing
the real server from `server.py` and asserting all six tools are
registered.

## SDK spike — what the installed version actually looks like

The brief for this server assumed `mcp.server.fastmcp.FastMCP` (the SDK's
v1 decorator API). `uv add "mcp[cli]"` resolved **`mcp==2.1.1`**, which no
longer ships that module — importing it raises
`ModuleNotFoundError` pointing at `mcp.server.mcpserver.MCPServer` as the
v2 replacement. This was confirmed directly (a throwaway spike server +
a real client round trip), not assumed from training data:

- `from mcp.server.mcpserver import MCPServer` (not `fastmcp.FastMCP`)
- `mcp = MCPServer("name")`
- `mcp.add_tool(fn)` — registers a plain callable by reference (used by
  `server.py` for all six tools); `mcp.tool()` (a decorator factory) also
  exists but isn't used here, per the brief's router-thin convention
- `mcp.run(transport="stdio")` — `"stdio"` is already the default
- `await mcp.list_tools() -> list[MCPTool]` — used by the test suite's
  registration regression guard

## What was verified vs. assumed

- **Verified directly**: the exact `mcp==2.1.1` API shape above (via a
  throwaway spike server + a real `mcp.client.Client` JSON-RPC round trip
  over stdio, both against the spike and against the real
  `python -m mcp_server` entry point); every wrapped function's actual
  signature and return shape (read the real source, not inferred); the
  `USE_POSTGRES` self-guard situation for each of the six tools
  (`decisions_list.list_decisions` and `ghost_service.build_veto_ledger`
  do **not** self-guard — their routers do instead — so this server
  replicates the guard directly; `scanner_status` and `watchlist_store`
  already self-guard); the `FIXTURE_USER_ID` lazy-seed gotcha
  (`PostgresAuthStore._ensure_seed()` is called from every store method,
  confirmed by reading `postgres_auth_store.py`); that `apps/api/app` was
  the one workspace package missing a `py.typed` marker (added it —
  `apps/api/app/py.typed` — since this server is the first package to
  cross-import `app.*`, and mypy strict otherwise treats every symbol
  from it as untyped `Any`); the full existing test suite (736 passed, 9
  skipped) both with and without this server's changes, to confirm the
  workspace-level `pyproject.toml` edits caused zero regressions.
- **Deliberately not built (first cut)**: `run_council_pass` passes
  `equity_resolver=None` to `resolve_feature_provider`, unlike
  `apps/api/app/routers/agent.py`'s `_execute_council`, which resolves the
  caller's real reconciler-cached account equity from Postgres. An MCP
  caller has no authenticated session to resolve real equity from, so a
  Postgres-backed run through this tool sizes the ATR sizer against the
  synthetic-feature 100k equity fixture, not the demo user's real broker
  equity, even with `USE_POSTGRES=1`.
- **Not interactively verified**: the MCP Inspector's web UI (no browser
  in this sandbox) and its `--cli` mode (argument-parsing friction — see
  above). The protocol-level round trip it would exercise was verified
  instead via the SDK's own client library directly, against both a
  spike server and the real entry point.

## Judge script (under 5 minutes)

1. `run_council_pass("NVDA")` — full council rationale for one symbol:
   regime, analyst scores, selected strategy + why, risk verdict, and the
   proposal itself if risk approved it. One audit row written; nothing
   executed.
2. `get_veto_ledger()` — the named risk rules that have said no, and what
   they blocked.
3. `get_scanner_status()` — whether the always-on trigger loop is armed,
   and what it's watching.
4. Point out: no tool here can place, approve, or cancel anything. Every
   order in this app still routes through
   `packages/engine/risk` → `packages/broker`, exactly as before this
   server existed — this MCP server only ever reads that pipeline's
   output or triggers a read-only council pass through the same front
   door the mobile app uses.
