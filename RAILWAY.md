# Deploying to Railway + running the mobile app

Step-by-step. ~10 minutes end-to-end if you've used Railway before.

## What you'll have at the end

  - The **FastAPI** at `https://<your-service>.railway.app` (auto-TLS).
  - A **managed Postgres** with all 7 Alembic migrations applied.
  - The **Expo mobile app** running on your phone, pointed at the
    Railway URL, ready to log in via magic-link.
  - The **agent council in MOCK mode** by default. Adding
    `ANTHROPIC_API_KEY` flips it to real Claude calls — see §6.

---

## 1. Prerequisites

  - Railway account: <https://railway.app>
  - This repo pushed to GitHub (Railway connects via GitHub).
  - Node.js 20+ + `pnpm` 9+ locally (for Expo).
  - Expo Go app on your phone (iOS App Store or Google Play).

The Python deps (`cryptography`, `alpaca-py`, `anthropic`, etc.) install
automatically inside the Docker image — you don't need them locally to
deploy.

## 2. Create the Railway project

  1. Railway dashboard → **New Project** → **Deploy from GitHub** →
     pick this repo.
  2. Railway sees `railway.toml` + `apps/api/Dockerfile` and starts
     building. First build takes ~3-4 minutes.

## 3. Add Postgres

  1. In the project view → **+ New** → **Database** → **Add Postgres**.
  2. Railway auto-creates a `DATABASE_URL` env var. The API auto-converts
     `postgresql://` → `postgresql+asyncpg://` at startup; no
     post-processing needed.

## 4. Set the required env vars

Open the API service → **Variables** → paste these. Marked **(generate
fresh)** values are sensitive secrets — generate locally first:

```bash
# Generate JWT signing secret (48 url-safe bytes)
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Generate Fernet master key for broker-token encryption
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Required vars:

| Variable | Value |
|---|---|
| `ENV` | `production` |
| `USE_POSTGRES` | `1` |
| `DEV_AUTH_BYPASS` | `0` |
| `JWT_SECRET` | **(generate fresh)** |
| `BROKER_TOKEN_ENCRYPTION_KEY` | **(generate fresh, base64 Fernet key)** |
| `CORS_ORIGINS` | `exp://exp.host,https://exp.host` (Expo Go uses these origins; add your custom domain later) |

Optional but recommended:

| Variable | Why |
|---|---|
| `ANTHROPIC_API_KEY` | Real Claude calls. Without it the council runs in MOCK mode (canned responses). |
| `ALPACA_OAUTH_CLIENT_ID` + `ALPACA_OAUTH_CLIENT_SECRET` | Real broker OAuth. Without these the mobile "Connect Alpaca" button surfaces an Alpaca error after the redirect. |
| `KITE_API_KEY` + `KITE_API_SECRET` | Zerodha (Kite Connect). Without these the Zerodha connect routes return 503. See §7b. |
| `LIVE_TRADING_ENABLED` | Default off. Required for any non-paper order (Alpaca live + all Zerodha). The executor blocks with `live_trading_disabled` otherwise. |
| `LLM_COST_WARN_USD` | Defaults to `25.00`. Bump for higher-throughput watchlists. |

Hit **Deploy**. The Dockerfile + `start.sh` will:
  1. Build the image.
  2. Run `alembic upgrade head` against `DATABASE_URL`.
  3. Launch uvicorn on `0.0.0.0:$PORT`.
  4. Railway's healthcheck hits `/health` → 200 once boot finishes.

## 5. Verify the deploy

From your laptop:

```bash
# Replace with your Railway URL
RAILWAY_URL=https://your-app.railway.app

# Liveness — should return {"status": "ok", "env": "production", ...}
curl $RAILWAY_URL/health

# Auth: request a magic-link. In prod, devToken is NOT returned (you'd
# need an email service wired). For now grab the token from Railway's
# logs — search for "magic-link issued for ...".
curl -X POST $RAILWAY_URL/api/v1/auth/request-login \
    -H 'content-type: application/json' \
    -d '{"email":"you@example.com"}'
```

If `/health` returns `503` for several minutes, check **Logs**:
  - `alembic upgrade head` failure → Postgres connection issue
  - `ImportError` → a dep version drift; rebuild with `--no-cache`
  - `LookupError: bcrypt` → rare; Railway's libcrypt mismatch — add
    `apt-get install -y libcrypto3` to the Dockerfile and re-deploy.

## 6. Wire ANTHROPIC_API_KEY (when ready)

Once you've smoke-tested the chain in MOCK mode, add a real Anthropic key:

  - Railway → Variables → `ANTHROPIC_API_KEY = sk-ant-...`
  - Redeploy.
  - Open the mobile app → Approvals → tap "Run council".
  - The LLM cost pill on Home should flip from `Mock-only` to a small
    real-spend label.

A 10-name daily cron is roughly $1-2/month at Anthropic's prices. The
default `LLM_COST_WARN_USD=25` cap is intentionally generous.

## 7. Wire Alpaca OAuth (when ready)

Sign up + create an OAuth app at <https://app.alpaca.markets>:

  - Set the redirect URI to: `autotrader://broker/callback`
  - Add `ALPACA_OAUTH_CLIENT_ID` + `ALPACA_OAUTH_CLIENT_SECRET` to Railway.
  - Redeploy.
  - In mobile → Settings → "Connect Alpaca paper" → grant access.

Until then the executor route returns 412 ("connect Alpaca first") and
the mobile shows the Connect button.

## 7b. Wire Zerodha / Kite Connect (when ready)

Create a Kite Connect app at <https://developers.kite.trade> (₹2000/month):

  - Set the app's **Redirect URL** to:
    `https://<your-app>.railway.app/api/v1/broker/connect/zerodha/redirect`
  - Add `KITE_API_KEY` + `KITE_API_SECRET` to Railway. Redeploy.
  - In mobile → Settings → "Connect Zerodha" → log in at Kite in the
    browser → the redirect page shows "Zerodha connected ✓".
  - Kite access tokens expire daily ~06:00 IST — reconnect each trading day.
  - Zerodha is live-only (no paper env). Orders stay blocked until you also
    set `LIVE_TRADING_ENABLED=1` — flip it deliberately; real money.

---

## 8. Run the mobile app against Railway

```bash
# In the repo root
pnpm install

# Tell Expo to point at Railway. Vital: this is read at bundle time, NOT
# at runtime — save the .env BEFORE `expo start`.
echo "EXPO_PUBLIC_API_URL=https://your-app.railway.app" > apps/mobile/.env

# Start the dev server
pnpm --filter @app/mobile dev
```

Then in Expo Go (your phone):
  1. Scan the QR from the terminal.
  2. The app loads + lands on the login screen (no Bearer token yet).
  3. Enter your email → "Send login link" → grab the magic-link token
     from the Railway logs (the deploy is in prod mode, so `devToken`
     isn't surfaced in the response).
  4. Open `autotrader://auth/verify?email=you@example.com&token=<paste>`
     via your phone's URL bar OR build a tiny redirect for yourself.

For a smoother dev loop, set `ENV=local` on Railway temporarily —
`devToken` will land in the `/auth/request-login` response so you can
deep-link directly. Flip back to `production` before sharing the URL.

---

## 9. The 10-second post-deploy smoke

After the app loads + you've signed in:

  - **Home** → top strip shows COUNCIL/APPROVALS/BROKER/RECONCILER + LLM COST.
    On a fresh deploy everything except RECONCILER should be `ok` or
    `warning`. RECONCILER reports `unknown` until you've set
    `RECONCILER_ENABLED=1` (see `.env.example`).
  - **Approvals** → empty. Tap **Run council** → a proposal appears.
  - **Strategies** → 5 rows, all at confidence=0.50 (cold start).
  - **Settings** → "Connect Alpaca paper" button visible.
  - **Review** → "Nothing to review yet" empty state.

If all of those render, the deploy is healthy.

---

## 10. Schedule the daily cron (optional, for Phase 4 paper-trading)

GitHub Actions is the easiest path — it doesn't add Railway cost:

`.github/workflows/daily_council.yml`:

```yaml
on:
  schedule:
    - cron: '15 13 * * 1-5'   # 13:15 UTC = 09:15 EST market open
    - cron: '30 21 * * 1-5'   # 21:30 UTC = post-close reflection
jobs:
  council:
    if: github.event.schedule == '15 13 * * 1-5'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv pip install --system 'httpx' 'anthropic>=0.39.0' 'sqlalchemy[asyncio]' 'asyncpg' 'pydantic>=2.9.0' 'pydantic-settings>=2.6.0'
      - run: |
          PYTHONPATH=apps/agents:packages/engine:packages/broker:apps/api \
          python apps/agents/scripts/daily_cron.py
        env:
          USE_POSTGRES: '1'
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          AGENT_CRON_WATCHLIST: 'SPY,QQQ,AAPL,NVDA,MSFT,GOOG,AMZN,META,TSLA,JPM'
  reflection:
    if: github.event.schedule == '30 21 * * 1-5'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv pip install --system 'anthropic>=0.39.0' 'sqlalchemy[asyncio]' 'asyncpg' 'pydantic>=2.9.0' 'pydantic-settings>=2.6.0'
      - run: |
          PYTHONPATH=apps/agents:packages/engine:packages/broker \
          python -m trading_agents.reflection_cli --since 24h --no-seed
        env:
          USE_POSTGRES: '1'
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Copy `DATABASE_URL` from Railway → GitHub repo Settings → Secrets.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `503` on `/api/v1/broker/connect/alpaca/start` | `cryptography` not installed | Should never happen with the provided Dockerfile. Re-deploy. |
| `412` on `/api/v1/orders/execute/...` | User hasn't connected Alpaca yet | Settings → Connect Alpaca paper |
| Mobile shows "Couldn't reach the agent server" | `EXPO_PUBLIC_API_URL` not set OR wrong | Edit `apps/mobile/.env`, restart Expo |
| Magic-link `devToken` not in response | `ENV=production` (correct prod behavior) | Set `ENV=local` temporarily for dev loop |
| `alembic: command not found` in container | Image built without the deps stage | `docker build --no-cache` |
| Healthcheck timing out | Postgres connection failure → app exits before binding | Check `DATABASE_URL` is set + the Postgres plugin is wired |
| LLM cost pill stays `unknown` | No real calls have happened | `ANTHROPIC_API_KEY` set + run the council from the mobile app |

For anything not on the list: Railway → **Logs** is the source of
truth. The API logs every magic-link issuance, broker connection,
council pass, and order placement.

---

## 12. What's NOT yet wired (planned)

  - **Email delivery** for magic-link tokens — Phase 3.2 follow-on.
    Use `ENV=local` temporarily OR pull from logs in production.
  - **Rate limiting** on `/auth/request-login` — Phase 4
    prod-readiness round. Until then, magic-link spam is theoretically
    possible (though hash-only storage limits the blast radius).
  - **Redis-backed OAuth state cache** — needed only if you scale past
    one uvicorn worker. Single-worker mode (the default) works fine.

See `AGENTV1.md` for the running playbook + AGENTV1's open-options
section for the prioritized backlog.
