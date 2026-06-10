# Runbook — Phase 3 → Phase 4 hand-off

Operator-facing notes. Anything in here is a precondition or recovery
step that doesn't fit in code comments.

---

## Real Alpaca paper-trade smoke

### Preconditions

1. **Postgres running + migrated**

   ```bash
   make infra-up
   make migrate
   ```

   `infra-up` boots docker-compose with Postgres + TimescaleDB + Redis.
   `make migrate` applies Alembic head (all 5 migrations) to the local DB.

2. **API up with auth enforcement + Postgres**

   ```bash
   USE_POSTGRES=1 DEV_AUTH_BYPASS=0 make dev-api
   ```

   The default `dev-api` target already sets `DEV_AUTH_BYPASS=0` — explicit
   here for clarity. The smoke MUST hit a real-auth API.

3. **Alpaca paper account + API keys**

   Sign up at https://alpaca.markets, switch to the Paper Trading tab,
   create an API key pair. Export:

   ```bash
   export ALPACA_API_KEY=PK...
   export ALPACA_API_SECRET=...
   export ALPACA_BASE_URL=https://paper-api.alpaca.markets  # paper, NOT live
   ```

4. **Python deps actually installed**

   The smoke uses `cryptography` (for decrypt-on-use) and `alpaca-py`
   (for the SDK). Both are declared in `apps/api/pyproject.toml` and
   `packages/broker/pyproject.toml` respectively. If you haven't:

   ```bash
   uv sync
   ```

5. **Anthropic key (optional)**

   The council can run in mock mode if `ANTHROPIC_API_KEY` is unset —
   the smoke still works end-to-end with the mock LLM responses, just
   with canned proposals. Set the key for a "real" smoke.

### Running the smoke

```bash
export RUN_ALPACA_SMOKE=1
uv run python scripts/smoke_paper_trade.py --symbol AAPL
```

Optional flags:
- `--symbol` (default `AAPL`)
- `--base-url` (default `http://localhost:8000`)
- `--email` (default `smoke@local.dev`)

Expected output (compressed):

```
INFO — smoke target: http://localhost:8000 — symbol=AAPL — user=smoke@local.dev
INFO — logged in as smoke@local.dev (user_id=...)
INFO — broker/start succeeded — ...
INFO — proposal: BUY AAPL qty=21 @ stop=215.34 target=263.29
INFO — ORDER PLACED: broker_order_id=... symbol=AAPL side=BUY qty=21 status=accepted is_paper=True
INFO — smoke complete ✓
```

### Skipping OAuth in the smoke

The production app does Alpaca OAuth through `/broker/connect/alpaca/{start,callback}`.
The smoke can't script that grant flow (it requires a browser + 2FA).

Two ways to seed the connection so the smoke can proceed:

#### Option A — Use the env-key bypass (one-shot, smoke-only)

Run this once to insert an active broker connection for the smoke user:

```python
# in a Python REPL with the API venv:
import asyncio, os
from app.services.crypto import encrypt_for_storage
from app.services.broker_store import get_broker_store
from app.services.postgres_auth_store import PostgresAuthStore

async def seed():
    auth = PostgresAuthStore()
    user = await auth.upsert_user("smoke@local.dev", auth_method="env_key")
    store = get_broker_store()
    enc = encrypt_for_storage(os.environ["ALPACA_API_KEY"])
    # Note: env-key Alpaca auth isn't quite the same as OAuth tokens — for
    # the smoke we store the raw key as if it were an access token. The
    # executor's AlpacaBroker.from_oauth_token call will use it as a
    # Bearer token; Alpaca-py also supports api_key/secret auth via
    # from_env() if you'd rather wire the smoke that way.
    await store.upsert_connection(
        user_id=user.id, broker="alpaca", is_paper=True,
        account_number=None,
        encrypted_access_token=enc,
        encrypted_refresh_token=None,
        access_token_expires_at=None,
    )

asyncio.run(seed())
```

#### Option B — Complete the real OAuth flow once

Set up Alpaca OAuth client credentials, complete the flow in the mobile
app (or via Postman), then run the smoke. The connection persists across
restarts under Postgres.

### What the smoke validates

| Step | Validates |
|---|---|
| Login | Magic-link auth + JWT issuance + dev-token return path |
| `/broker/connections` | The user actually has a broker linked |
| `/agent/run` | Council runs end-to-end + appends a proposal |
| `/orders/execute/{id}` | Decrypt-on-use + risk re-eval + place_order + idempotency |

### What the smoke does NOT exercise

- **Fill polling.** The order is placed; we don't wait for it to fill.
  Phase 4 hardening adds the reconciler-driven fill loop.
- **Push notification delivery.** The fan-out fires (council route
  schedules a `proposal_pending` task), but the smoke runs without a
  mobile device registered.
- **Live trading.** Hardcoded paper. `is_paper=True` propagated through
  the entire chain. PLAN.md §11 gates live on Phase 4 paper-validation
  closing.

### Failure modes + recoveries

| Symptom | Cause | Fix |
|---|---|---|
| `503 — uv sync` | `cryptography` not installed | `uv sync` |
| `412 — connect Alpaca first` | No active broker connection | Run the seed script above |
| `404 — no pending proposal` | Council HOLD'd or proposal expired | Try another symbol; check `/approvals/pending` |
| `200 + riskBlocked=true` | Risk re-eval rejected (e.g. circuit-breaker tripped) | Check `riskVetoRule` field; address per-rule |
| `502 — broker call failed` | Alpaca returned 4xx/5xx | Check API key, check market hours (paper still respects them) |

---

## Phase 4 daily cadence

Once the smoke is green, the founder runs the chain daily. Two pieces
need to be scheduled:

### Daily council cron

Runs each NYSE business day at market open (≈9:15 EST = 13:15 UTC).
Decides on a small watchlist.

```bash
PYTHONPATH=apps/agents:packages/engine:packages/broker:apps/api \
USE_POSTGRES=1 \
uv run python -m apps.agents.scripts.daily_cron \
    --user-id 00000000-0000-0000-0000-000000000001 \
    --watchlist SPY,QQQ,AAPL,NVDA,MSFT,GOOG,AMZN,META,TSLA,JPM
```

The script is **idempotent** on `(user, UTC date, symbol)` — re-running
the same day is a no-op for symbols already decided. Use `--force` only
during smoke testing.

### Reflection cron

Runs EOD UTC (≈21:30 UTC = 17:30 EST, after market close + a buffer
for late fills).

```bash
PYTHONPATH=apps/agents:packages/engine:packages/broker \
USE_POSTGRES=1 \
uv run python -m trading_agents.reflection_cli --since 24h --no-seed
```

The Reflection Agent reads pending decisions (`reviewed_at IS NULL AND
realized_pnl IS NOT NULL`), grades per-strategy, writes clamped deltas
to `strategy_confidence`. The Selector picks them up on tomorrow's pass.

### Zerodha daily reconnect reminder

Kite access tokens are flushed ~06:00 IST every morning (no refresh
tokens), so a connected Zerodha user must re-login each trading day.
This cron pushes "reconnect before market open" to every user whose
stored token has expired:

```bash
PYTHONPATH=apps/api:apps/agents:packages/engine:packages/broker \
USE_POSTGRES=1 \
python apps/api/scripts/zerodha_reconnect_cron.py        # add --force to smoke
```

Schedule weekdays at **03:30 UTC = 09:00 IST** (NSE opens 09:15 IST):

```yaml
schedule:
  - cron: '30 3 * * 1-5'
```

Tapping the push opens the mobile Settings tab (payload
`kind: zerodha_reconnect`). Re-running the script by hand re-sends —
that's intended for manual nudges; the once-daily scheduler is the
idempotency boundary.

### Scheduling — GitHub Actions

`.github/workflows/daily_council.yml`:

```yaml
on:
  schedule:
    - cron: '15 13 * * 1-5'   # 13:15 UTC weekdays = market open
    - cron: '30 21 * * 1-5'   # 21:30 UTC weekdays = post-close reflection
jobs:
  council:
    if: github.event.schedule == '15 13 * * 1-5'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv sync
      - run: |
          PYTHONPATH=apps/agents:packages/engine:packages/broker:apps/api \
          uv run python -m apps.agents.scripts.daily_cron --watchlist "$WATCHLIST"
        env:
          USE_POSTGRES: '1'
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          WATCHLIST: 'SPY,QQQ,AAPL,NVDA,MSFT,GOOG,AMZN,META,TSLA,JPM'
  reflection:
    if: github.event.schedule == '30 21 * * 1-5'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv sync
      - run: |
          PYTHONPATH=apps/agents:packages/engine:packages/broker \
          uv run python -m trading_agents.reflection_cli --since 24h --no-seed
        env:
          USE_POSTGRES: '1'
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

### Scheduling — Fly machines

Fly's machine scheduler (`fly machines run --schedule ...`) is the
preferred alternative once we're past github-actions free-tier limits.
The command shape is the same; just swap the env into the machine config.

---

## Operator dashboard

`GET /api/v1/health/full` aggregates per-component liveness in one
call. The mobile Home screen renders this as a status strip; operators
also call it directly to check the chain.

| Component | Healthy state |
|---|---|
| `council` | Last run < 8h ago, ≥1 run in last 24h |
| `approvals` | Inbox clear OR oldest pending < 10m |
| `broker` | At least one active connection |
| `reconciler` | Last tick < 2m ago (only when `USE_POSTGRES=1`) |
| `llmCost` | Placeholder until LiteLLM ledger lands |

Anything `warning` or `danger` deserves operator attention. The
`reconciler=unknown` state is **expected** for mock-mode runs and is
not a problem.

---

## Other ops notes (placeholders for follow-ons)

### Database backups

TBD — Phase 4 prereq.

### Push notification troubleshooting

TBD — once we have a registered device.

### Live-trading flip

GATED on Phase 4 paper-validation closing (PLAN.md §11). Until then,
NO live trading. The `is_paper=True` flag MUST stay locked at every
layer that consumes it.

---

## Phase 4 daily review checklist

Once a few days of trades have closed, the operator runs through the
Review tab:

1. **Open the Review tab** on mobile.
2. **Swipe through each closed trade**:
   - Right (👍 good): the agent's call was right (regardless of PnL sign).
   - Left (✗ bad): the agent's call was wrong.
   - Up (↻ skip): too ambiguous to grade.
3. **Check the Home agreement strip** — if `agreement_pct` < 45%, the
   Reflection Agent is mis-calibrated. Investigate which strategy is
   drifting against your view.
4. **Watch the LLM-cost pill** — at the 30d $25 default cap (`LLM_COST_WARN_USD`),
   it flips to `warning`. Bump the env var if expected, or tighten the
   prompt if not.

### Tuning the cost alert

Set `LLM_COST_WARN_USD` to override the default $25/30d cap:

```bash
LLM_COST_WARN_USD=100 USE_POSTGRES=1 make dev-api
```

Council passes are roughly:
  - Mock mode: $0.00
  - Real (Anthropic) with cache warm: ~$0.005/pass — Selector (Haiku) +
    3 analysts + Drafter (Sonnet) + Risk Officer (no LLM).
  - Reflection cycles: ~$0.001 per strategy reviewed.

A 10-name daily watchlist × 22 NYSE business days ≈ 220 council passes/month
≈ ~$1.10/month at current prices. Cheap. The $25 cap is conservative
enough to catch a regression.

### Reading the calibration signal

| Agreement % | Meaning | Action |
|---|---|---|
| ≥ 65% | Reflection tracks you well | None — keep grading |
| 45–65% | OK but coarse | Look at buckets — which (grade, direction) cell has the most disagreement? |
| < 45% | Reflection is mis-calibrated | Pull the disagreeing decisions, look for a systematic blind spot (regime? sector? confidence threshold?) |

The buckets returned by `/api/v1/review/agreement` give per-cell counts
across `(operator_grade, reflection_direction)`. A `(bad, positive)`
cluster means Reflection nudged confidence UP on strategies the operator
thinks aged badly — the canonical reason to tighten the Reflection prompt.
