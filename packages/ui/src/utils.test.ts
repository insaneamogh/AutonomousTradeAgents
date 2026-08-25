/**
 * Smoke tests for the tiny helpers in utils.ts. These exist primarily to
 * prove the Jest runner is wired up correctly (real assertions, not just
 * a trivially-passing placeholder) — see fable5findings.md build log.
 */
import { cn, color, formatMmSs, formatRelative, formatUsd, secondsUntil } from './utils';

describe('cn', () => {
  it('joins truthy class fragments with a space', () => {
    expect(cn('a', 'b', 'c')).toBe('a b c');
  });

  it('drops falsy fragments', () => {
    expect(cn('a', false, null, undefined, 'b')).toBe('a b');
  });

  it('returns an empty string when nothing is truthy', () => {
    expect(cn(false, null, undefined)).toBe('');
  });
});

describe('color', () => {
  it('resolves a palette token for the given theme', () => {
    expect(color('light', 'accentPrimary')).toBe('#1E40AF');
    expect(color('dark', 'accentPrimary')).toBe('#3B82F6');
  });
});

describe('formatUsd', () => {
  it('adds thousand separators and 2dp by default', () => {
    expect(formatUsd(1234.5)).toBe('1,234.50');
  });

  it('honors a custom fractionDigits', () => {
    expect(formatUsd(1234.5, 0)).toBe('1,235' /* rounds */);
  });
});

describe('formatMmSs', () => {
  it('formats whole minutes and seconds with zero padding', () => {
    expect(formatMmSs(65)).toBe('01:05');
  });

  it('clamps negative durations to 00:00', () => {
    expect(formatMmSs(-5)).toBe('00:00');
  });
});

describe('formatRelative', () => {
  const now = new Date('2026-01-01T00:10:00.000Z').getTime();

  it('renders seconds for sub-minute gaps', () => {
    expect(formatRelative(new Date('2026-01-01T00:09:30.000Z'), now)).toBe('30s ago');
  });

  it('renders minutes for sub-hour gaps', () => {
    expect(formatRelative(new Date('2026-01-01T00:00:00.000Z'), now)).toBe('10 min ago');
  });
});

describe('secondsUntil', () => {
  it('returns 0 when the ISO string is undefined', () => {
    expect(secondsUntil(undefined)).toBe(0);
  });

  it('returns 0 (not negative) once the deadline has passed', () => {
    const now = new Date('2026-01-01T00:10:00.000Z').getTime();
    expect(secondsUntil('2026-01-01T00:00:00.000Z', now)).toBe(0);
  });

  it('returns the whole seconds remaining before the deadline', () => {
    const now = new Date('2026-01-01T00:00:00.000Z').getTime();
    expect(secondsUntil('2026-01-01T00:00:30.000Z', now)).toBe(30);
  });
});
