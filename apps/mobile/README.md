# apps/mobile

Expo (React Native) app. The user-facing surface.

## Status — Phase 3 (auth + OAuth + push all shipped)

What works end-to-end:
- Magic-link login (`/auth/login` → email → magic-link → `/auth/verify` → tokens).
- Refresh-token rotation in the API client interceptor — 401 → silent refresh + retry once, second 401 → kick to login.
- Refresh tokens persist in `expo-secure-store`; access tokens are **memory-only** (never persisted).
- Biometric unlock (Face ID / Touch ID) on cold launch + after backgrounding.
- **Settings tab** with Notifications + Brokers + Security + Account sections — biometric toggle (with explicit acknowledge modal on disable per PLAN.md §3), broker connect/disconnect, sign out.
- **Alpaca OAuth** (paper, today) via the system browser → `autotrader://broker/callback` deep link.
- **Zerodha (Kite) connect** — opens the Kite login in the browser; the connection completes on the API's `/broker/connect/zerodha/redirect` page (no deep link needed). Kite tokens expire daily ~06:00 IST → reconnect each trading day; Settings shows a "refresh" button after login.
- **Push notifications** — `expo-notifications` permission state machine, idempotent device registration, foreground heads-up banner, tap-to-`/approvals` routing for both cold-start and warm-start taps.
- Deep-link handlers: `autotrader://auth/verify?...`, `autotrader://broker/callback?...`.

What's still missing (next sessions):
- Postgres adapters for Auth/Broker/Notification stores (migrations 0001/0004/0005 are in place).
- Real-Alpaca paper-trade smoke (full chain: login → connect → council → risk → executor → fill notification).
- E2E tests via Maestro / Detox. Until then, the flow is exercised manually + via the API-side test suite (88 tests across engine + agents + api).

## Dev

```bash
pnpm --filter @app/mobile dev
```

Run the API with auth enforcement (the default now):

```bash
make dev-api          # DEV_AUTH_BYPASS=0 — mobile MUST send Bearer
make dev-api-legacy   # DEV_AUTH_BYPASS=1 — pre-Phase-3 fallback if you need it
```

## Auth flow

```
┌──────────────┐
│  /auth/login │  email input → POST /api/v1/auth/request-login
└──────┬───────┘
       │
       │ 200 { expiresAt, devToken? }   (devToken non-null in dev)
       │
       ├── inbox / dev tap → autotrader://auth/verify?email=...&token=...
       │                                                                ↓
┌──────▼────────┐                                            ┌─────────────────────┐
│  /auth/verify │ ← param-driven auto-submit                 │  DeepLinkHandler    │
│               │   POST /api/v1/auth/verify                 │  (root _layout.tsx) │
│               │     → access + refresh                     └─────────────────────┘
│               │     → useAuthStore.signIn()
└──────┬────────┘
       │
       │ status = 'authenticated'
       │
┌──────▼────────────────────────────────────────────┐
│  AuthRouteGuard redirects /auth → /(tabs)         │
│  BiometricGate prompts Face ID / Touch ID         │
│  Tabs render; queries read accessToken via api.ts │
└───────────────────────────────────────────────────┘
```

## Where state lives

| Store | Role | Persistence |
|---|---|---|
| `useAuthStore` (Zustand) | `accessToken`, `user`, `status` (idle/restoring/authenticated/unauthenticated) | access = memory only; refresh + user = `expo-secure-store` via `src/lib/tokenStorage.ts` |
| `useBiometricStore` (Zustand) | `unlocked` + `requireOnLaunch` toggle | memory only; reset every cold launch |
| TanStack Query | server cache (account, activity, approvals) | memory only |

## Adding a screen that calls the API

1. Use the `request<T>(path)` helper from `src/lib/api.ts` (or a TanStack Query hook in `src/hooks/`). Both attach the Bearer header automatically.
2. If you need to skip auth (rare — basically only `/auth/*` itself), pass `{ skipAuth: true }`.
3. Don't read `accessToken` directly — the API client owns that. Read user identity via `useAuthStore((s) => s.user)`.
4. Don't bypass the API client for `fetch` — you'll lose the 401 → refresh retry.

## DO NOT

- **Don't persist the access token.** It's intentionally memory-only. The refresh token is the secure-store payload; the access is re-derived on every launch.
- **Don't disable biometric without a confirmation modal.** PLAN.md §3 calls for explicit acknowledgement.
- **Don't read tokens from the auth store inside the API client at module-eval time** — that creates a circular import. Use `registerAuthSnapshot()` once from the root layout.
- **Don't add new deps without explicit user instruction.** Everything used today (`expo-secure-store`, `expo-local-authentication`, `zustand`, `expo-linking`) is already declared in `package.json`.

## Token hygiene

- All colors come from `packages/ui/src/tokens.ts` via NativeWind classes (`bg-bg-base`, `text-text-primary`, etc.). **Never** use raw hex.
- `accent-primary` is the Approve-button color — never `gain`/green.
- Numerals use Inter with `font-variant-numeric: tabular-nums` (via `<PriceDisplay>`).
