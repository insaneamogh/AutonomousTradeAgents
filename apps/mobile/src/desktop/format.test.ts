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
import { pendingAwareCaption, pendingAwareUsd, stillMarkingCaption, usd } from './format';

describe('pendingAwareUsd', () => {
  it('renders a real, non-zero amount normally even if some marks are still pending', () => {
    // A bucket can have BOTH finalized dollars and still-marking rows at
    // once — the pending explainer must never hide a real number.
    expect(pendingAwareUsd(1234, 3)).toBe(usd(1234));
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
  it('omits the pending clause entirely once nothing is left marking', () => {
    expect(stillMarkingCaption(10, 0)).toBe('10 finalised');
    expect(stillMarkingCaption(10, 0, 3)).toBe('10 finalised');
  });

  it('falls back to the bare "still marking" count when the countdown is not supplied', () => {
    expect(stillMarkingCaption(6, 2)).toBe('6 finalised · 2 still marking');
  });

  it('names a concrete countdown alongside the still-marking count', () => {
    expect(stillMarkingCaption(6, 2, 3)).toBe(
      '6 finalised · 2 still marking — finalizes in 3 trading days',
    );
  });

  it('reads "finalizes any day now" instead of a zero countdown', () => {
    expect(stillMarkingCaption(0, 2, 0)).toBe('0 finalised · 2 still marking — finalizes any day now');
  });
});
