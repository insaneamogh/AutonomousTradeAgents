/**
 * Tiny fetch wrapper. Owns:
 *   - base URL resolution (EXPO_PUBLIC_API_URL, sensible simulator fallback)
 *   - JSON encode/decode
 *   - error-shape normalization (throws ApiError with status + body)
 *   - Bearer-token injection from the auth store
 *   - automatic refresh-on-401 with single retry (Phase 3)
 *   - the trading lock: order-placing calls are refused unless the
 *     biometric gate has actually verified the user (see below)
 *
 * Refresh strategy (PLAN.md §3): the API hands out 15-min access tokens
 * + 30-day refresh tokens (rotated on every refresh call). The client
 * caches the access in memory; on a 401 it calls the auth store's
 * ``refresh()`` once + retries the original request. A second 401 drops
 * the user back to login.
 *
 * The auth store is read via a lazy getter to avoid a circular import
 * — ``authStore`` imports ``request`` for its own refresh call (which
 * passes ``skipAuth: true`` to avoid an infinite loop).
 */

import Constants from 'expo-constants';
import { Platform } from 'react-native';

const DEFAULT_PORT = 8000;

/**
 * Refuse a cleartext base URL outside `__DEV__`.
 *
 * This client places real-money orders and carries a bearer token on every
 * request. A release build talking `http://` would hand both to anyone on
 * the path, so a misconfigured EXPO_PUBLIC_API_URL must fail loudly at
 * startup rather than silently downgrade — and the LAN/simulator fallbacks
 * below have no business existing in a release build at all.
 */
function assertSecure(url: string): string {
  if (__DEV__) return url;
  if (url.startsWith('http://localhost:')) return url; // LOCAL-REPRO-HACK
  if (!url.startsWith('https://')) {
    throw new Error(
      `Insecure API base URL (${url}). Release builds must use https:// — ` +
        'set EXPO_PUBLIC_API_URL to your https API origin.',
    );
  }
  return url;
}

function resolveBaseUrl(): string {
  // 1. Explicit override wins.
  const fromEnv = process.env.EXPO_PUBLIC_API_URL;
  if (fromEnv) return assertSecure(fromEnv.replace(/\/+$/, ''));

  // 2. Production web build: the app is exported statically and served BY
  //    the API itself (FastAPI's SPA catch-all — see apps/api/app/main.py),
  //    so same-origin is always correct and survives a domain change with
  //    no rebuild. Guarded by `!__DEV__` so `expo start --web` (served from
  //    the Metro dev server's own origin, not the API's) still falls
  //    through to the debugger-host / localhost:8000 logic below.
  if (Platform.OS === 'web' && !__DEV__ && typeof window !== 'undefined') {
    return assertSecure(window.location.origin);
  }

  // 3. Use the Expo dev server's host so a physical device can reach the
  //    API at the same LAN IP as the bundler.
  const debuggerHost =
    Constants.expoConfig?.hostUri ?? Constants.expoGoConfig?.debuggerHost;
  if (debuggerHost) {
    const host = debuggerHost.split(':')[0];
    if (host && host !== 'localhost' && host !== '127.0.0.1') {
      return assertSecure(`http://${host}:${DEFAULT_PORT}`);
    }
  }

  // 4. Platform-specific simulator/emulator fallbacks.
  if (Platform.OS === 'android') return assertSecure(`http://10.0.2.2:${DEFAULT_PORT}`);
  return assertSecure(`http://localhost:${DEFAULT_PORT}`);
}

export const BASE_URL = resolveBaseUrl();

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/**
 * Turn any error `request()` can throw into something true to say out loud.
 *
 * ONE implementation, imported everywhere a call's error needs displaying.
 * This used to be copy-pasted per-screen, and the copies drifted: the
 * desktop council launcher got rewritten to stop blaming the user's
 * connection on a status-less failure, but the phone "Run" button
 * (`app/(tabs)/approvals.tsx`) and the theater screen
 * (`app/council/[runId].tsx`) each carried their OWN hardcoded string that
 * never got the memo — so the exact complaint the earlier fix targeted
 * kept reproducing on every surface that wasn't hand-patched. Import this
 * instead of writing another copy.
 *
 * A 422 is the server telling us exactly what is wrong with the input —
 * showing "the agent server may be cold" for it sends the user chasing
 * infrastructure when the real problem was e.g. the ticker they picked.
 * Two shapes of 422 exist and both need handling: our own hand-raised
 * errors carry `{detail: "<plain string>"}`, but FastAPI's own pydantic
 * validation carries `{detail: [{loc, msg, type}, ...]}` — an ARRAY.
 *
 * A response with no `status` at all (network failure, CORS, DNS, or a
 * container that hadn't finished booting yet) is the only case that's
 * genuinely infrastructure, so that keeps the "may still be starting up"
 * wording rather than blaming the caller's connection.
 */
export function runErrorMessage(err: unknown): string {
  const e = err as { status?: number; body?: { detail?: unknown } } | null;
  const detail = e?.body?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown } | undefined;
    if (typeof first?.msg === 'string') return first.msg;
  }
  if (e?.status === 429) return 'Daily council budget reached. Try again tomorrow.';
  if (typeof e?.status === 'number') {
    return `The server refused the request (${e.status}). Try again.`;
  }
  // Reached only when the error carries NO status — `fetch` rejected and no
  // response ever arrived, and every automatic network retry already
  // failed. Deliberately does not blame the caller's connection: the
  // observed cause is our own container restarting or still cold-starting,
  // not their wifi.
  return "The server didn't respond - it may still be starting up. Try again in a moment.";
}

// ─────────────────────────────────────────────────────────────────────
// Trading lock
//
// The biometric gate can only prove identity when the device HAS enrolled
// biometrics. When it doesn't, we still let the user read their portfolio
// — but every order-placing call is refused here, at the one chokepoint no
// screen can route around. Enforcing in the API client (rather than per
// button) means a new screen is fail-closed by default.
// ─────────────────────────────────────────────────────────────────────

/** Paths that move money. Matched as prefixes against the request path. */
const TRADING_PATHS = [
  '/api/v1/approvals/',
  '/api/v1/orders/execute',
  '/api/v1/positions/',
  '/api/v1/agent/run',
];

const TRADING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

let _tradingUnlocked = false;
let _tradingLockReason = 'Biometric verification required.';

/**
 * Called by the biometric gate. `unlocked` is true only after a successful
 * biometric authentication; unavailable hardware, missing enrolment, and a
 * backgrounded app all leave it false.
 */
export function setTradingUnlocked(unlocked: boolean, reason?: string): void {
  _tradingUnlocked = unlocked;
  if (reason) _tradingLockReason = reason;
}

export function isTradingUnlocked(): boolean {
  return _tradingUnlocked;
}

/** Thrown instead of issuing a trading request while the app is locked. */
export class TradingLockedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TradingLockedError';
  }
}

function isTradingRequest(path: string, method: string): boolean {
  if (!TRADING_METHODS.has(method)) return false;
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return TRADING_PATHS.some((p) => normalized.startsWith(p));
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
  /** When true, the Bearer header is NOT attached and 401s are NOT
   * intercepted. Used by /auth/refresh itself to avoid recursion.
   */
  skipAuth?: boolean;
  /**
   * Retry when `fetch` itself rejects — i.e. no HTTP response was ever
   * received (connection refused/reset, DNS, a container restarting
   * mid-flight). Never retries a response that arrived, whatever its
   * status.
   *
   * Up to `NETWORK_RETRY_DELAYS_MS.length` retries (currently 2, at 1s then
   * 2s), deliberately matching TanStack Query's own default query backoff
   * (`queryClient.ts`'s `retry: 2` with its built-in exponential delay) —
   * a single 1s retry turned out to not reliably outlast a real Railway
   * cold start/redeploy, so a mutation that opts in now gets exactly the
   * same resilience budget every GET on the same screen already gets for
   * free. Below that budget, a container that is still booting reads as
   * "the server didn't respond" even though the SAME blip would have
   * healed invisibly on any query.
   *
   * OPT-IN PER CALL, and it must stay that way. TanStack retries queries
   * but mutations zero times by default, which is the correct default
   * precisely because `orders/execute`, `approvals/decision` and
   * `positions/close` are mutations — silently re-sending one of those is
   * how you place a trade twice. Only set this on a call where a duplicate
   * is harmless.
   */
  retryOnNetworkError?: boolean;
}

// ─────────────────────────────────────────────────────────────────────
// Auth-store access via a lazy getter
//
// ``authStore`` imports ``request`` for /auth/refresh, so importing
// ``useAuthStore`` here at module-eval time would create a cycle. We
// resolve the store lazily on first auth-aware call instead.
// ─────────────────────────────────────────────────────────────────────

type AuthSnapshot = {
  accessToken: string | null;
  refresh: () => Promise<string | null>;
  /** True while ``authStore``'s ``restore()`` hasn't yet resolved
   * ('idle' / 'restoring'). See ``waitUntilBootstrapped`` below — this is
   * the flag that decides whether an authenticated call needs to wait for
   * it at all. */
  isBootstrapping: boolean;
  /**
   * Resolves once ``restore()`` has settled (to 'authenticated' OR
   * 'unauthenticated'); resolves immediately if it already has.
   *
   * On a cold boot, several screens' queries mount and fire their
   * authenticated GETs before ``restore()`` gets a chance to hydrate the
   * access token from storage — with no token yet, each one 401s, and the
   * interceptor below independently calls ``auth.refresh()`` to recover.
   * ``refresh()`` de-dupes concurrent callers into one network call (see
   * ``authStore.ts``'s ``inFlightRefresh`` docstring) but ONLY catches
   * ones that overlap closely enough in time — on a real device, several
   * distinct queries' round-trips rarely land in the exact same tick, so
   * they end up chaining into SEVERAL separate, sequential
   * ``/auth/refresh`` calls instead of joining ``restore()``'s one. The
   * API's refresh tokens are single-use and rotate on every call, so two
   * of those sequential calls landing close enough together still race:
   * one reads the token before the other's rotation is applied, and the
   * backend correctly treats the loser as a replay and revokes the whole
   * session (``superseded`` — see ``authStore.ts``'s ``CREDENTIAL_DEAD_CODES``),
   * signing the user out even though nothing was actually wrong with their
   * stored session. Waiting for bootstrap here means these queries never
   * fire with no token in the first place, so they never 401, and never
   * become extra, avoidable competitors for the one refresh ``restore()``
   * already has in flight.
   */
  waitUntilBootstrapped: () => Promise<void>;
};

let _getAuthSnapshot: (() => AuthSnapshot) | null = null;

/** Called once from the root layout to wire the auth store into the API client. */
export function registerAuthSnapshot(getter: () => AuthSnapshot): void {
  _getAuthSnapshot = getter;
}

function currentAuth(): AuthSnapshot | null {
  return _getAuthSnapshot ? _getAuthSnapshot() : null;
}

// ─────────────────────────────────────────────────────────────────────
// Request
// ─────────────────────────────────────────────────────────────────────

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return _request<T>(path, options, /* retried */ false, /* networkRetryCount */ 0);
}

/** A caller-initiated abort must never be retried — it is not a failure. */
function isAbortError(err: unknown): boolean {
  return (
    err instanceof Error && (err.name === 'AbortError' || err.name === 'TimeoutError')
  );
}

// Same shape as TanStack Query's default `retryDelay` (queryClient.ts's
// queries get `retry: 2` plus that same exponential backoff) — two retries,
// 1s then 2s. A single 1s retry (the original fix) was not always enough to
// outlast a genuine Railway cold start/redeploy; this gives the mutation the
// identical multi-second window every query on the same screen already
// survives on, no more, no less.
const NETWORK_RETRY_DELAYS_MS = [1_000, 2_000];

async function _request<T>(
  path: string,
  options: RequestOptions,
  retried: boolean,
  networkRetryCount: number,
): Promise<T> {
  const method = options.method ?? 'GET';
  if (isTradingRequest(path, method)) {
    if (!_tradingUnlocked) {
      throw new TradingLockedError(_tradingLockReason);
    }
  }

  const url = `${BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
  const headers: Record<string, string> = { 'content-type': 'application/json' };

  if (!options.skipAuth) {
    let auth = currentAuth();
    // Let restore() finish hydrating the access token before this call
    // fires at all — see `waitUntilBootstrapped`'s docstring above for why
    // firing early (then 401ing, then independently refreshing) is a real
    // "logged out on reload" bug, not just wasted requests.
    if (auth?.isBootstrapping) {
      await auth.waitUntilBootstrapped();
      auth = currentAuth();
    }
    if (auth?.accessToken) {
      headers['authorization'] = `Bearer ${auth.accessToken}`;
    }
  }

  // `fetch` rejects ONLY when no response was received at all — connection
  // refused/reset, DNS, or an abort. Anything the server actually answered,
  // including a 502 from the edge proxy, resolves here and is handled below
  // with its status intact. That distinction is what makes retrying this
  // branch safe: the request demonstrably did not reach a handler.
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: options.body != null ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
  } catch (err) {
    const delay = NETWORK_RETRY_DELAYS_MS[networkRetryCount];
    if (isAbortError(err) || !options.retryOnNetworkError || delay === undefined) throw err;
    // The observed cause is a container restart (Railway redeploy / cold
    // start): the first click lands while the old process is gone and the
    // new one is not listening yet, or is still inside its startup work
    // (lifespan hasn't finished, so nothing is accepting connections yet),
    // and a later attempt succeeds once it is.
    await new Promise((resolve) => setTimeout(resolve, delay));
    return _request<T>(path, options, retried, networkRetryCount + 1);
  }

  const text = await res.text();
  const body = text.length > 0 ? safeParse(text) : null;

  if (res.ok) {
    return body as T;
  }

  // 401 retry loop — only once, only on auth-aware calls, only when we
  // have a refresh path available.
  if (res.status === 401 && !options.skipAuth && !retried) {
    const auth = currentAuth();
    if (auth) {
      const fresh = await auth.refresh();
      if (fresh) {
        return _request<T>(path, options, /* retried */ true, networkRetryCount);
      }
    }
  }

  throw new ApiError(res.status, body, `HTTP ${res.status} ${path}`);
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
