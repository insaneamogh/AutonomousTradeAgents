# Plan H — let judges log in and connect their own Alpaca account

**Status:** §1 is FIXED — see below. §2/§3/§4 remain plan, not built.
Written 2026-08-30 by `ID:MODEL1REAL`.

**§1 update, verified 2026-09-01 by `ID:MODEL2OFF`:** the allowlist fix landed the
same day this plan was written — commit `2709d236` ("fix(auth,broker): stop
auto-attaching every signup to the operator's own Alpaca account"),
2026-08-30T18:12 UTC, ~39 minutes after the last confirmed leak. Verified directly
against the live production DB: a real signup at 2026-08-30T17:33 UTC (before the
fix) *did* get auto-attached to the operator's env connection — the exact failure
mode this section describes — and no signup since the fix has. That leaked
connection was later revoked and no orders were ever placed on it. This section's
narrative below is kept as-written for context; treat "what happens today" in it as
"what happened before 2026-08-30T18:12 UTC," not current behavior.

---

## 0. The short version

Most of what you want **already exists**. Alpaca OAuth (PKCE, state, token exchange,
web + native redirect URIs, encrypted token storage) is fully implemented in
`apps/api/app/services/broker/alpaca_oauth.py` and wired to
`POST /broker/connect/alpaca/start` + `/callback`. It is simply **not configured** —
`ALPACA_OAUTH_CLIENT_ID` / `_SECRET` are unset, so `is_configured()` is false and a
judge clicking Connect would dead-end on Alpaca's own error page.

So the work is: **one security fix, one OAuth registration, one empty-state pass.**

---

## 1. 🚨 FIX FIRST — a judge who signs up is auto-attached to YOUR account

### What happens today, verified 2026-08-30

`apps/api/app/routers/auth.py:129` calls `ensure_env_broker_connection(user_id)` from
**every path that mints a new session** — magic-link verify and Google sign-in. Its
own docstring says this is the deliberate "per-login catch-up" for users the boot
sweep missed.

The effect with `ALPACA_API_KEY` set on the server:

> **Any person who signs up gets an `env:alpaca` sentinel connection created for them,
> resolving to the server's own Alpaca keys — i.e. YOUR submitted paper account.**

And they are a *real* authenticated user, so `require_real_auth` passes. That means a
judge who signs up can:

- **approve a pending proposal** → places a real paper order on your account
- **close a position** → realises a loss on the account being judged for P&L
- **revoke the broker connection** → stops the agent
- **arm auto-approve consent**

This is not a theoretical multi-tenancy concern. It happens on their **first login**,
and it lands on the exact number the contest scores.

### The fix

Gate `ensure_env_broker_connection` to an explicit owner allowlist — the shared
account is the *operator's*, not every signup's:

```
ALPACA_ENV_CONNECTION_USER_IDS = <cron user id>[,<fixture user id>]
```

- Empty/unset → preserve today's behaviour **only** when the deployment is
  single-tenant. Safer default for this repo: if the var is unset, fall back to
  `AGENT_CRON_USER_ID` alone.
- Everyone else gets **no** connection and is invited to connect their own (§2).
- Apply the same allowlist inside `bootstrap_env_broker_connections`, which
  enumerates **every `User` row** at boot — otherwise the next restart re-attaches
  everyone the login path just stopped attaching.

> ⚠️ **Do not "fix" this by removing the env-connection mechanism.** The operator's own
> account depends on it — that is how the agent trades at all. Narrow who it applies
> to; do not delete it.

**Verify by acting, not reading:** sign up a throwaway account against staging and
confirm it lands with zero broker connections and an empty portfolio — *before*
inviting anyone.

---

## 2. Turn on "Connect your own Alpaca" — mostly registration, not code

### What already works

| Piece | Where | State |
|---|---|---|
| PKCE authorize URL + `state` | `alpaca_oauth.build_authorize_url` | ✅ built |
| Token exchange | `alpaca_oauth` | ✅ built |
| Pending-OAuth cache keyed by `state` | `PendingOAuthCache` | ✅ built |
| Fixed server-known redirect URIs (web + native) | `default_web_redirect_uri()` / `default_redirect_uri()` | ✅ built, and **deliberately never caller-supplied** — that would be an open-redirect / code-hijack hole |
| Encrypted token storage | `broker_connections.encrypted_access_token` | ✅ built |
| `POST /connect/alpaca/start` / `/callback` | `routers/broker.py`, both `require_real_auth` | ✅ built |

### What is missing

**Status 2026-08-31:** a fresh account hitting "Connect Alpaca paper" now sees the
server's own warning — *"Alpaca OAuth is not configured on this server
(ALPACA_OAUTH_CLIENT_ID/_SECRET)"*. That message is **correct and working as designed**
(the code detects the placeholder client id and refuses to send the user into a
dead end). It is telling you the two env vars are unset, not that anything is broken.

⚠️ **Blocking unknown, checked 2026-08-31:** Alpaca's public OAuth docs confirm
*"All API clients must authenticate with OAuth 2.0"* but **do not document the app
registration flow, whether PAPER accounts are supported, the redirect-URI rules, or
the scope list.** The pages referenced ("Register Your App", "OAuth Integration
Guide") were not retrievable. **Resolve this with Alpaca support before building on
it** — if OAuth is live-only, this route is closed and the shared demo login is the
answer instead.

1. **Register an OAuth app with Alpaca** (their dashboard) and set:
   ```
   ALPACA_OAUTH_CLIENT_ID=…
   ALPACA_OAUTH_CLIENT_SECRET=…
   ```
   Without these, `client_id()` returns the literal `DEV-ALPACA-CLIENT-ID` placeholder
   and Alpaca answers with a generic "unknown client" error that gives no hint the
   problem is server-side. The code already detects this and returns a `warnings[]`
   entry from `/connect/alpaca/start` — **make the UI show that warning instead of
   navigating into a dead end.**
2. **Register the web redirect URI** with Alpaca — it must exactly match
   `default_web_redirect_uri()` (this API's own `GET /connect/alpaca/redirect`).
   Mismatched redirect URIs are the single most common OAuth failure.
3. Confirm Alpaca's OAuth supports **paper** accounts for your app type. If it is
   live-only, this whole route is unavailable and §3 becomes the answer instead —
   **check this before doing anything else in §2.**

---

## 3. The empty state — right now a fresh user is shown a lie

A user with no broker connection has no `positions_snapshot` row, so
`store.get_account()` falls through to the **hardcoded cold-boot fixture**
(`postgres_store.py:99`): `equity=100_000, cash=100_000, buying_power=200_000`.

A judge who signs up today sees a confident, fully-populated $100,000 portfolio that
**is a constant in our source code**. The tell is `buying_power=200_000`; a real
Alpaca paper account reports `400,000`.

**That is the worst possible failure mode for this feature** — not an error, a
plausible fake. Fix it before opening signups:

- `get_account` should signal "no broker connected" rather than inventing a
  portfolio. Either a nullable response or an explicit `status: "disconnected"`.
- Every screen then renders a **connect-your-account empty state** instead of zeroes.
  `Settings.tsx` already has the right pattern (*"No broker linked — the council can
  still deliberate, but nothing can execute"*); extend it to Dashboard/Positions.
- Keep the cold-boot fixture **only** for the genuine cold-boot case (connection
  exists, reconciler has not ticked yet), which is what it was written for.

---

## 4. Recommended shape for the submission

Offer judges **both**, and label them:

1. **A read-only demo link** ([`PLAN_JUDGE_SURFACE.md`](PLAN_JUDGE_SURFACE.md) §1) —
   they see the real account with real history and can change nothing. This is what
   most judges will use, and it is the one that protects the P&L being scored.
2. **"Or connect your own Alpaca paper account"** — full login, their keys, their
   account, their agent. Proves the product is real rather than a single-account demo.

Those are complementary: (1) protects your number, (2) proves it generalises.

---

## 5. Tests

| Test | Break this to make it fail |
|---|---|
| **`test_signup_does_not_attach_the_server_account`** | Drop the allowlist from the login path — a new user gets an `env:alpaca` connection |
| **`test_boot_sweep_respects_the_allowlist`** | Drop it from `bootstrap_env_broker_connections` — the next restart re-attaches everyone |
| `test_owner_still_gets_the_env_connection` | Over-narrow the allowlist — **this is the one that catches breaking your own agent** |
| `test_account_reports_disconnected_with_no_connection` | Return the cold-boot fixture unconditionally |
| `test_cold_boot_fixture_still_used_when_a_connection_exists` | Guards the legitimate case §3 must not regress |

**Baseline: 961 passed, 10 skipped** (plus the mobile suite: 28). `git stash` and
re-run before blaming your change.

---

## 6. Where you will go wrong

1. **Opening signups before §1 lands.** Every new account gets write access to the
   submitted trading account. This is the whole reason §1 is first.
2. **Deleting the env-connection mechanism** instead of narrowing it — that is how the
   agent trades at all.
3. **Over-narrowing the allowlist** and silently disconnecting the operator. Test both
   directions.
4. **Shipping OAuth without registering the redirect URI**, or with the `DEV-ALPACA-…`
   placeholder still in place — the failure surfaces on Alpaca's page as "unknown
   client" and looks like their bug, not ours.
5. **Accepting a caller-supplied `redirect_uri`.** The current code picks between two
   fixed server-known values on a platform hint, and the comment explains why. Leave it.
6. **Leaving the cold-boot fixture as the no-connection response.** A plausible fake
   portfolio is worse than an honest empty state.

---

*Related: [`PLAN_JUDGE_SURFACE.md`](PLAN_JUDGE_SURFACE.md) · [`PLAN_NEXT.md`](PLAN_NEXT.md) ·
[`../CLAUDE.md`](../CLAUDE.md)*
