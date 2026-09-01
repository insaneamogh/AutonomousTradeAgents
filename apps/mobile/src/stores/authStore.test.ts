/**
 * authStore's session-refresh resilience: `restore()` and `refresh()` must
 * only wipe the persisted refresh token when the backend says THIS
 * credential is dead (`session_revoked` / `token_invalid` / `superseded`)
 * — a bare `session_not_found` (or no `code` at all, e.g. an older API
 * build) must NOT wipe storage, so a later successful backend restore
 * doesn't force a brand-new login. See
 * `apps/api/app/services/auth/auth.py`'s `REFRESH_CODE_*` constants for
 * the backend half of this contract.
 *
 * First fetch-mock test in this app's auth surface — `api.test.ts`'s
 * smoke tests deliberately stop short of touching `request()` itself.
 * We mock global `fetch` directly and `jest.mock()` `tokenStorage` (via an
 * explicit factory, so the real module — and therefore expo-secure-store —
 * is never evaluated) so we can assert exactly when `clearAll()` does or
 * doesn't run, without touching a real SecureStore.
 */

import {
  clearAll,
  loadPersistedUser,
  loadRefreshToken,
  savePersistedUser,
  saveRefreshToken,
} from '@/lib/tokenStorage';
import { useAuthStore } from '@/stores/authStore';

jest.mock('@/lib/tokenStorage', () => ({
  loadRefreshToken: jest.fn(),
  loadPersistedUser: jest.fn(),
  saveRefreshToken: jest.fn(),
  savePersistedUser: jest.fn(),
  clearAll: jest.fn(),
  clearRefreshToken: jest.fn(),
  clearPersistedUser: jest.fn(),
}));

const mockLoadRefreshToken = jest.mocked(loadRefreshToken);
const mockLoadPersistedUser = jest.mocked(loadPersistedUser);
const mockSaveRefreshToken = jest.mocked(saveRefreshToken);
const mockSavePersistedUser = jest.mocked(savePersistedUser);
const mockClearAll = jest.mocked(clearAll);

/** Shape a mocked `fetch` response the way `src/lib/api.ts`'s `request()` reads it. */
function mockFetchResponse(status: number, body: unknown): void {
  const ok = status >= 200 && status < 300;
  (global.fetch as jest.Mock).mockResolvedValueOnce({
    ok,
    status,
    // eslint-disable-next-line @typescript-eslint/require-await
    text: async () => JSON.stringify(body),
  });
}

const ISSUED = {
  userId: 'user-1',
  email: 'user@example.com',
  accessToken: 'access-token-abc',
  refreshToken: 'refresh-token-xyz',
  accessExpiresInSeconds: 900,
  refreshExpiresInSeconds: 2_592_000,
};

beforeEach(() => {
  jest.resetAllMocks();
  global.fetch = jest.fn();
  useAuthStore.setState({ status: 'idle', user: null, accessToken: null });
  mockLoadPersistedUser.mockResolvedValue(null);
  mockSaveRefreshToken.mockResolvedValue(undefined);
  mockSavePersistedUser.mockResolvedValue(undefined);
  mockClearAll.mockResolvedValue(undefined);
});

describe('authStore.restore()', () => {
  it('goes straight to unauthenticated (no wipe) with no persisted refresh token', async () => {
    mockLoadRefreshToken.mockResolvedValue(null);

    await useAuthStore.getState().restore();

    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(global.fetch).not.toHaveBeenCalled();
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('signs in on a successful refresh', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(200, ISSUED);

    await useAuthStore.getState().restore();

    expect(useAuthStore.getState().status).toBe('authenticated');
    expect(useAuthStore.getState().accessToken).toBe(ISSUED.accessToken);
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('does NOT wipe storage on a session_not_found-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'session not found', code: 'session_not_found' });

    await useAuthStore.getState().restore();

    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('does NOT wipe storage on a 401 carrying no code at all (older API build)', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'refresh token rejected' });

    await useAuthStore.getState().restore();

    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('DOES wipe storage on a session_revoked-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'session revoked', code: 'session_revoked' });

    await useAuthStore.getState().restore();

    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(mockClearAll).toHaveBeenCalledTimes(1);
  });

  it('DOES wipe storage on a token_invalid-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'refresh token rejected', code: 'token_invalid' });

    await useAuthStore.getState().restore();

    expect(mockClearAll).toHaveBeenCalledTimes(1);
  });

  it('DOES wipe storage on a superseded-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, {
      detail: 'refresh token superseded — session revoked',
      code: 'superseded',
    });

    await useAuthStore.getState().restore();

    expect(mockClearAll).toHaveBeenCalledTimes(1);
  });
});

describe('authStore.refresh()', () => {
  it('returns null and does not wipe with no persisted refresh token', async () => {
    mockLoadRefreshToken.mockResolvedValue(null);

    const result = await useAuthStore.getState().refresh();

    expect(result).toBeNull();
    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('returns the new access token and signs in on success', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(200, ISSUED);

    const result = await useAuthStore.getState().refresh();

    expect(result).toBe(ISSUED.accessToken);
    expect(useAuthStore.getState().status).toBe('authenticated');
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('does NOT wipe storage on a session_not_found-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'session not found', code: 'session_not_found' });

    const result = await useAuthStore.getState().refresh();

    expect(result).toBeNull();
    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('does NOT wipe storage on a 401 carrying no code at all (older API build)', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'refresh token rejected' });

    const result = await useAuthStore.getState().refresh();

    expect(result).toBeNull();
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('DOES wipe storage on a session_revoked-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'session revoked', code: 'session_revoked' });

    const result = await useAuthStore.getState().refresh();

    expect(result).toBeNull();
    expect(mockClearAll).toHaveBeenCalledTimes(1);
  });

  it('DOES wipe storage on a token_invalid-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'bad refresh token', code: 'token_invalid' });

    const result = await useAuthStore.getState().refresh();

    expect(result).toBeNull();
    expect(mockClearAll).toHaveBeenCalledTimes(1);
  });

  it('DOES wipe storage on a superseded-coded 401', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(401, { detail: 'refresh token superseded', code: 'superseded' });

    const result = await useAuthStore.getState().refresh();

    expect(result).toBeNull();
    expect(mockClearAll).toHaveBeenCalledTimes(1);
  });

  // Refresh tokens are single-use and rotate on the server; a second call
  // presenting the same (now-spent) token is treated as a replay and the
  // server revokes the WHOLE session, not just the second call. Several
  // dashboard queries poll independently against one 15-minute access
  // token, so two or more requests hitting a 401 in the same tick used to
  // each fire their own `/auth/refresh` — the loser's call died as a
  // "superseded" replay and wiped a credential that was never actually
  // dead. These pin that concurrent callers share ONE network call.
  it('de-dupes concurrent calls into a single network request', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(200, ISSUED);

    const [a, b, c] = await Promise.all([
      useAuthStore.getState().refresh(),
      useAuthStore.getState().refresh(),
      useAuthStore.getState().refresh(),
    ]);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(a).toBe(ISSUED.accessToken);
    expect(b).toBe(ISSUED.accessToken);
    expect(c).toBe(ISSUED.accessToken);
    expect(useAuthStore.getState().status).toBe('authenticated');
  });

  it('allows a fresh network call once the in-flight one has settled', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockFetchResponse(200, ISSUED);
    await useAuthStore.getState().refresh();

    mockFetchResponse(200, ISSUED);
    await useAuthStore.getState().refresh();

    expect(global.fetch).toHaveBeenCalledTimes(2);
  });
});

describe('authStore.restore() joins the same de-dupe as refresh()', () => {
  // On a cold reload, `restore()` fires from the root layout at the same
  // moment several screens' queries mount with no access token yet (it's
  // memory-only and a reload wipes it) — each one 401s and the API
  // interceptor independently calls `refresh()` to recover. `restore()`
  // USED TO post its own separate /auth/refresh with the same stored
  // (single-use, rotating) token instead of joining `inFlightRefresh` —
  // see the file-level comment on that variable for why two holders of one
  // token is exactly the "superseded" replay scenario the backend revokes.
  // These pin that `restore()` shares the SAME in-flight call as a
  // concurrent `refresh()`, so there is only ever one network call no
  // matter which of the two callers fires first.
  it('makes exactly one network call when restore() races a concurrent refresh()', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockLoadPersistedUser.mockResolvedValue(null);
    mockFetchResponse(200, ISSUED);

    await Promise.all([
      useAuthStore.getState().restore(),
      useAuthStore.getState().refresh(),
    ]);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().status).toBe('authenticated');
    expect(useAuthStore.getState().accessToken).toBe(ISSUED.accessToken);
    expect(mockClearAll).not.toHaveBeenCalled();
  });

  it('makes exactly one network call when a concurrent refresh() races restore()', async () => {
    // Same race, opposite call order — the de-dupe must not depend on
    // which of the two happens to reach the network first.
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockLoadPersistedUser.mockResolvedValue(null);
    mockFetchResponse(200, ISSUED);

    await Promise.all([
      useAuthStore.getState().refresh(),
      useAuthStore.getState().restore(),
    ]);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().status).toBe('authenticated');
  });

  it('still restores correctly when nothing else is racing it', async () => {
    mockLoadRefreshToken.mockResolvedValue('stored-refresh-token');
    mockLoadPersistedUser.mockResolvedValue(null);
    mockFetchResponse(200, ISSUED);

    await useAuthStore.getState().restore();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().status).toBe('authenticated');
    expect(useAuthStore.getState().accessToken).toBe(ISSUED.accessToken);
  });
});
