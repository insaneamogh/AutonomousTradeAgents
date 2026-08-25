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
import { ApiError, TradingLockedError, isTradingUnlocked, setTradingUnlocked } from '@/lib/api';

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
