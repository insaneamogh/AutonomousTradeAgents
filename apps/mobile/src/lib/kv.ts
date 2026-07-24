/**
 * Synchronous key-value storage (MMKV).
 *
 * Used for small, non-sensitive UI preferences that must be read on the very
 * first render with no async flash — the theme preference is the first client.
 * Secrets (refresh token) never go here; they use SecureStore via
 * ``src/lib/tokenStorage``.
 *
 * MMKV has no web implementation, so we fall back to localStorage on web
 * (a dev-preview surface only). The shape mirrors the subset of the MMKV API
 * we use.
 */

import { Platform } from 'react-native';

interface SyncKv {
  getString(key: string): string | undefined;
  set(key: string, value: string): void;
  delete(key: string): void;
}

function createNativeKv(): SyncKv {
  // Lazy require so web bundles don't pull the native module.
  const { MMKV } = require('react-native-mmkv') as typeof import('react-native-mmkv');
  const mmkv = new MMKV({ id: 'autotrader.prefs' });
  return {
    getString: (key) => mmkv.getString(key),
    set: (key, value) => mmkv.set(key, value),
    delete: (key) => mmkv.delete(key),
  };
}

function createWebKv(): SyncKv {
  return {
    getString: (key) => globalThis.localStorage?.getItem(key) ?? undefined,
    set: (key, value) => globalThis.localStorage?.setItem(key, value),
    delete: (key) => globalThis.localStorage?.removeItem(key),
  };
}

export const kv: SyncKv = Platform.OS === 'web' ? createWebKv() : createNativeKv();
