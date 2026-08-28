# `mcp_server` — read/propose-only MCP server for the trading agent council

An MCP (Model Context Protocol) server exposing **six deliberately
read-only / propose-only tools**, each a thin adapter over a function
that already exists in this codebase. No new business logic beyond
return-shape flattening.

Built for lablab.ai's "Alpaca AI Trading Agents Hackathon" (required tech:
Alpaca's Trading API, MCP server, CLI). This is **our own** MCP server
wrapping the existing safe pipeline — deliberately **not** Alpaca's own
MCP server, which exposes real order-placement tools directly to an LLM.
That would violate this codebase's one architectural rule (see the root
[`CLAUDE.md`](../../CLAUDE.md)):

> **Agents propose, deterministic code disposes.** Agents never call
> broker APIs directly — every order routes through
> `packages/engine/risk` → `packages/broker`.

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
