/** Pure formatting helpers for the desktop tree. No React, no tokens. */

import { vetoRuleLabels } from '@app/shared-types';

/** `$100,000` — whole dollars, thousands-separated. */
export function usd(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(value)) return '—';
  return value.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** `+$1,204` / `−$310` — signed, with a real minus sign. */
export function signedUsd(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(value)) return '—';
  const body = usd(Math.abs(value), digits);
  if (value > 0) return `+${body}`;
  if (value < 0) return `−${body}`;
  return body;
}

/** `+1.24%` / `−0.30%`. */
export function signedPct(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '—';
  const sign = value > 0 ? '+' : value < 0 ? '−' : '';
  return `${sign}${Math.abs(value).toFixed(digits)}%`;
}

/** Sign of a number as a tone name, for pill/text colouring. */
export function tone(value: number | null | undefined): 'bull' | 'bear' | 'neutral' {
  if (value == null || Number.isNaN(value) || value === 0) return 'neutral';
  return value > 0 ? 'bull' : 'bear';
}

/** `6h ago`, `3d ago`, `just now`. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 45) return 'just now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.round(days / 30)}mo ago`;
}

/** `14:32` local clock, for the theater log. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** `pdt_block` → `Pattern day trader rule`, via the shared veto-rule label
 * map. Falls back to `PDT BLOCK`-style uppercasing for anything not in the
 * map, so a truly unknown identifier still renders instead of crashing. */
export function ruleLabel(rule: string): string {
  return vetoRuleLabels[rule] ?? rule.replace(/_/g, ' ').toUpperCase();
}

/** Sentence-case a snake/kebab identifier: `risk_officer` → `Risk officer`. */
export function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
