## apps/api auth — Phase 3 foundation

Magic-link login + HS256 access/refresh JWTs + per-device session rotation.
Mobile lands in a follow-on session; this is the API surface mobile will
call into.

### Flow

```
┌──────────────┐  POST /api/v1/auth/request-login {email}
│   Mobile     │ ─────────────────────────────────────────► ┌──────────────────────┐
│              │ ◄───────────────────────────────────────── │  AuthService         │
│              │   200 { expiresAt, devToken? }             │  · mints opaque tok  │
│              │   (devToken only in non-prod)              │  · hashes (scrypt)   │
│              │                                            │  · stores hash       │
│              │                                            └──────────────────────┘
│              │
│              │  POST /api/v1/auth/verify {email, token}
│              │ ─────────────────────────────────────────► ┌──────────────────────┐
│              │ ◄───────────────────────────────────────── │  · hash-match token  │
│              │   200 { accessToken, refreshToken, ... }   │  · single-use lock   │
│              │                                            │  · upsert user       │
│              │                                            │  · mint access (15m) │
│              │                                            │  · mint refresh (30d)│
│              │                                            │  · open session row  │
│              │                                            └──────────────────────┘
│              │
│              │  GET /api/v1/* + Authorization: Bearer <access>
│              │ ─────────────────────────────────────────► everything else
│              │
│              │  POST /api/v1/auth/refresh {refreshToken}
│              │ ─────────────────────────────────────────► rotates the pair
│              │
│              │  POST /api/v1/auth/logout {refreshToken?}
└──────────────┘ ─────────────────────────────────────────► revoke session
```

### What lives where

| File | Purpose |
|---|---|
| `app/services/jwt_service.py` | Stdlib HS256 JWT mint/verify + scrypt token hashing. Algorithm-confusion guard on the header. Swap to `python-jose` is one import-line change. |
| `app/services/auth_store.py` | `AuthStore` Protocol + `MockAuthStore`. Postgres impl wired against migrations 0001 + 0004 ships in a follow-on session. |
| `app/services/auth.py` | Magic-link request / verify / refresh / rotation logic. The orchestrator over the store + JWT primitives. |
| `app/middleware/auth.py` | `get_current_user` Depends + `require_real_auth` (no-bypass variant). |
| `app/routers/auth.py` | POST /request-login · POST /verify · POST /refresh · POST /logout · GET /me. |

### Routes

| Route | Auth | Notes |
|---|---|---|
| `GET  /health` | public | Liveness; never gated. |
| `POST /api/v1/auth/request-login` | public | Body: `{ email }`. Rate-limit lands in a follow-on. |
| `POST /api/v1/auth/verify` | public | Body: `{ email, token, deviceId?, deviceLabel? }`. |
| `POST /api/v1/auth/refresh` | refresh token | Rotates the pair. Replay → session revoked. |
| `POST /api/v1/auth/logout` | real auth | Refuses `DEV_AUTH_BYPASS`. |
| `GET  /api/v1/auth/me` | real auth | Refuses `DEV_AUTH_BYPASS`. |
| `GET  /api/v1/account` | bearer OR bypass | Existing route; DEV_AUTH_BYPASS keeps the mobile demo working. |
| `GET  /api/v1/activity` | bearer OR bypass | ″ |
| `GET  /api/v1/approvals/pending` | bearer OR bypass | ″ |
| `POST /api/v1/approvals/{id}/decision` | bearer OR bypass | ″ |
| `POST /api/v1/agent/run` | bearer OR bypass | Council pass; `user.id` is now passed into `run_council`. |

### Env

| Var | Default | Notes |
|---|---|---|
| `JWT_SECRET` | dev placeholder in `core/config.py` | Override in prod via Doppler. Must be ≥ 32 bytes high-entropy. |
| `ENV` | `local` | When `prod` / `production`, `request-login` stops returning `devToken`. |
| `DEV_AUTH_BYPASS` | `0` (off by default now) | When 1 + no Bearer header → request resolves to the fixture user. Defaults off as of Phase 3 mobile-auth round; flip to 1 only for the legacy pre-mobile-auth flow. |
| `USE_POSTGRES` | `0` | When 1, all stores (AuthStore, BrokerStore, NotificationStore, DecisionLog, StrategyConfidenceStore) use the Postgres adapters against the schema in `infra/migrations/`. Run `make infra-up && make migrate` first. |
| `DATABASE_URL` | (Postgres only) | `postgresql+asyncpg://user:pass@host:5432/db`. Read by `engine.db.session.async_session_factory`. |

### Token shape

```
HS256 JWT — base64url(header).base64url(payload).base64url(sig)
header  : {"alg":"HS256","typ":"JWT"}   ← byte-locked; no algorithm-confusion
payload :
  sub  : <user_id>
  iat  : <unix-seconds>
  exp  : <unix-seconds>
  typ  : "access" | "refresh"           ← discriminator; mismatch → 401
  jti  : <16-char URL-safe random>      ← uniqueness so back-to-back mints differ
  sid  : <session_id>                   ← refresh only; lets the store revoke
```

Access TTL: 15 minutes. Refresh TTL: 30 days. Rotation: every `/refresh` mints fresh access + fresh refresh; old refresh is invalidated by hash mismatch on the next call.

### Storage

Refresh tokens are **stored hashed** via scrypt (stdlib `hashlib.scrypt`,
`n=2^14, r=8, p=1, dklen=32`). The raw token never touches the DB.
Hash format: `scrypt$<salt>$<digest_b64u>`.

Magic-link tokens use the same scheme. `used_at` locks the row after first
verify so replays fail.

### DO NOT

- **Don't lower JWT_SECRET in prod**. The local default is intentionally
  obviously-dev. Doppler holds the prod secret.
- **Don't trust the JWT's self-declared `alg`**. `jwt_service.verify`
  compares the header byte-for-byte against the HS256 declaration — anything
  else (incl. `alg: none`) is rejected.
- **Don't use the same access token across devices**. Refresh per-device;
  rotating a refresh in one device doesn't kill the other.
- **Don't run lockfile commit or `uv sync` to pull `python-jose`/`passlib`**.
  Per the user's standing instruction — they'll do it themselves.
  `jwt_service` is stdlib-only on purpose so we don't block on a sync.
- **Don't store raw refresh tokens or raw magic-link tokens**. Hashed at rest.
- **Don't put auth checks in the routers' bodies**. `Depends(get_current_user)`
  / `Depends(require_real_auth)` is the contract.

### Follow-on sessions

1. **Rate limiting** on `/auth/request-login` (5/hour/email via `slowapi`).
2. **PostgresAuthStore** wired against the 0004 migration.
3. ~~**Mobile auth screens**~~ ✅ shipped — see `apps/mobile/README.md`.
4. ~~**Alpaca OAuth**~~ ✅ shipped — see "Broker OAuth flow" below.
5. ~~**Flip `DEV_AUTH_BYPASS=0`**~~ ✅ default-on `dev-api` Make target.
6. **PostgresBrokerStore** wired against `broker_connections` (migration 0001).
7. **OAuth state cache → Redis** so multi-worker prod doesn't lose state across workers.
8. **Token refresh scheduler** — periodic background task that calls
   `alpaca_oauth.refresh_broker_tokens` for connections with
   `access_token_expires_at` within a TTL of now.

---

## Broker OAuth flow

PLAN.md §3 — per-user OAuth so each connection holds its own credentials
(revoking a user invalidates their broker access). PKCE (RFC 7636) on
top of OAuth 2.0.

```
┌──────────────┐  POST /api/v1/broker/connect/alpaca/start  { isPaper }
│   Mobile     │ ─────────────────────────────────────────► ┌────────────────────────┐
│              │ ◄───────────────────────────────────────── │ Generate state (CSRF)  │
│              │   200 { authorizeUrl, state, expiresAt,    │ Generate code_verifier │
│              │         devWarning? }                      │ Derive code_challenge  │
│              │                                            │ Stash (state, user_id, │
│              │                                            │   code_verifier) in    │
│              │                                            │   PendingOAuthCache    │
│              │                                            │   (in-memory, 15-min)  │
│              │                                            └────────────────────────┘
│              │
│              │  Linking.openURL(authorizeUrl)
│              │ ─────────────────────────────────────────► system browser
│              │                                              user logs in @ Alpaca
│              │                                              user grants scopes
│              │                                              redirect: autotrader://
│              │                                                broker/callback?
│              │                                                code=...&state=...
│              │ ◄───────────────────────────────────────── DeepLinkHandler
│              │
│              │  POST /api/v1/broker/connect/alpaca/callback
│              │     { code, state }
│              │ ─────────────────────────────────────────► ┌────────────────────────┐
│              │ ◄───────────────────────────────────────── │ Verify state matches   │
│              │   200 { connection }                       │   stash + same user_id │
│              │                                            │ Single-use consume     │
│              │                                            │ POST /oauth/token to   │
│              │                                            │   Alpaca with the      │
│              │                                            │   stashed code_verifier│
│              │                                            │ Encrypt tokens         │
│              │                                            │   (Fernet)             │
│              │                                            │ Upsert broker_         │
│              │                                            │   connections row      │
│              │                                            └────────────────────────┘
└──────────────┘
```

### Routes

| Route | Auth | Notes |
|---|---|---|
| `POST /api/v1/broker/connect/alpaca/start` | real auth | Returns `authorizeUrl + state`. |
| `POST /api/v1/broker/connect/alpaca/callback` | real auth | Body `{ code, state }`. Single-use on state. |
| `GET  /api/v1/broker/connections` | real auth | Encrypted tokens NEVER returned. |
| `DELETE /api/v1/broker/connections/{id}` | real auth | 404 if id belongs to a different user. |

### Env

| Var | Default | Notes |
|---|---|---|
| `ALPACA_OAUTH_CLIENT_ID` | `DEV-ALPACA-CLIENT-ID` | Doppler in prod. |
| `ALPACA_OAUTH_CLIENT_SECRET` | `DEV-ALPACA-CLIENT-SECRET` | Doppler in prod. |
| `ALPACA_OAUTH_REDIRECT_URI` | `autotrader://broker/callback` | Mobile app's deep-link scheme. |
| `ALPACA_AUTHORIZE_URL` | `https://app.alpaca.markets/oauth/authorize` | Override for tests / sandbox splits. |
| `ALPACA_TOKEN_URL` | `https://api.alpaca.markets/oauth/token` | ″ |
| `BROKER_TOKEN_ENCRYPTION_KEY` | dev fallback (clearly-marked) | 32 bytes URL-safe base64. Generate via `Fernet.generate_key()`. |
| `BROKER_TOKEN_DECRYPTION_KEYS` | empty | Comma-separated extras for key rotation (MultiFernet). |

### Encryption

- `cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256 + URL-safe base64).
- Fresh IV per encrypt → repeated encryptions of the same plaintext produce
  different ciphertexts (no fingerprinting).
- HMAC catches tamper.
- `MultiFernet` for key rotation — set the new key as primary, keep the
  old in `BROKER_TOKEN_DECRYPTION_KEYS` until all rows are re-encrypted,
  then drop it.
- **The module imports `cryptography` lazily.** When the library isn't
  installed (declared in pyproject but un-synced), broker OAuth routes
  return a clean 503 pointing at `uv sync`. Mock paths that don't touch
  broker tokens keep working.

### DO NOT

- **Don't store broker tokens unencrypted.** Encryption helper or 503.
- **Don't ship without the `state` CSRF parameter on `/authorize`.**
- **Don't put `code_verifier` in a cookie or response.** Server-side stash only.
- **Don't reuse the dev encryption key in prod.** The `/start` response
  surfaces `devWarning` when the dev key is in play — visible to ops.

---

## Zerodha (Kite Connect) connect flow

Kite is **NOT OAuth** — no PKCE, no refresh tokens. The operator's
personal Kite Connect app (`KITE_API_KEY` + `KITE_API_SECRET`, app-level
env) pairs with a per-user **daily** access token.

```
1. POST /broker/connect/zerodha/start          (real auth)
     → stash (state, user_id) in PendingOAuthCache
     → 200 { loginUrl, state, expiresAt }
       loginUrl = kite.zerodha.com/connect/login?v=3&api_key=…
                  &redirect_params=state%3D<state>

2. User logs in at Kite (browser) → Zerodha redirects to the app's
   REGISTERED redirect URL with request_token=…&state=<state>.
   Register https://<api>/api/v1/broker/connect/zerodha/redirect
   in the Kite developer console for the browser flow.

3a. GET /broker/connect/zerodha/redirect?request_token&state   (no bearer)
      Browser landing. The single-use, 15-min, high-entropy state from
      the authed /start IS the proof of initiation. Renders a tiny
      "connected ✓" HTML page.

3b. POST /broker/connect/zerodha/callback { requestToken, state }  (real auth)
      Mobile path — same-user check enforced like the Alpaca callback.

Both completion paths: verify+consume state → exchange request_token
(sha256(api_key + request_token + api_secret) checksum) → encrypt the
access token (Fernet, same key as Alpaca) → upsert broker_connections
with broker='zerodha', is_paper=false.
```

### Routes

| Route | Auth | Notes |
|---|---|---|
| `POST /api/v1/broker/connect/zerodha/start` | real auth | 503 if `KITE_API_KEY`/`KITE_API_SECRET` unset. |
| `POST /api/v1/broker/connect/zerodha/callback` | real auth | Body `{ requestToken, state }`. Single-use state, same-user check. |
| `GET  /api/v1/broker/connect/zerodha/redirect` | state-only | Browser landing for the Kite redirect. HTML response. |

### Env

| Var | Default | Notes |
|---|---|---|
| `KITE_API_KEY` | unset (503) | From developers.kite.trade. App-level, not per-user. |
| `KITE_API_SECRET` | unset (503) | ″ |
| `KITE_DEFAULT_PRODUCT` | `CNC` | `MIS` for intraday; derivatives force `NRML`. |
| `KITE_API_BASE` / `KITE_LOGIN_BASE` | Kite prod URLs | Override for tests. |
| `BROKER_PREFERENCE` | `alpaca,zerodha` | Which active connection the executor picks first. |
| `LIVE_TRADING_ENABLED` | unset (off) | Required for any non-paper order (Alpaca live + all Zerodha). |

### Daily token expiry

Kite flushes access tokens ~06:00 IST every morning; there is no refresh
token. We store the computed expiry in `access_token_expires_at`;
`broker_use` checks it BEFORE decrypting and raises a clear
"reconnect Zerodha" error instead of a confusing Kite 403. **The user
re-runs the connect flow each trading day.**

### DO NOT

- **Don't store `KITE_API_SECRET` per-user.** It's app-level env, only
  ever used server-side for the exchange checksum.
- **Don't add a refresh path.** Kite doesn't have one — anything that
  looks like one is caching a dead token.
- **Don't bypass the live-trading gate.** All Zerodha connections are
  real money (`is_paper=false`); the executor refuses them unless
  `LIVE_TRADING_ENABLED=1`.

---

## Push notifications

Mobile registers device tokens; council `/agent/run` fans out a proposal-
pending push on every approved proposal. Hand-rolled Expo Push client —
no `expo-server-sdk-python`.

### Flow

```
┌──────────────┐   on launch (post-auth + biometric)
│   Mobile     │   Notifications.requestPermissionsAsync()
│              │   Notifications.getExpoPushTokenAsync()
│              │
│              │   POST /api/v1/notifications/register-device
│              │   { expoPushToken, platform, label }
│              │ ─────────────────────────────────────────► ┌────────────────────┐
│              │ ◄───────────────────────────────────────── │  Idempotent upsert │
│              │   200 { id, platform, label, ... }         │  on (user, token)  │
│              │                                            └────────────────────┘
│              │
│ ◄─────────── │   Council /agent/run produces a proposal
│              │   schedule_proposal_pending_notification(
│              │     user_id, proposal
│              │   ) — fire-and-forget asyncio.create_task
│              │                                            │
│              │                                  ┌─────────▼──────────┐
│              │                                  │ list_active_devices│
│              │                                  │ fan-out via Expo  │
│              │                                  │ Push (chunks 100) │
│              │                                  │ DeviceNotReg →    │
│              │                                  │   revoke_by_token │
│              │                                  └─────────┬─────────┘
│              │ ◄───────────────────────────────────────── │
│  push lands  │   foreground: in-app banner + tap
│              │   background: system banner + tap
│              │
│              │   tap → addNotificationResponseReceivedListener
│              │       → invalidate ['approvals']
│              │       → router.push('/approvals')
└──────────────┘
```

### Routes

| Route | Auth | Notes |
|---|---|---|
| `POST /api/v1/notifications/register-device` | real auth | Idempotent on `(user_id, expo_push_token)`. |
| `GET  /api/v1/notifications/devices` | real auth | Filters to the caller's devices. |
| `DELETE /api/v1/notifications/devices/{id}` | real auth | 404 if id belongs to a different user. |

### Schema (migration 0005)

`device_tokens(id, user_id, expo_push_token, platform, label, created_at, last_seen_at, revoked_at)`
+ `UQ (user_id, expo_push_token)` + partial index on `revoked_at IS NULL`
(the fan-out hot query).

### Council hook

`apps/api/app/routers/agent.py` calls `schedule_proposal_pending_notification`
**after** the proposal lands in the pending queue. The call is
fire-and-forget (`asyncio.create_task`) — the council route returns
immediately. The Expo Push client is fail-soft:

  - **Per-ticket `DeviceNotRegistered`** → mark the token revoked locally
    (the OS uninstalled the app or denied the channel).
  - **Network / 5xx from Expo** → log + swallow.
  - **One stale device never blocks the council route.**

### Body content

Notification bodies are LOCK-SCREEN-SAFE:

  - Title: `"New trade proposal"`
  - Body: `"BUY 21 NVDA — tap to review"`
  - Data: `{ kind: "proposal_pending" }` — drives the tap-routing in mobile.

NO broker tokens, NO proposal IDs, NO PII in the body or data.

### Env

| Var | Default | Notes |
|---|---|---|
| (none required) | | Expo Push doesn't need a server-side API key for the public push API. |

### DO NOT

- **Don't block the council route on the push fan-out.** `asyncio.create_task` + swallow.
- **Don't put proposal IDs / broker tokens in the notification body.** Lock-screen visible.
- **Don't add `expo-server-sdk-python`.** One POST endpoint, hand-rolled.
- **Don't poll the Expo "receipt" endpoint** in Phase 3. Mobile cares about OS receipt; carrier-receipt is Phase 4 hardening.

---

## Executor flow

Phase 3.5 — `POST /api/v1/orders/execute/{proposal_id}`. The mobile
Approve button calls this; we re-evaluate risk against the broker's
current view of the world, then place the order. This is the LAST line
of defense before a real (paper) order hits Alpaca.

### Flow

```
┌──────────────┐   POST /api/v1/orders/execute/{proposal_id}
│   Mobile     │ ─────────────────────────────────────────► ┌────────────────────────┐
│              │                                            │ 1. Resolve proposal in │
│              │                                            │    pending queue       │
│              │                                            │ 2. with_alpaca_client: │
│              │                                            │    decrypt access tok  │
│              │                                            │ 3. Build RiskContext   │
│              │                                            │    from BROKER (NOT    │
│              │                                            │    cached snapshot)    │
│              │                                            │ 4. evaluate() — first  │
│              │                                            │    veto wins           │
│              │                                            │ 5. AlpacaBroker        │
│              │                                            │    .place_order        │
│              │                                            │    (client_order_id =  │
│              │                                            │    agent-exec-<pid>)   │
│              │                                            │ 6. store.decide()      │
│              │                                            │    flips proposal to   │
│              │                                            │    'approved'          │
│              │ ◄───────────────────────────────────────── └────────────────────────┘
│              │   200 { order, riskBlocked: false }                                or
│              │   200 { order: null, riskBlocked: true, riskVetoRule, riskReason }
└──────────────┘
```

### Routes

| Route | Auth | Notes |
|---|---|---|
| `POST /api/v1/orders/execute/{id}` | `require_real_auth` | No bypass — executing trades requires a real session. |

### Trading mode (paper vs live)

``TRADING_MODE=paper`` (the DEFAULT) short-circuits the executor before
any broker is opened: risk re-eval runs against the user's simulated
paper book (``app/services/paper_broker.py``), the fill is immediate at
the proposal's limit/last price, and the paper portfolio (per market —
US book in USD, IN book in INR) tracks positions + realized P&L. The
paper book surfaces in ``GET /portfolio/summary`` as ``broker: paper``.
Going live is a TWO-key flip: ``TRADING_MODE=live`` AND
``LIVE_TRADING_ENABLED=1``.

### Auth chain

The executor uses the per-user token path, never env keys. The
decrypt-on-use helper (`app.services.broker_use.with_broker_client`):

1. Looks up the caller's active `broker_connections` row
   (`BROKER_PREFERENCE` env breaks ties when several are active).
2. Checks `access_token_expires_at` (Zerodha tokens die daily).
3. Decrypts `encrypted_access_token` via `app.services.crypto`.
4. Hands the plaintext to `AlpacaBroker` / `ZerodhaBroker`.
5. Drops references on exit so the plaintext is GC-eligible.

The plaintext access token NEVER touches the database, logs, or response
body. The audit log uses a masked form (`PK12…XYZ7`).

### Idempotency

`client_order_id = "agent-exec-<proposal_id>"`. Alpaca de-dupes on this
natively for ~24 hours; Zerodha has no native dedupe so the adapter
emulates it via the order `tag` within the trading day. Either way a
retry of `/orders/execute/{id}` with the same proposal_id lands on the
EXISTING order, not a duplicate. The first
successful execute also flips the proposal's `user_response` to
`approved` so a subsequent execute returns 404 (no longer pending).

### Risk re-evaluation

The proposal carried a risk decision at council time. Between then and
the user tapping Approve, anything could have changed:

  - Account equity crashed → drawdown halt now active.
  - Open positions grew → max_open_positions breached.
  - Same-day day-trade closed → PDT counter incremented.
  - A new wash-sale window opened.

We re-run `engine.risk.evaluate` against a FRESH `RiskContext` built
from the broker's `get_account_equity` + `get_buying_power` + `list_positions`.
If the verdict changed, we return 200 with `riskBlocked=True` and the
specific `veto_rule` — the order never goes to Alpaca.

### Failure surfaces

| HTTP | Meaning |
|---|---|
| 200 + `order` populated | Placed |
| 200 + `riskBlocked=true` | Risk re-eval rejected; no order placed |
| 401 | Bearer token missing or expired |
| 404 | Proposal not found or already executed |
| 412 | No active broker connection, or the Zerodha daily token expired — (re)connect a broker |
| 503 | `cryptography` or `alpaca-py` not installed — run `uv sync` |
| 502 | Broker call itself failed (broker 5xx, network error) |

### DO NOT

- **Don't skip the risk re-eval.** Proposals age. The reconciler can
  flip the breaker. The chain is the last gate before a real order.
- **Don't keep the decrypted token alive past the route call.** The
  context manager drops it on exit; don't reach around it.
- **Don't ship live trading silently.** `is_paper` flows from
  `broker_connections` through the executor's live gate: non-paper
  connections (Alpaca live + all Zerodha) are refused with the named
  rule `live_trading_disabled` unless `LIVE_TRADING_ENABLED=1` is set
  deliberately by the operator.
- **Don't log decrypted tokens.** Masked form only.

See `docs/RUNBOOK.md` for the end-to-end smoke + operator preconditions.

---

## Review flow — Phase 4 month-1 hand-grading

The operator (founder + 2–3 trusted users) grades the agent's completed
trades. The agreement stat — operator's grade vs Reflection's confidence
drift — is the calibration signal for the Reflection Agent.

### Routes

| Route | Auth | Notes |
|---|---|---|
| `GET  /api/v1/review/queue?windowDays=30` | `get_current_user` | Completed (`realized_pnl IS NOT NULL`) decisions the caller hasn't graded yet. |
| `POST /api/v1/review/{decision_id}` | `get_current_user` | Body `{ grade: "good"\|"bad"\|"skip", notes? }`. Idempotent upsert on `(decision_id, operator)`. 404 on unknown OR still-open decision. |
| `GET  /api/v1/review/agreement?windowDays=30` | `get_current_user` | Bucket stats + `agreement_pct` between operator grade and strategy-confidence drift direction. `skip` excluded from denominator. |

Routes use `get_current_user` (not `require_real_auth`) so the operator
sees an empty queue under `DEV_AUTH_BYPASS=1` instead of a 401.

### Agreement math

Agreement is computed by mapping:
  - `good` ↔ `positive` direction (Reflection nudged this strategy's
    confidence UP)
  - `bad`  ↔ `negative` direction (nudged DOWN)
  - `skip` excluded from the denominator

`positive`/`negative` threshold is ±0.02 around the 0.5 cold-start
prior. Drift between -0.02 and +0.02 is `neutral` and counts against
agreement (the operator graded but Reflection saw nothing to nudge).

### Schema (migration 0006)

```
decision_review (
  id UUID PRIMARY KEY,
  decision_id UUID FK agent_decisions ON DELETE CASCADE,
  operator_user_id UUID FK users ON DELETE CASCADE,
  grade VARCHAR(8) NOT NULL,
  notes TEXT,
  reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (decision_id, operator_user_id)
)
```

The UQ makes the upsert path idempotent. A single operator gets one
grade per decision; re-grading overwrites.

---

## LLM cost ledger

Every call through `trading_agents.llm.LLM.complete` writes one row.
The `/health/full` endpoint sums YTD; budget caps + per-role optimization
read the same table.

### Wiring

`trading_agents.cost_ledger.get_cost_ledger()` is a process singleton.
The LLM wrapper calls `record(...)` after each successful completion
(real OR mock). The write is **best-effort + swallowed** — telemetry
must never break the council.

### Pricing

Per-million-token USD, hardcoded in `cost_ledger.py::_PRICES`. Update
when Anthropic revs prices — no migration needed since the schema
stores absolute USD.

```
                        input/M    output/M   cache-read/M   cache-create/M
claude-haiku-4-5         $1.00      $5.00      $0.10          $1.25
claude-sonnet-4-6        $3.00     $15.00      $0.30          $3.75
claude-opus-4-7         $15.00     $75.00      $1.50         $18.75
```

Unknown models fall back to Sonnet pricing with a WARNING log.

### Mock vs real

Mock-LLM responses (`is_mock=True`) get a row but cost = $0.00 (no
token counts to charge). `sum_cost_since(exclude_mock=True)` is the
YTD-spend default — keeps mock dev runs out of the budget.

### Health-strip surface

`_llm_cost_health()` in `app.services.health` reads the ledger:

  - 0 real + 0 mock calls in 30d → `unknown` ("No LLM calls in last 30d")
  - 0 real + ≥1 mock                  → `ok` ("Mock-only, $0.00 spend")
  - ≥1 real, under cap                → `ok` ("30d spend $X.XX across N calls")
  - ≥1 real, at-or-over cap           → `warning`

Cap is `LLM_COST_WARN_USD` env, default $25 / 30 days.

### Schema (migration 0007)

```
llm_calls (
  id UUID PRIMARY KEY,
  agent_decision_id UUID FK agent_decisions ON DELETE SET NULL,
  user_id UUID FK users ON DELETE SET NULL,
  model VARCHAR(64) NOT NULL,
  role VARCHAR(32) NOT NULL,     -- router|technical|fundamental|macro|selector|drafter|reflection
  input_tokens INT NOT NULL DEFAULT 0,
  output_tokens INT NOT NULL DEFAULT 0,
  cache_read_tokens INT NOT NULL DEFAULT 0,
  cache_creation_tokens INT NOT NULL DEFAULT 0,
  cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
  is_mock BOOLEAN NOT NULL DEFAULT false,
  called_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

Partial index on `is_mock = false` powers the YTD-spend hot query.

### DO NOT

- **Don't fail the council on ledger writes.** The `_record_to_ledger`
  helper in `llm.py` try/excepts + logs. Telemetry must be optional.
- **Don't expose per-call rows on a public API.** Aggregates only;
  per-call detail lives in audit log queries.
- **Don't reuse a real-call row as a mock.** The `is_mock` flag is the
  budget-exclusion contract — keep it honest.
