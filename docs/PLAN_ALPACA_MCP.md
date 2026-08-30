# Plan D — Alpaca's own MCP server / CLI

**Status:** plan, not built. Written 2026-08-30 by `ID:MODEL1REAL`.

> **This is an ELIGIBILITY requirement, not a scoring one.** The hackathon rule is:
> *"projects must utilize either **Alpaca's** MCP server or its CLI tools."* Shipping without
> it risks the entry not being judged at all. It has also already been got wrong once.

---

## 0. 🚨 STEP 1 IS A BLOCKING VERIFICATION GATE — NO CODE UNTIL IT PASSES

CLAUDE.md §2 exists because of this exact requirement:

> *"When a requirement comes from an external spec, open the spec and quote it before
> building. Do not build against a plausible reading of a requirement you have not read."*

**Before writing one line of code, fetch these pages and quote the answers into
`fable5findings.md`:**

### A. `github.com/alpacahq/alpaca-mcp-server`
1. The exact package / entry-point name that `uvx` invokes.
2. The exact env var that scopes toolsets, **and its exact legal values, verbatim from the
   README**.
3. The exact tool names in the read-only toolsets — especially the option-chain and snapshot
   tools.
4. Whether the transport is stdio or HTTP.

### B. `github.com/alpacahq/cli`
1. Release asset naming for `linux/amd64`.
2. **The exact clock subcommand.** `docs/HACKATHON.md` asserts `alpaca clock get`. **That
   assertion is unverified and may be wrong** — it could be `alpaca market clock` or
   something else entirely.
3. The exact JSON-output flag.
4. **The exact auth env var names.**

### The gate
> **If either page cannot be fetched, STOP and report to the user. Do not proceed from
> memory. Do not infer one tool's command surface from the other's conventions.**

### 🪤 Named trap: the env var names probably do not match

This repo uses **`ALPACA_API_KEY` / `ALPACA_SECRET_KEY`** (`engine/features/clock.py:117-133`).
Alpaca's own tooling conventionally uses **`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`**. You
will be tempted to assume they are the same. **Verify, and if they differ, map them
explicitly in the subprocess `env=` dict** — do not export new globals into the process.

---

## 1. Resolve the contradiction in our own docs first

`docs/HACKATHON.md` §5 proposes **two** MCP sessions with disjoint toolsets, one of them an
`execution` session holding the `trading` toolset.

`apps/mcp_server/mcp_server/tools.py:9-19` says the opposite — that wrapping Alpaca's
execution-capable MCP tools into an LLM tool loop *"would violate this codebase's one
architectural rule."*

**Resolve toward read-only, and delete the two-session table from `HACKATHON.md` §5** so
nobody implements it. It is not merely the safer option — it is the **stronger claim**:

> Two disjoint sessions says: *"we mounted execution tools and trusted ourselves to only use
> them from the right place."* That is a **policy**, and policy is exactly what the five
> competing entries already claim.
>
> One read-only session says: *"there is exactly one Alpaca MCP session in this system and
> `place_option_order` is not in it. Execution never touches MCP at all — it goes
> `engine.risk` → `packages/broker` → Alpaca REST, deterministic Python end to end."* That is
> a **capability boundary with no exception.**

A boundary with a carve-out is weaker than one without. Also fix the same claim in
`apps/mcp_server/README.md`.

---

## 2. D.2 — wire the clock that already exists. **Do this FIRST.**

### ✅ Verified 2026-08-30

`packages/engine/engine/features/clock.py:54` defines `AlpacaClock`, which calls Alpaca's
`/v2/clock`. `clock_from_env()` at `:117` builds it from env. **`clock_from_env` has zero
non-test callers.** `engine/scanner/engine.py:86` calls `is_us_market_open(at)` — the
hardcoded holiday table — directly.

So: **the early-close and unscheduled-halt awareness that `docs/HACKATHON.md` §5 promises
from the Alpaca CLI already exists in Python and is simply unplugged.**

> 🚨 **Do not skip this because "the CLI does the same thing."** The CLI is the *eligibility
> artifact*. This is the actual functional upgrade. If you ship only the CLI you have added a
> subprocess and left the improvement on the floor.

Also note: `docs/HACKATHON.md` §5 says the gate is `pandas_market_calendars` in
`daily_cron.main`. **It is not there.** The real path is
`market_calendar.is_us_market_open` → `engine/scanner/engine.py:86` → `scheduler.py:317`
(`if not result.market_open`). Correct the doc; do not go hunting for code it describes.

### The change (~30 lines, no new dependency)

- `packages/engine/engine/scanner/engine.py` — `Scanner` gains `clock: ClockProvider | None = None`.
  `scan()` uses `await self.clock.now()` when present, else falls back to
  `is_us_market_open(at)`.
- `packages/engine/engine/scanner/types.py` — `ScanResult` gains
  `market_open_source: str = "local_calendar"`.
- Wire `clock_from_env()` at the scanner construction site.
- Surface `market_open_source` through `apps/api/app/services/council/scanner_status.py` →
  `schemas/scanner.py` (which already carries `market_open`), so the UI and the demo can show
  which source answered.

---

## 3. D.3 — the CLI, flag-gated, in front of the REST clock

New `packages/engine/engine/features/alpaca_cli.py`:

```python
async def cli_clock(*, timeout: float = 5.0) -> MarketClock | None:
    """`alpaca <verified clock subcommand>` via subprocess.

    Returns None on ANY failure — missing binary, non-zero exit, timeout,
    unparseable JSON. Never raises. A dev laptop will not have this binary and
    that must be a silent, correct fallback, not an error.
    """
```

Rules, each of which has bitten someone:

- **`asyncio.create_subprocess_exec` with an argv list. Never `shell=True`. Never
  string-interpolate into the command.** A subprocess inside the FastAPI process is a new
  attack surface; say so in the docstring.
- **`asyncio.wait_for(proc.communicate(), timeout)` AND `proc.kill()` in the timeout path**,
  or you leak a zombie process every tick on a loop that runs every 30 seconds.
- Return `MarketClock(..., source="alpaca_cli")`.

New `resolve_market_clock()` in `clock.py`, a three-step fallback chain, each step reporting
its own `source`:

```
CLI (only if USE_ALPACA_CLI=1)  →  AlpacaClock REST  →  local holiday calendar
```

**Default `USE_ALPACA_CLI=0`**, so D.2's behaviour is completely unchanged by D.3 landing.
Flip it deliberately.

**The judge-visible artifact** is `market_open_source: "alpaca_cli"` in the scanner status
payload. That is the demo evidence that the requirement is met — screenshot it.

---

## 4. D.5 — the Dockerfile. **LAST, and its own revertible commit.**

The runtime stage is `python:3.12-slim` plus `libpq5` and `ca-certificates`. **No `curl`, no
`wget`, no Go toolchain.** Installing the `alpaca` binary means either an apt line plus a
download, or a dedicated fetch stage with `COPY --from`.

- **Verify locally with `docker build -f apps/api/Dockerfile .` before pushing.**
  `railway.toml` sets `restartPolicyMaxRetries = 3` and `healthcheckTimeout = 600` — a broken
  image takes the whole app down, four days from the deadline.
- Add a build-time assertion — **`RUN alpaca version`, the SUBCOMMAND, not `--version`.**
  This CLI sets no root-command Version, so the flag form falls through to a bare
  invocation, demands credentials and exits 1. It failed every build with
  `{"error":"authentication required"}`, which reads like a secrets problem and is
  really a wrong-verb problem. Verified against the real binary 2026-08-30 — **so a bad
  download fails the build, not the runtime.**
- Keep `USE_ALPACA_CLI=0` in this commit. **The image change and the behaviour change must be
  independently revertible.**
- `uv` does reach the runtime image (`COPY --from=deps /usr/local /usr/local`), so `uvx` is
  available — but `uvx alpaca-mcp-server` downloads from PyPI on first invocation, meaning
  network at runtime and a slow first call. If §5 ships, pre-install the package in the deps
  stage instead.

---

## 5. D.4 — MCP client consumption. **The first thing to cut.**

`apps/agents/trading_agents/mcp_client/alpaca_mcp.py` — a thin stdio MCP client launching
`uvx alpaca-mcp-server` with the read-only toolsets verified in step 1. Exactly **one** call
site: an alternative option-chain fetch behind `USE_ALPACA_MCP=1`, falling back to
`engine.options.contracts.fetch_option_candidates`.

> **This is the piece most likely to eat a whole day and break the deploy. The requirement is
> "MCP server OR CLI" — §3 alone satisfies eligibility.** If Wednesday arrives and this is
> not working, drop it and say so in the write-up. **Two half-working integrations is a worse
> submission than one working one.**

---

## 6. Build order

```
0.  D.0 verification gate — fetch both pages, quote into fable5findings.md   BLOCKING
1.  D.1 delete the two-session table from HACKATHON.md §5 + mcp_server/README.md
2.  D.2 wire AlpacaClock into the scanner            ← the actual upgrade, do this first
3.  D.3 alpaca_cli.py + resolve_market_clock, USE_ALPACA_CLI=0
4.  D.5 Dockerfile, own commit, local docker build verified
5.  --- flip USE_ALPACA_CLI=1, screenshot market_open_source ---
6.  D.4 MCP client — only if everything else is done
```

---

## 7. Tests

| Test | Break this to make it fail |
|---|---|
| **`test_clock_falls_back_when_the_cli_binary_is_missing`** | Let `FileNotFoundError` propagate out of `cli_clock` |
| `test_cli_clock_returns_none_on_timeout_and_kills_the_process` | Remove the `proc.kill()` |
| `test_cli_clock_returns_none_on_unparseable_json` | Let `json.JSONDecodeError` propagate |
| `test_scanner_reports_market_open_source` | Hardcode the source string |
| `test_scanner_uses_the_local_calendar_when_no_clock_is_injected` | Make `clock` required |

The fallback chain must degrade **silently** CLI → REST → local calendar. A dev laptop has no
`alpaca` binary and no keys; that path must be the quiet normal case, not a warning storm.

Baseline: **792 passed, 9 skipped**; 9 pre-existing ruff errors.

---

## 8. Where you are most likely to go wrong

1. **Building against a remembered CLI surface.** `alpaca clock get` is asserted in
   `docs/HACKATHON.md` and **verified nowhere**. §0 exists for this.
2. **Assuming `ALPACA_API_KEY` is what the CLI reads.** Alpaca's own tooling uses `APCA_`
   prefixes.
3. **Believing the CLI is the upgrade.** `AlpacaClock` already does the functional job and is
   unwired. Ship §2 first.
4. **Editing the Dockerfile early and casually.** Last, own commit, local build verified.
5. **`shell=True`.** Never.
6. **Not killing the process on timeout** — one zombie per tick on a 30s loop.
7. **Implementing the two-session MCP design** because `HACKATHON.md` §5 still describes it.
   Delete that table in step 1 so it cannot mislead you later.
8. **Removing the fallback chain** once the CLI works on your machine.

---

*Related: [`HACKATHON.md`](HACKATHON.md) §5 · [`../apps/mcp_server/README.md`](../apps/mcp_server/README.md) · [`../CLAUDE.md`](../CLAUDE.md) §2*
