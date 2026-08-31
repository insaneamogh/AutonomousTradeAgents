/**
 * ContractFunnel tests — rendered via react-test-renderer, this repo's
 * established style for a dumb, props-only component (see
 * SymbolResultsList.test.tsx). No React Query mocking needed: the
 * component never fetches, it only renders what it's given.
 *
 * Fixture numbers match docs/IMPL_CONTRACT_FUNNEL_UI.md §0's worked
 * example verbatim (4,128 -> 2,064 -> 1,843 -> 130 -> 3 -> 3 -> 1) so a
 * reader can cross-check the test against the spec directly.
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestInstance, ReactTestRenderer } from 'react-test-renderer';
import type { FunnelStageDto } from '@app/shared-types';

import { ContractFunnel } from './ContractFunnel';
import type { ContractFunnelProps } from './ContractFunnel';

/** `ReactTestInstance.props` is typed `any` — one narrowing cast here
 * keeps every call site free of unsafe-member-access instead of each
 * repeating an inline `as`. */
interface BarFillStyle {
  width: string;
  minWidth: number;
  backgroundColor: string;
}

function fillStyle(node: ReactTestInstance): BarFillStyle {
  return (node.props as { style: BarFillStyle }).style;
}

// Fixed order — matches `_STAGE_REJECTION_REASONS`'s insertion order in
// packages/engine/engine/options/selection.py:180 (total isn't in that
// map, it's the ungated chain count the first real stage drops from).
const STAGE_KEYS_IN_ORDER = [
  'total',
  'contract_type',
  'dte_window',
  'delta_band',
  'liquidity',
  'iv_present',
  'iv_realized_vol_band',
];

const BOUGHT_STAGES: FunnelStageDto[] = [
  { key: 'total', label: 'Contracts in chain', survivors: 4128, dropped: 0 },
  { key: 'contract_type', label: 'Calls (or puts)', survivors: 2064, dropped: 2064 },
  { key: 'dte_window', label: '10–45 DTE', survivors: 1843, dropped: 221 },
  { key: 'delta_band', label: 'In the delta band', survivors: 130, dropped: 1713 },
  { key: 'liquidity', label: 'OI ≥ 100 · spread ≤ 12%', survivors: 3, dropped: 127 },
  { key: 'iv_present', label: 'IV reported', survivors: 3, dropped: 0 },
  { key: 'iv_realized_vol_band', label: 'IV sane vs realized', survivors: 1, dropped: 2 },
];

// Same funnel, but delta_band emptied it — a HOLD run.
const HELD_STAGES: FunnelStageDto[] = [
  { key: 'total', label: 'Contracts in chain', survivors: 4128, dropped: 0 },
  { key: 'contract_type', label: 'Calls (or puts)', survivors: 2064, dropped: 2064 },
  { key: 'dte_window', label: '10–45 DTE', survivors: 1843, dropped: 221 },
  { key: 'delta_band', label: 'In the delta band', survivors: 0, dropped: 1843 },
  { key: 'liquidity', label: 'OI ≥ 100 · spread ≤ 12%', survivors: 0, dropped: 0 },
  { key: 'iv_present', label: 'IV reported', survivors: 0, dropped: 0 },
  { key: 'iv_realized_vol_band', label: 'IV sane vs realized', survivors: 0, dropped: 0 },
];

function renderFunnel(props: ContractFunnelProps): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(<ContractFunnel {...props} />);
  });
  return tree;
}

/** Every `<i data-stage="...">` bar fill, in render order. */
function barStageOrder(tree: ReactTestRenderer): string[] {
  return tree.root
    .findAll((node) => typeof node.props['data-stage'] === 'string')
    .map((node) => node.props['data-stage'] as string);
}

describe('ContractFunnel', () => {
  it('renders every stage in the fixed _STAGE_REJECTION_REASONS order', () => {
    const tree = renderFunnel({ stages: BOUGHT_STAGES, windowDays: 30, runs: 14, bought: 3 });
    expect(barStageOrder(tree)).toEqual(STAGE_KEYS_IN_ORDER);
  });

  it('never re-sorts a differently-ordered input — it trusts the array it is given', () => {
    // Guards the component itself, not just a well-behaved fixture: even
    // if fed stages out of order, it must render them in THAT order and
    // never silently re-sort (e.g. by survivor count descending).
    const shuffled = [BOUGHT_STAGES[3], BOUGHT_STAGES[0], BOUGHT_STAGES[6]];
    const tree = renderFunnel({ stages: shuffled });
    expect(barStageOrder(tree)).toEqual(['delta_band', 'total', 'iv_realized_vol_band']);
  });

  it('gives a 1-survivor stage a visible (minWidth-floored) bar', () => {
    const tree = renderFunnel({ stages: BOUGHT_STAGES, windowDays: 30, runs: 14, bought: 3 });
    const oneSurvivor = tree.root.find((node) => node.props['data-stage'] === 'iv_realized_vol_band');
    expect(BOUGHT_STAGES.find((s) => s.key === 'iv_realized_vol_band')?.survivors).toBe(1);
    expect(fillStyle(oneSurvivor).minWidth).toBe(2);
    expect(fillStyle(oneSurvivor).width).not.toBe('0%');
  });

  it('gives a 0-survivor stage no minWidth floor — only a real survivor is forced visible', () => {
    const tree = renderFunnel({ stages: HELD_STAGES, rejectionReason: 'no_delta_in_band' });
    const zeroSurvivor = tree.root.find((node) => node.props['data-stage'] === 'liquidity');
    expect(fillStyle(zeroSurvivor).minWidth).toBe(0);
  });

  it('names the rejection stage in plain English on a HOLD run', () => {
    const tree = renderFunnel({ stages: HELD_STAGES, rejectionReason: 'no_delta_in_band', symbol: 'NVDA' });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('HELD');
    expect(text).toContain('no_delta_in_band');
    // The interpolated count is the DTE-window stage's survivor count
    // (1,843), delivered here as delta_band's own `dropped` figure.
    expect(text).toContain('1,843');
    expect(text).toContain('contracts were in the DTE window');
  });

  it('colours the stage that hit zero, not some other stage', () => {
    const tree = renderFunnel({ stages: HELD_STAGES, rejectionReason: 'no_delta_in_band' });
    const deltaBand = tree.root.find((node) => node.props['data-stage'] === 'delta_band');
    expect(fillStyle(deltaBand).backgroundColor).toBe('var(--pg-bear)');
    const dteWindow = tree.root.find((node) => node.props['data-stage'] === 'dte_window');
    expect(fillStyle(dteWindow).backgroundColor).not.toBe('var(--pg-bear)');
  });

  it('shows an explicit empty state on a zero-run window, never a funnel of zeroes', () => {
    const tree = renderFunnel({ stages: [], windowDays: 30, runs: 0, bought: 0 });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('No options passes yet in this window');
    expect(barStageOrder(tree)).toEqual([]);
  });

  it('treats an aggregate reporting runs:0 as empty even if 7 zero-value stages are still sent', () => {
    // Defensive against a backend that sums to zero rather than omitting
    // stages entirely — `runs === 0` is the authoritative empty signal.
    const zeroStages = STAGE_KEYS_IN_ORDER.map((key) => ({ key, label: key, survivors: 0, dropped: 0 }));
    const tree = renderFunnel({ stages: zeroStages, windowDays: 30, runs: 0, bought: 0 });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('No options passes yet in this window');
    expect(barStageOrder(tree)).toEqual([]);
  });

  it('does not show the dropped column for the first (baseline) stage', () => {
    // Isolated from BOUGHT_STAGES on purpose: that fixture's own
    // iv_present row (3 -> 3) legitimately renders "−0" further down
    // (see the next test), so asserting its absence there would be
    // testing the wrong thing. This fixture's only zero-drop stage is
    // at index 0, so "−0" must not appear anywhere in it.
    const firstStageDropsNothingElseDoes: FunnelStageDto[] = [
      { key: 'total', label: 'Contracts in chain', survivors: 10, dropped: 0 },
      { key: 'contract_type', label: 'Calls (or puts)', survivors: 6, dropped: 4 },
      { key: 'dte_window', label: '10–45 DTE', survivors: 2, dropped: 4 },
    ];
    const tree = renderFunnel({ stages: firstStageDropsNothingElseDoes });
    const text = JSON.stringify(tree.toJSON());
    expect(text).not.toContain('−0');
  });

  it('renders "−0" for a real (non-first) stage that dropped nothing', () => {
    const soleZeroDrop: FunnelStageDto[] = [
      { key: 'total', label: 'Contracts in chain', survivors: 10, dropped: 0 },
      { key: 'contract_type', label: 'Calls (or puts)', survivors: 10, dropped: 0 },
    ];
    const tree = renderFunnel({ stages: soleZeroDrop });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('−0');
  });

  it('omits the aggregate "WE BOUGHT" footer on a single-run render', () => {
    const tree = renderFunnel({ stages: HELD_STAGES, rejectionReason: 'no_delta_in_band' });
    const text = JSON.stringify(tree.toJSON());
    expect(text).not.toContain('WE BOUGHT');
  });

  it('shows the aggregate "WE BOUGHT" footer with the given count', () => {
    const tree = renderFunnel({ stages: BOUGHT_STAGES, windowDays: 30, runs: 14, bought: 3 });
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain('WE BOUGHT 3');
  });
});
