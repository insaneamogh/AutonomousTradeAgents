/**
 * ScanFunnel tests — mirrors ContractFunnel.test.tsx's style exactly:
 * react-test-renderer, no QueryClientProvider, since this is a dumb,
 * props-only component that never fetches.
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestInstance, ReactTestRenderer } from 'react-test-renderer';
import type { ScanFunnelSweepDto, ScanFunnelUniverseDto } from '@app/shared-types';

import { ScanFunnel } from './ScanFunnel';
import type { ScanFunnelProps } from './ScanFunnel';

interface BarFillStyle {
  width: string;
  minWidth: number;
  backgroundColor: string;
}

function fillStyle(node: ReactTestInstance): BarFillStyle {
  return (node.props as { style: BarFillStyle }).style;
}

function barStageOrder(tree: ReactTestRenderer): string[] {
  return tree.root
    .findAll((node) => typeof node.props['data-stage'] === 'string')
    .map((node) => node.props['data-stage'] as string);
}

const UNIVERSE: ScanFunnelUniverseDto = {
  eligibleCount: 1024,
  examinedCount: 178,
  refreshedAt: '2026-09-02T04:00:00Z',
};

const SWEEP_BASELINE: ScanFunnelSweepDto = {
  kind: 'baseline',
  watchlistSize: 110,
  clearedMath: 34,
  admittedToLlm: 20,
  cappedBreakdown: { llm_daily_symbol_cap_reached: 14 },
  generatedAt: '2026-09-02T15:00:00Z',
};

function renderScanFunnel(props: ScanFunnelProps): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(<ScanFunnel {...props} />);
  });
  return tree;
}

describe('ScanFunnel', () => {
  it('renders all four tiers in order with the correct dropped counts', () => {
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: SWEEP_BASELINE });
    expect(barStageOrder(tree)).toEqual([
      'eligible_universe',
      'examined_this_sweep',
      'cleared_math',
      'admitted_to_llm',
    ]);

    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('1,024');
    // examined_this_sweep: 1024 -> 110, dropped 914
    expect(text).toContain('−914');
    // cleared_math: 110 -> 34, dropped 76
    expect(text).toContain('−76');
    // admitted_to_llm: 34 -> 20, dropped 14
    expect(text).toContain('−14');
  });

  it('gives the base (first) tier no minWidth floor requirement but a real bar', () => {
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: SWEEP_BASELINE });
    const first = tree.root.find((node) => node.props['data-stage'] === 'eligible_universe');
    expect(fillStyle(first).width).not.toBe('0%');
  });

  it('captions the first bar with the once-daily examinedCount and refresh time, not a 5th bar', () => {
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: SWEEP_BASELINE });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('178 examined in the most recent daily refresh');
    expect(barStageOrder(tree)).toHaveLength(4);
  });

  it('lists the capped breakdown with a known reason\'s plain-English label', () => {
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: SWEEP_BASELINE });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('Daily LLM symbol cap reached');
    expect(text).toContain('14');
  });

  it('falls back to the raw skip_reason string for an unmapped reason', () => {
    const sweep: ScanFunnelSweepDto = { ...SWEEP_BASELINE, cappedBreakdown: { some_future_reason: 3 } };
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('some_future_reason');
  });

  it('captions a triggered sweep\'s narrow sample distinctly from a baseline one', () => {
    const triggered: ScanFunnelSweepDto = { ...SWEEP_BASELINE, kind: 'triggered', watchlistSize: 2, clearedMath: 1, admittedToLlm: 1 };
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: triggered });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('TRIGGERED');
    expect(text).toContain('NARROW SAMPLE');
  });

  it('labels a baseline sweep as such', () => {
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: SWEEP_BASELINE });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('BASELINE SWEEP');
  });

  it('never fabricates a zero eligible-universe bar when the universe refresh has not run', () => {
    const noUniverse: ScanFunnelUniverseDto = { eligibleCount: null, examinedCount: null, refreshedAt: null };
    const tree = renderScanFunnel({ universe: noUniverse, sweep: SWEEP_BASELINE });

    // No eligible_universe bar at all -- not a real bar, and not a
    // zero-width fake one either. examined_this_sweep becomes the base.
    expect(barStageOrder(tree)).toEqual(['examined_this_sweep', 'cleared_math', 'admitted_to_llm']);
    const base = tree.root.find((node) => node.props['data-stage'] === 'examined_this_sweep');
    // As the base tier it drops nothing (there is nothing earlier to
    // compare against), so its own bar renders full width, not "−110".
    const text = JSON.stringify(tree.toJSON());
    expect(text).not.toContain('−110');
    expect(fillStyle(base).width).toBe('100%');
  });

  it('shows an explicit empty state when no sweep has completed yet, never a funnel of zeroes', () => {
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: null });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('No sweep has completed yet');
    expect(barStageOrder(tree)).toEqual([]);
  });

  it('shows shimmer rows while loading, not the empty state', () => {
    const tree = renderScanFunnel({ universe: UNIVERSE, sweep: null, loading: true });
    const text = JSON.stringify(tree.toJSON());
    expect(text).not.toContain('No sweep has completed yet');
    expect(tree.root.findAll((node) => node.props.className === 'pg-skel')).toHaveLength(4);
  });
});
