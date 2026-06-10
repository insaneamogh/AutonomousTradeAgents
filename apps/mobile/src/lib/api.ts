/**
 * Tiny fetch wrapper. Owns:
 *   - base URL resolution (EXPO_PUBLIC_API_URL, sensible simulator fallback)
 *   - JSON encode/decode
 *   - error-shape normalization (throws ApiError with status + body)
 *   - Bearer-token injection from the auth store
 *   - automatic refresh-on-401 with single retry (Phase 3)
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

function resolveBaseUrl(): string {
  // 1. Explicit override wins.
  const fromEnv = process.env.EXPO_PUBLIC_API_URL;
  if (fromEnv) return fromEnv.replace(/\/+$/, '');

  // 2. Use the Expo dev server's host so a physical device can reach the
  //    API at the same LAN IP as the bundler.
  const debuggerHost =
    Constants.expoConfig?.hostUri ?? Constants.expoGoConfig?.debuggerHost;
  if (debuggerHost) {
    const host = debuggerHost.split(':')[0];
    if (host && host !== 'localhost' && host !== '127.0.0.1') {
      return `http://${host}:${DEFAULT_PORT}`;
    }
  }

  // 3. Platform-specific simulator/emulator fallbacks.
  if (Platform.OS === 'android') return `http://10.0.2.2:${DEFAULT_PORT}`;
  return `http://localhost:${DEFAULT_PORT}`;
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
  const url = `${BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
  const headers: Record<string, string> = { 'content-type': 'application/json' };

  if (!options.skipAuth) {
    const auth = currentAuth();
    if (auth?.accessToken) {
      headers['authorization'] = `Bearer ${auth.accessToken}`;
    }
  }

  const res = await fetch(url, {
    method: options.method ?? 'GET',
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
