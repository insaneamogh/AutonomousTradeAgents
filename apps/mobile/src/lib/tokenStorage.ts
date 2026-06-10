/**
 * Persisted token storage.
 *
 * We split storage by token role intentionally:
 *
 *   - REFRESH token   → SecureStore (Keychain on iOS, EncryptedSharedPreferences
 *                       on Android). Persists across launches. Used to obtain
 *                       a fresh access token on app start.
 *   - ACCESS  token   → memory ONLY (the Zustand authStore). Never persisted.
 *                       If the app is killed, the next launch re-derives it
 *                       from the stored refresh.
 *
 * This matches PLAN.md §3 and the apps/api/AUTH.md DO NOT list. Storing the
 * access token at rest would let a device-snapshot attacker replay it
 * silently for the next 15 minutes; the refresh token, on the other hand,
 * is single-use-with-rotation + server-revocable so its leak is bounded.
 */

import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';

const REFRESH_KEY = 'autotrader.auth.refresh_token';
const SESSION_USER_KEY = 'autotrader.auth.user';

// SecureStore has no web implementation. Web is a dev-preview surface only
// (real users are on iOS/Android), so localStorage is an acceptable
// fallback there — never ship web to production with broker tokens.
const webStore = {
  async getItemAsync(key: string): Promise<string | null> {
    return globalThis.localStorage?.getItem(key) ?? null;
  },
  async setItemAsync(key: string, value: string): Promise<void> {
    globalThis.localStorage?.setItem(key, value);
  },
  async deleteItemAsync(key: string): Promise<void> {
    globalThis.localStorage?.removeItem(key);
  },
};

const store = Platform.OS === 'web' ? webStore : SecureStore;

interface PersistedUser {
  userId: string;
  email: string;
}

/** Save the refresh token. Called by the auth store on successful login + every refresh rotation. */
export async function saveRefreshToken(token: string): Promise<void> {
  await store.setItemAsync(REFRESH_KEY, token, {
    keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK,
  });
}

/** Read the stored refresh token (or null). Called once on app launch. */
export async function loadRefreshToken(): Promise<string | null> {
  return store.getItemAsync(REFRESH_KEY);
}

/** Delete the stored refresh token. Called on logout + on auth failures. */
export async function clearRefreshToken(): Promise<void> {
  await store.deleteItemAsync(REFRESH_KEY);
}

/** Persist the user identity alongside the refresh token. Display-only — we
 * still re-fetch on launch to validate the session is alive.
 */
export async function savePersistedUser(user: PersistedUser): Promise<void> {
  await store.setItemAsync(SESSION_USER_KEY, JSON.stringify(user), {
    keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK,
  });
}

export async function loadPersistedUser(): Promise<PersistedUser | null> {
  const raw = await store.getItemAsync(SESSION_USER_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as PersistedUser;
    if (typeof parsed.userId === 'string' && typeof parsed.email === 'string') {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}

export async function clearPersistedUser(): Promise<void> {
  await store.deleteItemAsync(SESSION_USER_KEY);
}

/** Convenience: wipe everything. Logout calls this; some auth-error paths too. */
export async function clearAll(): Promise<void> {
  await Promise.all([clearRefreshToken(), clearPersistedUser()]);
}
