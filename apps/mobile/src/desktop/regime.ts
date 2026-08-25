/**
 * Market regime for the ambient halo (§5.2/5.3) and the Market Mode tile.
 *
 * Composes the EXISTING hooks — no new fetching, no new endpoints. Two
 * honest sources, in priority order:
 *
 *   1. The regime the council actually recorded on its most recent graded
 *      decision (`useReviewQueue` items carry `regime`).
 *   2. Today's portfolio P&L sign, when the council has never run.
 *
 * `label` says which one it is, so the tile never implies more certainty
 * than the data supports.
 */

import type { Regime } from './theme';
import { useAccount } from '@/hooks/useAccount';
import { useReviewQueue } from '@/hooks/useReview';

export interface RegimeRead {
  regime: Regime;
  /** Display value for the Market Mode tile. */
  label: string;
  /** One-line provenance hint under the value. */
  caption: string;
  loading: boolean;
}

/** Map a free-form council regime string onto the three ambient tones. */
function toneOf(raw: string): Regime {
  const value = raw.toLowerCase();
  if (value.includes('bull') || value.includes('risk_on') || value.includes('risk-on')) return 'bull';
  if (value.includes('bear') || value.includes('risk_off') || value.includes('risk-off')) return 'bear';
  return 'neutral';
}

export function useRegime(): RegimeRead {
  const account = useAccount();
  const review = useReviewQueue(30);

  const fromCouncil = review.data?.items.find((item) => item.regime != null)?.regime ?? null;

  if (fromCouncil) {
    return {
      regime: toneOf(fromCouncil),
      label: fromCouncil.replace(/[_-]+/g, ' ').toUpperCase(),
      caption: 'Last council regime read',
      loading: false,
    };
  }

  const pct = account.data?.todayPnlPct ?? null;
  if (account.isLoading || (review.isLoading && pct == null)) {
    return { regime: 'neutral', label: '—', caption: '', loading: true };
  }

  const regime: Regime = pct == null || pct === 0 ? 'neutral' : pct > 0 ? 'bull' : 'bear';
  return {
    regime,
    label: regime === 'bull' ? 'RISK ON' : regime === 'bear' ? 'RISK OFF' : 'FLAT',
    caption: 'Derived from today’s portfolio P&L',
    loading: false,
  };
}
