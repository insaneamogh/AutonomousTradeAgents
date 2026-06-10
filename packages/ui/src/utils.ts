/**
 * Tiny helpers used across the UI primitives. Keep this file small.
 */

import type { ThemeMode } from './tokens';
import { palette } from './tokens';

/**
 * Join className strings, dropping nulls / undefineds / falses. Behaves like
 * `clsx` but with zero deps. Order matters — later wins for Tailwind utilities
 * thanks to NativeWind's resolution order.
 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

/**
 * Resolve a palette token for the active theme. Use this when you need a hex
 * value at runtime — e.g. for SVG fill props that don't accept className.
 */
export function color(theme: ThemeMode, token: keyof typeof palette.light): string {
  return palette[theme][token];
}

/**
 * Format a USD amount with proper thousand separators + 2dp. Returns the
 * string without currency symbol — callers prepend $ themselves so colour
 * can wrap separately.
 */
export function formatUsd(value: number, fractionDigits: number = 2): string {
  return value.toLocaleString('en-US', {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

/**
 * Format a signed percentage with two decimals and an explicit + or − sign.
 * Returns the bare string — colour is the caller's job.
 */
export function formatPct(value: number, fractionDigits: number = 2): string {
  const sign = value > 0 ? '+' : value < 0 ? '−' : '';
  return `${sign}${Math.abs(value).toFixed(fractionDigits)}%`;
}

/**
 * Convert a duration in seconds into mm:ss. Used by the ApprovalCard countdown.
 */
export function formatMmSs(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(safe / 60);
  const s = safe % 60;
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

/**
 * Relative timestamp like "2 min ago", "1 h ago". Coarse — fine for the
 * "agent activity" feed where precision isn't useful. Accepts ISO strings
 * or Date objects.
 */
export function formatRelative(
  then: Date | number | string,
  now: Date | number = Date.now(),
): string {
  const t =
    typeof then === 'string'
      ? new Date(then).getTime()
      : typeof then === 'number'
        ? then
        : then.getTime();
  const n = typeof now === 'number' ? now : now.getTime();
  const sec = Math.max(0, Math.floor((n - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
  return `${Math.floor(sec / 86400)} d ago`;
}

/**
 * Convert an ISO-8601 string to seconds remaining until that moment.
 * Negative becomes 0. Used by the mobile layer to feed the ApprovalCard's
 * countdown without passing the raw Date into the component.
 */
export function secondsUntil(iso: string | undefined, now: number = Date.now()): number {
  if (!iso) return 0;
  return Math.max(0, Math.floor((new Date(iso).getTime() - now) / 1000));
}
