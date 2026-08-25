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
  return _request<T>(path, options, /* retried */ false);
}

async function _request<T>(
  path: string,
  options: RequestOptions,
  retried: boolean,
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
    const auth = currentAuth();
    if (auth?.accessToken) {
      headers['authorization'] = `Bearer ${auth.accessToken}`;
    }
  }

  const res = await fetch(url, {
    method,
    headers,
    body: options.body != null ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });

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
        return _request<T>(path, options, /* retried */ true);
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
