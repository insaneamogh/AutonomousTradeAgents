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
  runErrorMessage,
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

  it('retries at each backoff step in order, then surfaces the failure once the cap is hit', async () => {
    // A single 1s retry (the original fix) was found to not reliably
    // outlast a real Railway cold start/redeploy — this pins the widened
    // budget: 2 retries at 1s then 2s, the SAME schedule TanStack Query's
    // own default already gives every query on the same screen
    // (queryClient.ts's `retry: 2`). Spying on setTimeout (rather than
    // waiting the real ~3s) both keeps this fast and proves the exact
    // delays used, not just the call count.
    const delays: number[] = [];
    const setTimeoutSpy = jest
      .spyOn(global, 'setTimeout')
      .mockImplementation(((fn: () => void, ms?: number) => {
        delays.push(ms ?? 0);
        fn();
        return 0 as unknown as ReturnType<typeof setTimeout>;
      }) as unknown as typeof setTimeout);

    const fetchMock = jest.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    global.fetch = fetchMock as unknown as typeof fetch;

    try {
      await expect(
        request('/api/v1/agent/run/start', {
          method: 'POST',
          skipAuth: true,
          retryOnNetworkError: true,
        }),
      ).rejects.toThrow('Failed to fetch');
      // 1 initial attempt + 2 retries.
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(delays).toEqual([1_000, 2_000]);
    } finally {
      setTimeoutSpy.mockRestore();
    }
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

/**
 * `runErrorMessage` — ONE shared implementation for "what do we tell the
 * user about this failed call", imported by the desktop council launcher,
 * the phone "Run" button, and the theater screen. It used to be a
 * module-private copy inside the desktop screen only (untested, since
 * nothing outside that file could reach it) — the phone and theater
 * screens each grew their OWN hardcoded string instead of sharing it, and
 * those copies never got the fix this file already had. Promoting it here
 * is what makes that class of drift structurally impossible going forward.
 */
describe('runErrorMessage', () => {
  it('prefers the server-provided string detail (own hand-raised 422s)', () => {
    const err = new ApiError(422, { detail: 'AMZN is not a tradable US equity or ETF.' });
    expect(runErrorMessage(err)).toBe('AMZN is not a tradable US equity or ETF.');
  });

  it('reads the first message out of a FastAPI validation-array detail', () => {
    const err = new ApiError(422, {
      detail: [{ loc: ['body', 'symbol'], msg: 'string does not match regex', type: 'value_error' }],
    });
    expect(runErrorMessage(err)).toBe('string does not match regex');
  });

  it('names the daily budget explicitly on 429', () => {
    const err = new ApiError(429, null);
    expect(runErrorMessage(err)).toBe('Daily council budget reached. Try again tomorrow.');
  });

  it('falls back to a generic status message for any other 4xx/5xx', () => {
    const err = new ApiError(500, null);
    expect(runErrorMessage(err)).toBe('The server refused the request (500). Try again.');
  });

  it("does not blame the caller's connection when the error carries no status", () => {
    // The shape `fetch()` rejecting actually produces — no `status`, no
    // `body`. This is the one this whole fix exists for: the old copy in
    // this exact spot used to read "check your connection and try again".
    expect(runErrorMessage(new TypeError('Failed to fetch'))).toBe(
      "The server didn't respond - it may still be starting up. Try again in a moment.",
    );
  });
});
