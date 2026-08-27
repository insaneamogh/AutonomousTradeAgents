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

/**
 * Refresh-failure codes that mean the stored credential itself is dead —
 * see ``apps/api/app/services/auth/auth.py``'s ``REFRESH_CODE_*`` constants.
 * Only these justify wiping the persisted refresh token. Any OTHER 401
 * (a bare `session_not_found`, an unrecognized future code, or no `code`
 * at all — e.g. an older API build) means "the backend doesn't currently
 * recognize this session", not "this credential can never work again": a
 * later successful backend restore (e.g. once Postgres persistence lands)
 * should still be able to complete without forcing a brand-new login.
 */
const CREDENTIAL_DEAD_CODES = new Set(['session_revoked', 'token_invalid', 'superseded']);

/** True only for a 401 whose body carries one of the CREDENTIAL_DEAD_CODES. */
function isCredentialDead(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 401) return false;
  const body = err.body as { code?: unknown } | null | undefined;
  const code = typeof body?.code === 'string' ? body.code : null;
  return code !== null && CREDENTIAL_DEAD_CODES.has(code);
}

/**
 * De-dupes concurrent ``refresh()`` calls into one in-flight request.
 *
 * The API's refresh tokens are single-use and rotate on every call
 * (server-side compare-and-swap on the stored hash). The dashboard fires
 * several independently-polling queries (positions, scanner status,
 * health, review — every 30-60s) against a 15-minute access token, so
 * near the expiry boundary it was common for two or more requests to 401
 * within the same tick. Each one independently called ``refresh()``, so
 * both read the SAME stored refresh token and both POSTed it. The winner
 * rotated it; the loser's identical token was then a REPLAY — the server
 * doesn't just reject it, it revokes the whole session
 * (``auth.py::refresh`` — "somebody is using a replayed older refresh").
 * The loser's caller saw ``superseded`` in ``CREDENTIAL_DEAD_CODES`` and
 * wiped the credential, silently signing the user out mid-session. The
 * one visible symptom was whichever request happened to be the loser —
 * often "Decision failed — try again" on an approve tap, with no hint
 * that the real cause was a session the user never asked to end.
 *
 * Sharing one in-flight promise means every concurrent 401 waits on the
 * SAME refresh call and gets the SAME outcome — the race this was built
 * to detect (an attacker replaying an old token after we've already
 * rotated) still revokes correctly; two of our own requests hitting
 * expiry in the same tick no longer look like one.
 */
let inFlightRefresh: Promise<string | null> | null = null;

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
      // Only wipe storage when the backend says THIS credential is dead
      // (revoked / invalid / superseded). A bare "session not found" (or
      // any other failure) just means we can't restore right now — keep
      // the refresh token so a later successful restore doesn't force a
      // brand-new login.
      if (isCredentialDead(err)) {
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
    // Join the in-flight call rather than starting a second one — see the
    // comment on ``inFlightRefresh`` above for why this isn't optional.
    if (inFlightRefresh) return inFlightRefresh;

    const run = async (): Promise<string | null> => {
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
      } catch (err) {
        // Same distinction as restore(): only wipe storage when the
        // credential itself is confirmed dead, not on every failure — this
        // path is also used by the API interceptor's silent-refresh-on-401,
        // so it used to be even more aggressive than restore()'s about
        // wiping on ANY thrown error.
        if (isCredentialDead(err)) {
          await clearAll();
        }
        set({ status: 'unauthenticated', user: null, accessToken: null });
        return null;
      }
    };

    inFlightRefresh = run().finally(() => {
      inFlightRefresh = null;
    });
    return inFlightRefresh;
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
