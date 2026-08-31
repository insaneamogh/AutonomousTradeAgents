# Plan I — surface the Alpaca CLI so a judge can see it

**Status:** plan, not built. Written 2026-08-31 by `ID:MODEL1REAL`.
**Deliverable 1 of 3. Smallest of the three — do it first, it is ~1 hour.**

---

## 0. The problem in one line

`USE_ALPACA_CLI=1` is set, the binary ships in the image, and the clock chain really
does resolve **CLI → REST → local calendar**. But I grepped the entire mobile app for
`market_open_source` / `marketOpenSource` and got **zero hits**. The eligibility
artifact works and is invisible.

A judge cannot score what they cannot see.

---

## 1. What is verified (2026-08-31)

- `alpaca version` → `0.0.14`, exit 0. (`alpaca --version` exits 1 with an auth error —
  the binary sets no root-command Version. Do not use the flag form.)
- `alpaca clock` with `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` in env returns:
  ```json
  {"is_open": false, "next_close": "…", "next_open": "…", "timestamp": "…"}
  ```
- The CLI reads **our exact env var names** — no `APCA_` remapping needed.
- `ScanResult.market_open_source` exists and is plumbed through
  `scanner_status.py:96` → `schemas/scanner.py:56` (`market_open_source: str | None`).
- **Nothing in `apps/mobile/src` reads it.**
- `packages/engine/engine/features/alpaca_cli.py` already has the subprocess
  discipline: argv list, no `shell=True`, `proc.kill()` + `await proc.wait()` on
  timeout, returns `None` on any failure.

---

## 2. Build, cheapest first

### 2.1 Show the clock source in System Health (~30 min)

Settings already has a "SYSTEM HEALTH" card listing Council / Approvals / Broker /
Reconciler. Add a row:

```
MARKET CLOCK
Alpaca CLI · next open 09:30 ET
```

Read `marketOpenSource` off the existing `/api/v1/scanner/status` payload. Map:

| value | label |
|---|---|
| `alpaca_cli` | **Alpaca CLI** (green) |
| `alpaca_rest` | Alpaca REST `/v2/clock` (green) |
| `local_calendar` | Local holiday table (amber — a fallback, say so) |

**This one row is the screenshot that answers the eligibility question.** Everything
below is upside.

### 2.2 A read-only CLI diagnostics panel (~2h)

`GET /api/v1/ops/alpaca-cli` → runs `alpaca clock` (and optionally
`alpaca account get`) and returns **the raw JSON the binary printed**, rendered
verbatim in the UI under a heading like *"Output of `alpaca clock`, executed on the
API host."*

Seeing Alpaca's own tool output inside our product is far more convincing than a
string field, and it is the difference between "we claim we use it" and "here it is
running."

> 🚨 **Security rules for this endpoint, all non-negotiable:**
> - **Fixed argv only. No user input reaches the command line, ever.** Not a symbol,
>   not a flag, not a subcommand name. A hardcoded allowlist of two or three
>   subcommands, selected by an enum, is the entire surface.
> - **Read-only subcommands only** — `clock`, `account get`. Never `order`,
>   never `position`.
> - **Never `shell=True`.** Reuse `alpaca_cli.py`'s existing pattern.
> - `require_real_auth` — a read-only demo session must not be able to spawn
>   processes on the API host.
> - Timeout + `proc.kill()` + `await proc.wait()`, same as `cli_clock`.

### 2.3 Record it in the video (~10 min)

Terminal: `alpaca version`, `alpaca clock`, `alpaca account get` against the
submitted account. Ten seconds, unambiguous, zero code.

---

## 3. Tests

| Test | Break this to make it fail |
|---|---|
| `test_scanner_status_exposes_market_open_source` | Drop the field from the response |
| `test_ops_endpoint_rejects_a_demo_session` | Swap `require_real_auth` for `get_current_user` |
| `test_ops_endpoint_uses_a_fixed_argv` | Interpolate a caller-supplied string |
| `test_ops_endpoint_returns_null_when_the_binary_is_missing` | Let `FileNotFoundError` escape |

**Baseline: 969 passed, 11 skipped** (Python) + 28 (jest).

---

## 4. Where you will go wrong

1. **Using `alpaca --version`.** Exits 1 with `{"error":"authentication required"}`,
   which reads like a secrets problem and is a wrong-verb problem. It has already
   broken every build once.
2. **Letting any caller input reach the subprocess.** Fixed argv, enum-selected.
3. **Believing this replaces `AlpacaClock`.** The REST clock is the functional
   fallback; the CLI is the eligibility artifact. Both stay.
4. **Rendering `local_calendar` as green.** It is the degraded path — amber, and say
   what it means.

---

*Related: [`PLAN_MCP_DEMO.md`](PLAN_MCP_DEMO.md) · [`PLAN_OPTIONS_AGENTS.md`](PLAN_OPTIONS_AGENTS.md) ·
[`PLAN_ALPACA_MCP.md`](PLAN_ALPACA_MCP.md) (the original integration plan)*
