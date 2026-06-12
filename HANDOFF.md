# Handoff — what you do next

Everything code-side is done. The system is wired end-to-end. Below is
the exact list of **manual steps** you take to get a running Railway
deploy + Expo Go session. Items are ordered by what blocks what.

When you finish a section, ping me and I'll continue with whatever's
next on the playbook (currently: prod-readiness hardening — rate
limiting + Redis OAuth state).

---

## 0. What changed since this handoff was written (auto-mode + real data + Langfuse)

The `main` line now closes the auto-trade loop, runs on **real market
data**, and traces every agent in **Langfuse**. That adds a few manual
steps on top of the Railway recipe below. Full running history is in the
**Build log** at the bottom of [`fable5findings.md`](fable5findings.md) —
read that first to see what's done vs open.

### 0a. New env vars (set in Railway → Variables, alongside §1e)

```
# Real market data — features + ghost marks (DATA keys, NOT the OAuth client pair)
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
FRED_API_KEY=...                 # free: https://fredaccount.stlouisfed.org/apikeys (VIX/10y/dollar)

# Hard guards — make prod FAIL instead of silently running mock/synthetic
AGENTS_REQUIRE_REAL_LLM=1
AGENTS_REQUIRE_REAL_DATA=1

# Langfuse — per-agent council tracing (both keys required; absent => no-op)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com    # or https://us.cloud.langfuse.com / self-hosted
```

`.env.example` ([apps/api/.env.example](apps/api/.env.example)) has the full list with notes.

### 0b. Langfuse project (5 min)

1. <https://cloud.langfuse.com> → create a project.
2. Settings → API Keys → copy the public + secret keys into 0a.
3. After the next council run, the Langfuse **Traces** view shows one
   `council:<SYMBOL>` trace with a row per agent (router → … → drafter),
   each labelled DEFAULT (ok) / WARNING (parse-retry) / ERROR (unusable).
   That's the "where do agents fail or succeed" view. No keys → no-op, the
   council runs identically.

### 0c. Migration 0009 runs automatically

`start.sh` applies Alembic to head, so `0009_position_lifecycle`
(`agent_decisions.exit_mode/closed_at/close_reason` + `user_watchlist`)
lands on deploy. Nothing manual — just don't be surprised by the new
columns/table.

### 0d. The reconciler is now a per-user fleet — keep it running

Agent-managed exits depend on it. The reconciler fleet (replaces the old
fixture-user mock loop) does, per connected user, every tick: real-equity
snapshot + drawdown breaker, order/fill sync (+ detects closes you made
directly in the Alpaca app), and the **position manager** (time-stops +
council-SELL early exits for `exit_mode=agent` positions). It only acts
for users with a live Alpaca connection; the mock fallback is OFF in
production. So: time-stops and agent closes only happen while the API
process (with `USE_POSTGRES=1`) is up. If you run the API on Railway
24/5, you're covered; don't expect agent exits if the service is asleep.

### 0e. Auto-mode is per-position, entries stay manual

On each approval the user picks **who closes it**: `agent` (bracket
stop/target at the broker + time-stop/early-exit via the worker) or
`manual` (we only watch). Entries are always human-approved — there is no
auto-entry. A cron proposal now pushes a notification and expires at
end of market day (not 15 min).

### 0f. Still open (added to §7 of the handoff)

- **Sentry** — declared but unwired; needs a DSN. Langfuse covers LLM
  observability; Sentry is for API exceptions.
- **CLAUDE.md / PLAN.md drift** — they still say Zerodha is out-of-v1
  (it's built) and that LLM goes via a LiteLLM proxy (it's the Anthropic
  SDK directly). Reconcile when you get to it.
- **wash-sale on the Postgres path** — still informational-only / silent.

---

## Audit summary — broker / plugin / scheme alignment (already verified)

I checked the chain end-to-end before writing this. Findings:

| Area | Status | Notes |
|---|---|---|
| `BrokerInterface` Protocol | ✓ clean | `@runtime_checkable`, 7 methods, `name`/`is_paper` attributes. Zerodha v2 drops in without restructuring. |
| Alpaca scheme alignment | ✓ matches | `apps/mobile/app.json` scheme=`autotrader` ↔ `ALPACA_OAUTH_REDIRECT_URI=autotrader://broker/callback` ↔ DeepLinkHandler matches `broker/callback` path. |
| `is_paper` propagation | ✓ end-to-end | `broker_connections.is_paper` → `AlpacaBroker(paper=...)` → `OrderResponse.isPaper`. Live trading flip is one column + one env override away — explicitly gated on Phase 4 closing. |
| OAuth same-user check | ✓ enforced | Callback refuses if `state`'s stashed `user_id` doesn't match the caller. Alice can't redeem Bob's flow. |
| Token decrypt-on-use | ✓ bounded | `with_alpaca_client()` decrypts → yields → references dropped on exit. Audit log masks token (`PK12…XYZ7`). |
| Executor risk re-eval | ✓ | Fresh `RiskContext` built from broker's live `get_account_equity` / `get_buying_power` / `list_positions` before each order. |
| LLM cost ledger | ✓ writes every call | Mock + real, best-effort + swallowed. `/health/full`'s LLM COST pill reads it. |
| Push notification setup | ⚠ partial | `expo-notifications` declared + plugin block in `app.json`. EAS `projectId` is NOT set — fine for Expo Go dev, blocks EAS standalone builds. See §3 below. |
| Zerodha (India) | ⏸ v2+ scope | PLAN.md §1.4 explicitly defers. Not adding now. |

**TL;DR: the code is ready. The remaining work is config + credentials.**

---

## 1. Railway deploy (15 minutes, ~$5/month)

The full step-by-step lives in [`RAILWAY.md`](RAILWAY.md). Short version:

### 1a. Push the branch to GitHub

```bash
git push origin agent-v1
```

If `agent-v1` isn't your default branch on GitHub, that's fine —
Railway connects to whatever branch you point it at.

### 1b. Create the Railway project

  1. <https://railway.app> → **New Project** → **Deploy from GitHub**.
  2. Pick this repo. Pick the `agent-v1` branch.
  3. Railway sees `railway.toml` + `apps/api/Dockerfile` and starts
     building. First build: ~3–4 minutes.

### 1c. Add Postgres

  1. Project view → **+ New** → **Database** → **Add Postgres**.
  2. `DATABASE_URL` appears in the API service's env automatically.

### 1d. Generate secrets

Locally:

```bash
# JWT signing key (48 url-safe bytes)
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Fernet master key for broker-token encryption (32 bytes b64)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep these somewhere you'll find them again (1Password / Doppler /
Bitwarden — NOT in a .env you commit).

### 1e. Set Railway env vars

In the API service → **Variables** → paste:

```
ENV=production
USE_POSTGRES=1
DEV_AUTH_BYPASS=0
JWT_SECRET=<from step 1d>
BROKER_TOKEN_ENCRYPTION_KEY=<from step 1d>
CORS_ORIGINS=exp://exp.host,https://exp.host
```

The CORS line above lets Expo Go + the published Expo URL hit the API.
When you ship a custom domain, append it: `,https://app.yourdomain.com`.

### 1f. (Optional but recommended) Wire ANTHROPIC

Without `ANTHROPIC_API_KEY`, the council runs in MOCK mode — canned
responses. Useful for first-launch sanity testing. To flip to real
Claude calls:

  1. <https://console.anthropic.com> → API Keys → create one.
  2. Add to Railway: `ANTHROPIC_API_KEY=sk-ant-...`
  3. Redeploy. The LLM COST pill on Home flips from "Mock-only" to a
     small real-spend label.

Budget envelope: a 10-symbol daily watchlist runs ~$1–2/month at
Anthropic's current prices. The default `LLM_COST_WARN_USD=25.00` cap
is intentionally generous.

### 1g. (Optional) Wire Alpaca OAuth

Without these, the mobile "Connect Alpaca" button gets a 401 from
Alpaca on the redirect. Until you wire it, the system runs without a
broker connection (mobile shows the Connect button + the executor route
returns 412 if you try to execute).

  1. <https://app.alpaca.markets> → **OAuth Apps** → create one.
  2. Redirect URI: `autotrader://broker/callback` (exactly this — it
     matches `apps/mobile/app.json`'s scheme).
  3. Add to Railway:
     ```
     ALPACA_OAUTH_CLIENT_ID=<from Alpaca dashboard>
     ALPACA_OAUTH_CLIENT_SECRET=<from Alpaca dashboard>
     ```
  4. Redeploy.

### 1h. Deploy + verify

Click **Deploy** (or push to GitHub if you set auto-deploys).

Watch the build logs. You should see:

```
[start.sh] Running Alembic migrations against DATABASE_URL
INFO  [alembic.runtime.migration] Running upgrade -> 0001_initial_schema
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002_positions_snapshot
...
INFO  [alembic.runtime.migration] Running upgrade 0006 -> 0007_llm_calls
[start.sh] Launching uvicorn on 0.0.0.0:8080
INFO:     Uvicorn running on http://0.0.0.0:8080
INFO:     Application startup complete.
```

If you see `CORS LOCKOUT` in the logs, you forgot step 1e's
`CORS_ORIGINS`. Fix + redeploy.

Then from your laptop:

```bash
curl https://<your-app>.railway.app/health
# Expected: {"status":"ok","env":"production","version":"0.0.1"}
```

If you get this, the API is live.

---

## 2. Mobile against Railway (5 minutes)

```bash
# In the repo root
pnpm install

# Point Expo at Railway — saved BEFORE expo start (this is read at
# bundle time, not at runtime).
echo "EXPO_PUBLIC_API_URL=https://<your-app>.railway.app" > apps/mobile/.env

# Start the Expo dev server
pnpm --filter @app/mobile dev
```

On your phone:

  1. Install **Expo Go** (iOS App Store / Google Play) if you don't have it.
  2. Scan the QR code from the terminal.
  3. App loads → lands on the login screen.

### Logging in (with `ENV=production` on Railway)

In production, `/auth/request-login` doesn't return the magic-link
token in the response (security — tokens go via email). Email delivery
isn't wired yet (it's a tracked follow-on). For now, the easiest path:

  **Option A — Temporarily set `ENV=local` for the first login:**
    1. Railway → API service → Variables → `ENV=local` → redeploy.
    2. Open mobile → enter your email → tap "Send login link".
    3. The response now includes `devToken` → mobile shows "Continue with dev token".
    4. Tap that → you're in.
    5. Railway → flip `ENV=production` → redeploy. Your session
       persists (refresh token in SecureStore is still valid).

  **Option B — Pull the token from Railway logs:**
    1. Mobile → enter email → tap "Send login link".
    2. Railway → **Logs** → search for `magic-link issued for`.
    3. The log line contains the raw token.
    4. Manually construct the deep-link:
       `autotrader://auth/verify?email=YOU@example.com&token=PASTE`
    5. Open that URL on your phone (Safari / Chrome / Messages with yourself).
       The OS routes it to the Expo Go app → verify screen → in.

Option A is faster. Option B is the prod-realistic flow you'll switch
to once email delivery lands.

---

## 3. Push notifications — only if you build a standalone app

If you're just running in Expo Go (development), push notifications
work without any extra setup. **Skip this section.**

If you eventually want to publish via EAS Build:

  1. Install EAS CLI: `npm install -g eas-cli`
  2. From `apps/mobile/`, run: `eas init`
  3. Pick / create an EAS project.
  4. EAS will add `extra.eas.projectId` to `app.json` automatically.
  5. Commit the diff.

The `usePushRegistration` hook already reads `Constants.expoConfig.extra.eas.projectId`
and passes it to `getExpoPushTokenAsync({ projectId })` when present.

---

## 4. Connect an Alpaca paper account (if you wired §1g)

  1. Mobile → **Settings** tab → "Connect Alpaca paper" button.
  2. System browser opens Alpaca's authorize page.
  3. Sign in with your Alpaca account (paper, not live).
  4. Grant the scopes (`account:write`, `trading`).
  5. Alpaca redirects to `autotrader://broker/callback?code=...&state=...`.
  6. Expo Go receives the deep link → DeepLinkHandler POSTs to
     `/api/v1/broker/connect/alpaca/callback` → server decrypts +
     persists the encrypted tokens.
  7. Mobile shows: "Alpaca paper · PA-XXXXXXXX" + Disconnect button.

If the redirect fails ("Couldn't reach the agent server"):
  - Confirm `EXPO_PUBLIC_API_URL` matches your Railway URL.
  - Confirm Railway env has `ALPACA_OAUTH_CLIENT_ID` + `ALPACA_OAUTH_CLIENT_SECRET`.
  - Check Railway logs for the actual error.

---

## 5. Post-deploy 10-second smoke

Once logged in, walk through every tab:

  - **Home** → top strip shows COUNCIL · APPROVALS · BROKER · RECONCILER
    + a LLM COST cell below. Fresh deploy: COUNCIL and BROKER should be
    `warning` (no run yet / no connection yet); RECONCILER is `unknown`
    (mock mode); LLM COST is `unknown` (no calls yet).
  - **Approvals** → empty. Tap "Run council" → a proposal appears within
    seconds. Tap to expand.
  - **Strategies** → 5 cards, all at confidence 0.50 (cold start), all
    showing "NO DECISIONS YET" chips.
  - **Review** → "Nothing to review yet" empty state.
  - **Settings** → Notifications card (toggle), Brokers (Connect Alpaca
    button OR active connection), Security (Face ID toggle), Account
    (your email + Sign out).

If all five tabs render without errors → **the deploy is healthy.** You
can start using the system.

---

## 6. Daily cadence (Phase 4 paper-trading)

Once you want the council running daily without manual taps:

  1. Set up a GitHub Actions cron from the snippet in
     [`RAILWAY.md`](RAILWAY.md) §10.
  2. Copy your Railway `DATABASE_URL` to GitHub repo Settings → Secrets
     as `DATABASE_URL`. Same for `ANTHROPIC_API_KEY`.
  3. Commit the workflow file. Cron starts on the next NYSE business
     day at market open (13:15 UTC) + post-close reflection (21:30 UTC).

Watch the first run in GitHub Actions → confirm decisions land in the
Strategies tab + the Home health strip's COUNCIL pill stays `ok`.

---

## 7. What's NOT yet wired (intentionally)

These are tracked in [`AGENTV1.md`](AGENTV1.md). Listed here so you don't
trip over them:

  - **Email delivery for magic-link** — Postmark / Resend integration.
    Until then: §2 Option B (pull from Railway logs) OR §2 Option A
    (temporarily `ENV=local`).
  - **Rate limiting on `/auth/request-login`** — 5/hour/email + 30/hour/IP.
    A small abuser can spam magic-link tokens until this lands.
  - **OAuth state cache → Redis** — required if you scale past one
    uvicorn worker. Default single-worker mode works fine.
  - **Live trading** — `is_paper=True` flows through every layer.
    PLAN.md §11 explicitly gates live capital on Phase 4 paper-validation
    closing (5–6 months of paper trading with founder + 2-3 users).
  - **Zerodha / India** — v2+ scope per PLAN.md §1.4. `BrokerInterface`
    is the contract; a Zerodha class drops in then.

---

## 8. If something breaks

| Symptom | Most likely cause | First thing to check |
|---|---|---|
| Railway build fails | Dep version drift | Railway logs → look for `Cannot find` or `Resolution failed`. Re-deploy with **Clear build cache**. |
| `/health` returns 503 for >2 min | Alembic migration failed | Railway logs → search `alembic` |
| `CORS LOCKOUT` in logs | Forgot `CORS_ORIGINS` env var | §1e |
| Mobile shows "Couldn't reach the agent server" | `EXPO_PUBLIC_API_URL` wrong / `apps/mobile/.env` not saved | Restart Expo dev server after `.env` edit |
| Magic-link `devToken` not in response | `ENV=production` (correct prod behavior) | §2 Option A |
| 412 on `/orders/execute/...` | No active broker connection | Settings → Connect Alpaca |
| 503 on `/broker/connect/alpaca/start` | `cryptography` not installed | Should never happen with the Dockerfile — re-deploy |
| LLM COST pill stays `unknown` | No real LLM calls yet | Set `ANTHROPIC_API_KEY` + run a council pass |

Railway → **Logs** is the source of truth. Every magic-link issuance,
broker connect, council pass, and order placement is logged.

---

## When you're done

Ping me. I'll continue with the **prod-readiness round** queued in
[`AGENTV1.md`](AGENTV1.md):

  - Rate-limiting on `/auth/request-login` (5/hour/email + 30/hour/IP).
  - OAuth `state` cache → Redis (multi-worker prod safety).
  - `make dev-api-multi` target to validate the OAuth-across-workers flow.

That session also lands the docs updates + tests.

Beyond that, the prioritized backlog is in [`AGENTV1.md`](AGENTV1.md) →
"Other open options" — Phase 4 month-1 review checkpoint, email
delivery for magic-link, Postgres adapters for the last two stores
(review + cost ledger), and so on.
