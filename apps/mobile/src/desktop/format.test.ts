/**
 * format.ts — pure formatting helpers, no React. Focused here on
 * `pendingAwareUsd` / `pendingAwareCaption` / `stillMarkingCaption`: a user
 * reacted to the Dashboard's "Risk saved $— · 6 marks pending" tile as
 * looking broken or unbuilt, when the underlying ghost-eval measurement is
 * real and simply hasn't finalized yet (docs/IMPL_REFUSAL_LEDGER.md). These
 * functions are what turns a bare pending count into something that names
 * WHEN the next mark resolves, using
 * `GhostBucketDto.oldestPendingRemainingTradingDays` from the API.
 *
 * The finalize countdown deliberately lives in the CAPTION helpers, never
 * in `pendingAwareUsd` itself — verified live (a static render of the real
 * theme.ts CSS at the actual 3-column Dashboard tile width) that the long
 * form wraps the tile's big value line across three lines, since `.pg-num`
 * has no truncation rule the way a caption's `.pg-truncate` does.
 */
import {
  emptyBucketCaption,
  ghostSideUsd,
  pendingAwareCaption,
  pendingAwareUsd,
  riskProfileCaption,
  stillMarkingCaption,
  usd,
} from './format';

describe('pendingAwareUsd', () => {
  it('renders a real, non-zero amount normally even if some marks are still pending', () => {
    // A bucket can have BOTH finalized dollars and still-marking rows at
    // once — the pending explainer must never hide a real number.
    expect(pendingAwareUsd(1234, 3)).toBe(usd(1234));
  });

  it('shows the marks-so-far figure instead of "$—" when the API has one', () => {
    // The Refusal Ledger's whole claim is measured in dollars. A ghost
    // finalizes only after `horizonDays` TRADING days, so for the first
    // week EVERY row is pending and the tile was a bare "$—" while the
    // table already held real marks. Labelled "so far" so a provisional
    // mark can never read as the settled number.
    expect(pendingAwareUsd(0, 6, 163.03)).toBe('$163 so far · 6 still marking');
  });

  it('keeps the bare pending placeholder when there is no mark yet either', () => {
    expect(pendingAwareUsd(0, 6, 0)).toBe('$— · 6 marks pending');
    expect(pendingAwareUsd(0, 6, null)).toBe('$— · 6 marks pending');
  });

  it('renders a genuine zero normally when nothing is pending', () => {
    expect(pendingAwareUsd(0, 0)).toBe(usd(0));
  });

  it('renders the bare pending count, singular and plural', () => {
    expect(pendingAwareUsd(0, 6)).toBe('$— · 6 marks pending');
    expect(pendingAwareUsd(null, 1)).toBe('$— · 1 mark pending');
  });
});

describe('pendingAwareCaption', () => {
  it('returns the fallback unchanged once nothing is pending', () => {
    expect(pendingAwareCaption('Losses avoided by vetoes · 30d', 0, 0)).toBe(
      'Losses avoided by vetoes · 30d',
    );
    expect(pendingAwareCaption('Losses avoided by vetoes · 30d', 0, 0, 3)).toBe(
      'Losses avoided by vetoes · 30d',
    );
  });

  it('returns the fallback unchanged when a real, nonzero figure is already showing', () => {
    // 8 of 10 vetoed rows already finalized into a real $500 -- the
    // caption must not claim the whole tile is unresolved next to a
    // value that plainly isn't "$—" (isShowingPending's whole point).
    expect(pendingAwareCaption('Losses avoided by vetoes · 30d', 500, 2, 3)).toBe(
      'Losses avoided by vetoes · 30d',
    );
  });

  it('returns the fallback unchanged when the countdown is not known', () => {
    expect(pendingAwareCaption('Losses avoided by vetoes · 30d', 0, 6)).toBe(
      'Losses avoided by vetoes · 30d',
    );
    expect(pendingAwareCaption('Losses avoided by vetoes · 30d', 0, 6, null)).toBe(
      'Losses avoided by vetoes · 30d',
    );
  });

  it('swaps in a concrete, capitalized countdown exactly when the value is showing "$—"', () => {
    expect(pendingAwareCaption('Losses avoided by vetoes · 30d', 0, 6, 3)).toBe(
      'Finalizes in 3 trading days',
    );
    expect(pendingAwareCaption('Losses avoided by vetoes · 30d', null, 6, 3)).toBe(
      'Finalizes in 3 trading days',
    );
  });

  it('uses the singular "trading day" for exactly one remaining', () => {
    expect(pendingAwareCaption('Missed on declined picks · 30d', 0, 2, 1)).toBe(
      'Finalizes in 1 trading day',
    );
  });

  it('reads "Finalizes any day now" instead of a zero or negative countdown', () => {
    expect(pendingAwareCaption('Missed on declined picks · 30d', 0, 4, 0)).toBe(
      'Finalizes any day now',
    );
  });
});

describe('stillMarkingCaption', () => {
  // First argument is the bucket TOTAL (`GhostBucketDto.count`) — that is
  // what both call sites pass — so the finalised subset is total minus
  // pending. Reading it as "already finalised" is what produced the live
  // "6 finalised · 6 still marking" for a bucket where nothing had.
  it('omits the pending clause entirely once nothing is left marking', () => {
    expect(stillMarkingCaption(10, 0)).toBe('10 finalised');
    expect(stillMarkingCaption(10, 0, 3)).toBe('10 finalised');
  });

  it('falls back to the bare "still marking" count when the countdown is not supplied', () => {
    expect(stillMarkingCaption(8, 2)).toBe('6 finalised · 2 still marking');
  });

  it('names a concrete countdown alongside the still-marking count', () => {
    expect(stillMarkingCaption(8, 2, 3)).toBe(
      '6 finalised · 2 still marking — finalizes in 3 trading days',
    );
  });

  it('reports zero finalised when every row in the bucket is still marking', () => {
    // The live Insights render on 2026-09-01: six vetoed ghosts, all
    // `partial`, and the caption claimed all six had finalised while the
    // value beside it read "$—".
    expect(stillMarkingCaption(6, 6, 3)).toBe(
      '0 finalised · 6 still marking — finalizes in 3 trading days',
    );
  });

  it('reads "finalizes any day now" instead of a zero countdown', () => {
    expect(stillMarkingCaption(0, 2, 0)).toBe('0 finalised · 2 still marking — finalizes any day now');
  });
});

describe('riskProfileCaption', () => {
  // The real 2026-09-01 fix this covers: the API never sent `riskProfile`
  // at all (always undefined in production), AND separately the aggressive
  // caption text itself still said "12%" a full day after the real cap
  // dropped to 7.5% — this test would have caught either regression.
  it('names the real caps for the aggressive profile', () => {
    expect(riskProfileCaption('aggressive_paper')).toBe('under the 1.5%/11% aggressive caps');
  });

  it('names the real caps for the conservative profile', () => {
    expect(riskProfileCaption('conservative')).toBe('under the 1%/5% conservative caps');
  });

  it('falls back to a generic disclosure only when the profile is genuinely absent', () => {
    expect(riskProfileCaption(undefined)).toBe('risk profile disclosure pending');
    expect(riskProfileCaption(null)).toBe('risk profile disclosure pending');
  });

  it('names an unrecognised profile literally rather than silently hiding it', () => {
    expect(riskProfileCaption('some_future_profile')).toBe('under the "some_future_profile" risk profile');
  });
});

describe('ghostSideUsd', () => {
  it('shows the un-netted side when the settled figure floored to zero', () => {
    // The live 2026-09-04 case: 101 real marks priced from Alpaca option
    // quotes held $30,788 of avoided losses AND $32,967 of blocked gains,
    // netting +$2,179. `savedUsd = max(0, -2179) = 0` floored the tile to
    // "$—" — rendering "our vetoes cost us money" identically to "no data".
    expect(ghostSideUsd(0, 30788)).toBe(usd(30788));
  });

  it('prefers the settled figure when there is one', () => {
    expect(ghostSideUsd(1234, 9999)).toBe(usd(1234));
  });

  it('returns null when there is genuinely nothing on either side', () => {
    expect(ghostSideUsd(0, 0)).toBeNull();
    expect(ghostSideUsd(null, null)).toBeNull();
    expect(ghostSideUsd(undefined, undefined)).toBeNull();
  });
});

describe('emptyBucketCaption', () => {
  it('names an empty bucket rather than letting it read as a measured zero', () => {
    // `declined` has zero rows because auto-approve is on and nothing is
    // ever declined by hand — that is absent data, not a $0 result.
    expect(emptyBucketCaption(0, 'Nothing declined by hand')).toBe(
      'Nothing declined by hand',
    );
  });

  it('defers to the normal caption once the bucket has rows', () => {
    expect(emptyBucketCaption(4, 'Nothing declined by hand')).toBeNull();
  });
});
