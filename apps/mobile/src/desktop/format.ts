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

const RISK_PROFILE_CAPTIONS: Record<string, string> = {
  conservative: 'under the 1%/5% conservative caps',
  aggressive_paper: 'under the 1.5%/11% aggressive caps',
};

/** Verified against `RiskCaps.aggressive_paper()` (packages/engine/engine/risk/types.py):
 * `options_max_premium_pct` 1.0→1.5 and `options_max_total_premium_pct` 5.0→11.0.
 *
 * This string is shown to judges, so it has to track the code. It has drifted
 * twice already: it read 12.0 for a day (fable5findings.md 2026-09-01
 * `ebfc8718`), and on 2026-09-04 it still said "2.5%/7.5%" when the real caps
 * were 1.5%/7.5% — the single cap had been tightened and nothing updated the
 * caption. If you change either number in `aggressive_paper`, change it here. */
export function riskProfileCaption(profile: string | null | undefined): string {
  if (!profile) return 'risk profile disclosure pending';
  return RISK_PROFILE_CAPTIONS[profile] ?? `under the "${profile}" risk profile`;
}

/** "finalizes in 3 trading days" / "finalizes any day now" / "" when the
 * remaining count isn't known. A pending count on its own reads as
 * "broken or unbuilt" to a user with no context — this names WHEN it
 * resolves, using the same trading-day count `ghost_eval` itself uses to
 * decide when a mark actually finalizes (`GhostBucketDto
 * .oldestPendingRemainingTradingDays`), so it can never promise a date the
 * backend doesn't also believe.
 *
 * Deliberately never inlined into `pendingAwareUsd`'s VALUE string: `.pg-num`
 * (the big Numeral the value renders into) has no `white-space`/truncation
 * rule, unlike a caption's `.pg-truncate` — verified live in a static render
 * of the actual theme.ts CSS at the real 3-column Dashboard tile width, the
 * full clause wraps the value across three lines and blows out the tile's
 * height next to its siblings. A caption degrades gracefully (ellipsis);
 * the value line does not. So this phrase belongs only in captions —
 * `pendingAwareCaption` and `stillMarkingCaption` below. */
function finalizesPhrase(remainingTradingDays: number | null | undefined): string {
  if (remainingTradingDays == null) return '';
  if (remainingTradingDays <= 0) return 'finalizes any day now';
  return `finalizes in ${remainingTradingDays} trading day${remainingTradingDays === 1 ? '' : 's'}`;
}

/** True exactly when `pendingAwareUsd` would render "$— · N pending"
 * instead of a real number: pending rows exist AND no finalized ones have
 * contributed a nonzero figure yet. Shared with `pendingAwareCaption` so
 * the value and its caption can never disagree about whether this bucket
 * is showing a real number or a placeholder — e.g. a bucket with 8
 * finalized rows and 2 still marking already has a real (if incomplete)
 * dollar figure, and the caption must not claim the whole tile is
 * unresolved next to a value that plainly isn't `$—`. */
function isShowingPending(amount: number | null | undefined, pendingCount: number): boolean {
  return pendingCount > 0 && (amount == null || amount === 0);
}

/** `usd()`, except a legitimate `$0` next to at least one still-marking
 * ghost renders as "$— · N marks pending" instead — a completed $0 and an
 * unmeasured one must never look the same. Deliberately stays this short:
 * see `finalizesPhrase`'s docstring for why the finalize countdown lives in
 * the caption (`pendingAwareCaption`) instead of here. */
/** The two-sided ghost figure for a "Loss avoided" / "Upside blocked" tile.
 *
 * `savedUsd`/`missedUsd` are both `max(0, ±ghostPnl)` — they floor to zero
 * whenever the refusals netted the OTHER way, which then falls through to
 * `pendingAwareUsd`'s "$—" placeholder. On 2026-09-04 the vetoed bucket held
 * $30,788 of avoided losses and $32,967 of blocked gains against 101 real
 * marks priced from live Alpaca option quotes, netted to +$2,179, and the
 * tile showed "$—" — i.e. "our vetoes cost us money" rendered identically to
 * "we have no data".
 *
 * `sideUsd` is the un-netted half, so it is non-zero whenever ANY mark exists
 * on that side. Returns null when there is genuinely nothing to show, so the
 * caller can fall back to the pending placeholder. */
export function ghostSideUsd(
  settled: number | null | undefined,
  sideUsd: number | null | undefined,
): string | null {
  if (settled != null && settled !== 0) return usd(settled);
  if (sideUsd != null && sideUsd !== 0) return usd(sideUsd);
  return null;
}

/** Caption for a bucket that has no rows at all, as opposed to rows summing
 * to zero. An empty `declined` bucket (nothing was ever declined by hand —
 * auto-approve is on) rendered as a flat "$0", which reads as a measured
 * result rather than an absent one. */
export function emptyBucketCaption(count: number, fallback: string): string | null {
  return count === 0 ? fallback : null;
}

export function pendingAwareUsd(
  amount: number | null | undefined,
  pendingCount: number,
  markedAmount?: number | null,
): string {
  if (isShowingPending(amount, pendingCount)) {
    // A ghost finalizes only after `horizonDays` TRADING days, so for the
    // first week of any account's life EVERY row is still marking and this
    // tile is a bare "$—" — the Refusal Ledger, which is the whole claim,
    // showing nothing on exactly the days someone looks at it. When the
    // API has a marks-so-far figure (savedSoFarUsd / missedSoFarUsd),
    // show it, explicitly labelled "so far" so a provisional mark can
    // never be read as the settled number.
    if (markedAmount != null && markedAmount !== 0) {
      return `${usd(markedAmount)} so far · ${pendingCount} still marking`;
    }
    return `$— · ${pendingCount} mark${pendingCount === 1 ? '' : 's'} pending`;
  }
  return usd(amount);
}

/** A StatTile's caption, swapped for a concrete finalize countdown
 * ("Finalizes in 3 trading days") exactly when `pendingAwareUsd` (given
 * the SAME `amount`/`pendingCount`) is showing a pending placeholder
 * rather than a real number — see `isShowingPending`. `fallback` — the
 * tile's normal, static caption — comes back unchanged otherwise: nothing
 * pending, a real number already showing despite some rows still
 * marking, or the countdown not being known (e.g. an older API
 * response). */
export function pendingAwareCaption(
  fallback: string,
  amount: number | null | undefined,
  pendingCount: number,
  oldestPendingRemainingTradingDays?: number | null,
): string {
  if (!isShowingPending(amount, pendingCount)) return fallback;
  const phrase = finalizesPhrase(oldestPendingRemainingTradingDays);
  return phrase ? phrase.charAt(0).toUpperCase() + phrase.slice(1) : fallback;
}

/** "{count} finalised" / "{count} finalised · {pendingCount} still
 * marking — finalizes in N trading days" — the ghost-bucket caption used
 * by both Dashboard's Ghost P&L card and Insights' Ghost P&L tab. Pulled
 * out to one function so the two screens say the same thing about the
 * same number: Dashboard previously said "still open" here while Insights
 * said "still marking" for the identical `pendingCount`. Unlike
 * `pendingAwareCaption`, this renders as `pg-body-sm`/`pg-caption` text
 * with no `.pg-truncate` — verified live it wraps onto a second line
 * cleanly inside the Ghost P&L card's wider inset row, so the fuller
 * clause is safe here. */
export function stillMarkingCaption(
  count: number,
  pendingCount: number,
  oldestPendingRemainingTradingDays?: number | null,
): string {
  if (pendingCount <= 0) return `${count} finalised`;
  // `count` is the bucket TOTAL, not its finalized subset — both call
  // sites pass `bucket.count`. Rendering it as "N finalised" produced
  // "6 finalised · 6 still marking" for a bucket where NOTHING had
  // finalised, which is a straight contradiction sitting next to a "$—"
  // value. The finalized subset is what's left after the pending ones.
  const finalised = Math.max(0, count - pendingCount);
  const phrase = finalizesPhrase(oldestPendingRemainingTradingDays);
  return `${finalised} finalised · ${pendingCount} still marking${phrase ? ` — ${phrase}` : ''}`;
}
