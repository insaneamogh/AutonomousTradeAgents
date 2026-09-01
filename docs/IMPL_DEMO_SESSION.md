# IMPL 5 — the read-only demo session link

**Implementation spec.** No dependencies. Written 2026-08-31 by `ID:MODEL1REAL`.
Est **4h**.

> Goal: a link in the submission that shows a judge the **real** account with **real**
> history, and lets them change **nothing** — without weakening a single existing
> auth check, and without touching normal login/logout.

---

## 0. Why the obvious approaches both fail (verified 2026-08-30)

| Approach | Why it fails |
|---|---|
| `DEV_AUTH_BYPASS=1` | Resolves to `FIXTURE_USER_ID`, but every decision is written to `AGENT_CRON_USER_ID`. Judge gets a **fully working, completely empty app.** |
| "Just let them sign up" | A new user has no broker connection → no `positions_snapshot` → `get_account` returns the **hardcoded cold-boot fixture** (`equity=100_000, buying_power=200_000`). The real account reports `400,000`. They would be reading a constant in our source. |

Both give a plausible-looking fake. The identity a judge lands as **must resolve to the
account that owns the data.**

---

## 1. 🔑 The design — the security boundary already exists

`require_real_auth` (`apps/api/app/middleware/auth.py:187`) refuses any
`AuthedUser` whose `is_dev_bypass` is True. **All six money routes already call it:**

```
POST /approvals/{id}/decision          POST /orders/execute/{id}
POST /positions/{id}/close             POST|DELETE /broker/connections/*
POST /broker/connections/{id}/auto-approve-consent
POST /circuit-breaker/acknowledge
```

So: **mint the demo identity with `is_dev_bypass=True`** (or add a parallel `is_demo`
flag that `require_real_auth` rejects identically). Every mutating route then refuses it
**with zero new code**, while every read route works normally.

That is the whole feature. A link that reads everything and changes nothing, built by
*reusing* the existing check rather than adding a new one.

---

## 2. Implementation

### 2.1 Mint the token — reuse `jwt_service`

`apps/api/app/services/auth/jwt_service.py` already has `mint(...)`, `verify(...)`,
`mint_access`, `verify_access`, and a `Claims` dataclass. **Do not hand-roll signing.**

```python
DEMO_TOKEN_TYP = "demo"

def mint_demo(*, secret: str, user_id: str, expires_at: datetime) -> str:
    """A demo session token. Same signature scheme as access tokens, a
    DIFFERENT `typ` so it can never be accepted where an access token is
    expected (and vice versa)."""
```

A separate `typ` is load-bearing: a demo token must not be usable as an access token by
any code path that only checks the signature.

### 2.2 Exchange endpoint

```
POST /api/v1/auth/demo   { "token": "<signed>" }  ->  IssuedTokensResponse
```

- No auth required (that is the point), but **rate-limit it**.
- Verifies signature + `typ == "demo"` + not expired.
- Mints a **short-lived access token carrying a demo marker** and returns it in the same
  shape the magic-link flow returns, so the client stores it identically.
- **No refresh token.** A demo session expires and the judge re-clicks the link.

### 2.3 The marker must survive into `AuthedUser`

In `get_current_user`, when the access token's claims carry the demo marker:

```python
return AuthedUser(
    id=demo_user_id,          # DEMO_USER_ID env — the cron user
    email="demo@…",
    auth_method="demo",
    is_dev_bypass=True,       # ← this single field is the entire enforcement
    session_id=claims.sid,
)
```

> 🚨 **`is_dev_bypass=True` is the enforcement.** Not a middleware, not a route list —
> one field consumed by a check that already exists on every dangerous route. Any future
> mutating route that forgets `require_real_auth` is a hole; §5 has the test that
> catches that.

### 2.4 Client — `apps/mobile/app/_layout.tsx` or the root route

On load: if `?demo=<token>` is present → `POST /auth/demo` → store the session exactly
like a normal login → **strip the query param from the URL** (`history.replaceState`) so
it does not sit in the address bar or leak via `Referer`.

Normal magic-link / Google login and logout are **untouched**.

### 2.5 Config

| Var | Value |
|---|---|
| `DEMO_USER_ID` | `43221580-69bc-4134-8e1e-5af75499d874` (the cron user) |
| `DEMO_SESSION_ENABLED` | `0` default |
| `DEMO_TOKEN_TTL_DAYS` | e.g. `14` — must outlast the submission deadline |

Generate the link with a small CLI (`scripts/mint_demo_link.py`) so the token is never
committed.

---

## 3. Close the three leaky routes first

These accept the bypass today and must not be reachable by a demo session:

| Route | Risk | Fix |
|---|---|---|
| `POST /agent/run`, `/agent/run/start` | ~$0.04 LLM spend **per call, unbounded**. A crawler or an impatient judge is a real bill and pollutes the decision log mid-judging. | `require_real_auth`, **or** allow it with a hard rate limit (1/min/IP) — judges do not need it, the scheduler produces runs |
| `POST /watchlist`, `DELETE /watchlist/{symbol}` | Changes **what the agent trades**. A judge deleting NVDA mid-contest silently changes the P&L being scored. | `require_real_auth` |
| `POST /review/{decision_id}` | Pollutes reflection/calibration data | `require_real_auth` |

---

## 4. UI — say it on screen

A persistent banner while the session is demo:

> **Read-only demo** · viewing the live paper account · trading actions are disabled

(Shipped deliberately without a hardcoded account ID — `DemoSessionBanner.tsx` reads
generic "the live paper account" text precisely so a future account swap, like the
`PA3IAZI74E5R`→`PA31OTNBGE9I` one on 2026-09-01, never leaves stale copy on screen.)

And **disable** (not hide) Approve / Decline / Close / Revoke buttons, with a tooltip.
A judge who clicks Approve and gets a silent 401 reads it as a bug; a disabled button
with a reason reads as deliberate — and it demonstrates the permission model rather than
hiding it.

---

## 5. Tests

`apps/api/tests/test_demo_session.py`

| Test | Break this to make it fail |
|---|---|
| **`test_demo_session_refused_by_every_mutating_route`** | Parametrise over all 6 money routes; drop `is_dev_bypass=True` from the minting. **The whole feature, as one test.** |
| **`test_every_mutating_route_uses_require_real_auth`** | Introspect the router for POST/PUT/PATCH/DELETE and assert the dependency. **Catches a future route that forgets it** — the one hole this design can develop. |
| `test_demo_token_cannot_be_used_as_an_access_token` | Accept `typ="demo"` in `verify_access` |
| `test_expired_demo_token_refused` | Skip the expiry check |
| `test_demo_reads_the_cron_users_data` | Resolve to `FIXTURE_USER_ID` → empty app |
| `test_demo_endpoint_is_rate_limited` | Remove the limit |
| `test_query_param_is_stripped_after_exchange` | Leave `?demo=` in the URL |
| `test_agent_run_not_reachable_by_demo` | Leave it on `get_current_user` |

**Baseline: 969 passed, 11 skipped.**

### Verify by acting

Open the link in a **private window**. Confirm: dashboard, decisions, positions,
insights all populate with real data — **and** that `POST /approvals/{id}/decision`
returns 401. Test the 401 explicitly; do not infer it from reading the code.

---

## 6. Where you will go wrong

1. **Turning on `DEV_AUTH_BYPASS` instead.** Wrong user id → empty app.
2. **Setting `ENV=production`** to "harden" it — `_dev_bypass_enabled()` force-disables
   the bypass path there and the demo silently closes.
3. **Removing `require_real_auth` from anything** to make the demo smoother. The entire
   design is that a stranger reads everything and changes nothing.
4. **Reusing `typ="access"` for the demo token.** Different `typ`, always.
5. **Issuing a refresh token** to a demo session. It expires; they re-click.
6. **Leaving `?demo=` in the address bar.** Strip it.
7. **Hiding the disabled buttons** instead of disabling them with a reason — the
   permission model is a *feature*, show it.
8. **Shipping it before §3.** LLM spend and a mutable watchlist are both reachable
   otherwise.

---

*Related: [`IMPL_CONTRACT_FUNNEL_UI.md`](IMPL_CONTRACT_FUNNEL_UI.md) ·
[`IMPL_REFUSAL_LEDGER.md`](IMPL_REFUSAL_LEDGER.md) · [`PLAN_JUDGE_SURFACE.md`](PLAN_JUDGE_SURFACE.md) ·
[`PLAN_MULTI_TENANT.md`](PLAN_MULTI_TENANT.md)*
