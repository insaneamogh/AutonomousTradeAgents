/**
 * Smoke tests for the trading-lock primitives in api.ts. These exist
 * primarily to prove the Jest runner is wired up correctly end-to-end
 * (jest-expo preset, Babel transform, the `@/*` alias) with real
 * assertions rather than a trivially-passing placeholder — see
 * fable5findings.md build log.
 *
 * Deliberately does not touch `request()` itself — that needs a fetch
 * mock and is real test-suite scope, not a smoke test.
 */
import {
  ApiError,
  TradingLockedError,
  isTradingUnlocked,
  request,
  setTradingUnlocked,
} from '@/lib/api';

describe('ApiError', () => {
  it('carries the HTTP status and body, and defaults its message', () => {
    const err = new ApiError(404, { detail: 'not found' });
    expect(err.name).toBe('ApiError');
    expect(err.status).toBe(404);
    expect(err.body).toEqual({ detail: 'not found' });
    expect(err.message).toBe('HTTP 404');
  });

  it('accepts an explicit message override', () => {
    const err = new ApiError(500, null, 'boom');
    expect(err.message).toBe('boom');
  });
});

describe('TradingLockedError', () => {
  it('is a named Error subclass carrying the lock reason', () => {
    const err = new TradingLockedError('Biometric verification required.');
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe('TradingLockedError');
    expect(err.message).toBe('Biometric verification required.');
  });
});

describe('trading lock state', () => {
  afterEach(() => {
    // Don't leak lock state into other test files.
    setTradingUnlocked(false);
  });

  it('starts locked and reflects setTradingUnlocked', () => {
    expect(isTradingUnlocked()).toBe(false);
    setTradingUnlocked(true);
    expect(isTradingUnlocked()).toBe(true);
    setTradingUnlocked(false);
    expect(isTradingUnlocked()).toBe(false);
  });
});

/**
 * `retryOnNetworkError` — the fix for "Couldn't reach the agent server" on
 * the first council run.
 *
 * The blip itself (a container restarting mid-click) was always transient;
 * what made it VISIBLE is that TanStack retries queries twice and mutations
 * zero times, so every other call on the screen healed and this one did not.
 */
describe('request(): network-error retry', () => {
  const realFetch = global.fetch;
  beforeEach(() => {
    // `/agent/run` and `/orders/execute` are TRADING_PATHS, so the biometric
    // lock refuses them before fetch is ever reached. Unlock to exercise the
    // transport — the lock itself is covered above.
    setTradingUnlocked(true);
  });
  afterEach(() => {
    global.fetch = realFetch;
    setTradingUnlocked(false);
  });

  const ok = () =>
    ({ ok: true, status: 200, text: async () => JSON.stringify({ runId: 'r1' }) }) as Response;

  it('retries once when fetch rejects and the option is set', async () => {
    const fetchMock = jest
      .fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(ok());
    global.fetch = fetchMock as unknown as typeof fetch;

    const out = await request<{ runId: string }>('/api/v1/agent/run/start', {
      method: 'POST',
      skipAuth: true,
      retryOnNetworkError: true,
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(out.runId).toBe('r1');
  });

  it('does NOT retry when the option is absent — the default for mutations', async () => {
    const fetchMock = jest.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(
      request('/api/v1/orders/execute/p1', { method: 'POST', skipAuth: true }),
    ).rejects.toThrow('Failed to fetch');
    // The whole safety property: an order-placing call is never re-sent.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('retries at most once, then surfaces the failure', async () => {
    const fetchMock = jest.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(
      request('/api/v1/agent/run/start', {
        method: 'POST',
        skipAuth: true,
        retryOnNetworkError: true,
      }),
    ).rejects.toThrow('Failed to fetch');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('never retries a caller-initiated abort', async () => {
    const abort = new Error('aborted');
    abort.name = 'AbortError';
    const fetchMock = jest.fn().mockRejectedValue(abort);
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(
      request('/api/v1/agent/run/start', {
        method: 'POST',
        skipAuth: true,
        retryOnNetworkError: true,
      }),
    ).rejects.toThrow('aborted');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not retry a response that arrived, whatever its status', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: false,
      status: 502,
      text: async () => '<html>bad gateway</html>',
    } as Response);
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(
      request('/api/v1/agent/run/start', {
        method: 'POST',
        skipAuth: true,
        retryOnNetworkError: true,
      }),
    ).rejects.toMatchObject({ status: 502 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
