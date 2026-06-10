/**
 * Zustand auth store.
 *
 * Owns:
 *   - The in-memory access token (NEVER persisted).
 *   - The current user identity (mirrored from the JWT claims + the verify response).
 *   - The bootstrap state: 'idle' → 'restoring' → 'authenticated' | 'unauthenticated'.
 *
 * Persistence: only the refresh token + a small user record persist via
 * SecureStore (see ``src/lib/tokenStorage``). On launch the root layout
 * calls ``restore()`` to hydrate access from the persisted refresh.
 *
 * Why Zustand here:
 *   - The auth store is read from many places (the API interceptor,
 *     screens, the biometric gate) — Context would force a redraw on every
 *     access-token rotation. Zustand's selector-driven subscriptions keep
 *     unrelated screens still while the token rotates in the background.
 */

import { create } from 'zustand';

import { ApiError, request } from '@/lib/api';
import {
  clearAll,
  loadPersistedUser,
  loadRefreshToken,
  savePersistedUser,
  saveRefreshToken,
} from '@/lib/tokenStorage';

export type AuthStatus = 'idle' | 'restoring' | 'authenticated' | 'unauthenticated';

export interface AuthUser {
  userId: string;
  email: string;
}

interface IssuedTokensResponse {
  userId: string;
  email: string;
  accessToken: string;
  refreshToken: string;
  accessExpiresInSeconds: number;
  refreshExpiresInSeconds: number;
}

interface AuthState {
  status: AuthStatus;
  user: AuthUser | null;
  accessToken: string | null;

  /** Hydrate from SecureStore. Called once at app launch. */
  restore: () => Promise<void>;

  /** Persist tokens + flip status. Called by /auth/verify happy path. */
  signIn: (issued: IssuedTokensResponse) => Promise<void>;

  /** Rotate access/refresh after a 401. Returns the new access token on success. */
  refresh: () => Promise<string | null>;

  /** Wipe everything (in-memory + SecureStore). Tries server-side revoke but
   * never blocks logout on it.
   */
  signOut: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  status: 'idle',
  user: null,
  accessToken: null,

  restore: async () => {
    set({ status: 'restoring' });

    const refresh = await loadRefreshToken();
    if (!refresh) {
      set({ status: 'unauthenticated', user: null, accessToken: null });
      return;
    }

    // We trust the persisted user record for UI hydration but immediately
    // try a /auth/refresh — that's the source of truth. If refresh fails,
    // the user is logged out cleanly.
    const persistedUser = await loadPersistedUser();
    if (persistedUser) {
      set({ user: persistedUser });
    }

    try {
      const issued = await request<IssuedTokensResponse>('/api/v1/auth/refresh', {
        method: 'POST',
        body: { refreshToken: refresh },
        // The interceptor would loop forever if it tried to refresh during
        // a refresh. Bypass it for this call.
        skipAuth: true,
      });
      await get().signIn(issued);
    } catch (err) {
      // Refresh failed → session is dead. Wipe + drop to login.
      if (err instanceof ApiError && err.status === 401) {
        await clearAll();
      }
      set({ status: 'unauthenticated', user: null, accessToken: null });
    }
  },

  signIn: async (issued: IssuedTokensResponse) => {
    await saveRefreshToken(issued.refreshToken);
    await savePersistedUser({ userId: issued.userId, email: issued.email });
    set({
      status: 'authenticated',
      user: { userId: issued.userId, email: issued.email },
      accessToken: issued.accessToken,
    });
  },

  refresh: async () => {
    const refresh = await loadRefreshToken();
    if (!refresh) {
      set({ status: 'unauthenticated', user: null, accessToken: null });
      return null;
    }
    try {
      const issued = await request<IssuedTokensResponse>('/api/v1/auth/refresh', {
        method: 'POST',
        body: { refreshToken: refresh },
        skipAuth: true,
      });
      await get().signIn(issued);
      return issued.accessToken;
    } catch {
      // Any failure here = session gone. Drop to login.
      await clearAll();
      set({ status: 'unauthenticated', user: null, accessToken: null });
      return null;
    }
  },

  signOut: async () => {
    const refresh = await loadRefreshToken();
    // Best-effort revoke. Never block logout on the network.
    if (refresh) {
      try {
        await request('/api/v1/auth/logout', {
          method: 'POST',
          body: { refreshToken: refresh },
        });
      } catch {
        /* swallow */
      }
    }
    await clearAll();
    set({ status: 'unauthenticated', user: null, accessToken: null });
  },
}));
