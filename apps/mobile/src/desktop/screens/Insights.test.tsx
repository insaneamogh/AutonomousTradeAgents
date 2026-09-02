/**
 * InsightsScreen — the veto ledger honesty rules from
 * docs/IMPL_REFUSAL_LEDGER.md §4 are the product, not styling:
 *   - a trim is never summed into the veto count, and renders in its own
 *     section (§4 rule 1);
 *   - a null ghost renders the literal word "pending", never `$0` (rule 3);
 *   - a ghost that would have MADE money renders amber "missed", not
 *     hidden next to the wins (rule 2);
 *   - clicking a rule opens the story-trade exemplar for it (§2.2).
 *
 * Data hooks are mocked wholesale — same style as
 * src/components/SymbolResultsList.test.tsx: react-test-renderer + act(),
 * no QueryClientProvider needed because the real useQuery never runs.
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';
import type { GhostSummaryResponse, VetoLedgerResponse } from '@app/shared-types';

import { useCalibrationScorecard } from '@/hooks/useCalibration';
import { useFunnel } from '@/hooks/useFunnel';
import { useGhostSummary, useVetoLedger } from '@/hooks/useInsights';
import { useScanFunnel } from '@/hooks/useScanFunnel';

import { Pill } from '../primitives';
import { InsightsScreen } from './Insights';

jest.mock('@/hooks/useInsights');
jest.mock('@/hooks/useCalibration');
jest.mock('@/hooks/useFunnel');
jest.mock('@/hooks/useScanFunnel');
jest.mock('../ExemplarCard', () => ({
  ExemplarCard: ({ rule }: { rule: string }) => `exemplar:${rule}`,
}));

const mockUseVetoLedger = useVetoLedger as jest.Mock;
const mockUseGhostSummary = useGhostSummary as jest.Mock;
const mockUseCalibrationScorecard = useCalibrationScorecard as jest.Mock;
// The Contract Funnel section (landed in a parallel commit, merged into
// this screen after this test file was first written) isn't what these
// honesty-rule tests are about — an empty, loaded result keeps it out of
// their way without asserting anything about it.
const mockUseFunnel = useFunnel as jest.Mock;
// Same treatment as mockUseFunnel above, for the sibling scan-funnel card.
const mockUseScanFunnel = useScanFunnel as jest.Mock;

const GHOST_SUMMARY: GhostSummaryResponse = {
  windowDays: 30,
  asOf: '2026-08-31T00:00:00Z',
  vetoed: {
    count: 10,
    ghostPnl: -500,
    pendingCount: 2,
    oldestPendingTriggeredAt: '2026-08-28T00:00:00Z',
    oldestPendingRemainingTradingDays: 3,
  },
  declined: {
    count: 4,
    ghostPnl: 0,
    pendingCount: 4,
    oldestPendingTriggeredAt: '2026-08-29T00:00:00Z',
    oldestPendingRemainingTradingDays: 1,
  },
  savedUsd: 500,
  missedUsd: 0,
};

// Rule ids chosen from packages/shared-types/src/vetoRuleLabels.json so the
// rendered label is a stable, known string rather than the uppercase
// fallback ruleLabel() produces for an unmapped identifier.
const LEDGER: VetoLedgerResponse = {
  windowDays: 30,
  totalVetoes: 13,
  totalBlockedNotional: 25700,
  rules: [
    {
      rule: 'pdt_block',
      count: 6,
      blockedNotional: 12400,
      ghostPnl: -340,
      preventedLossUsd: 340,
      lastAt: '2026-08-29T00:00:00Z',
    },
    {
      rule: 'sector_concentration',
      count: 4,
      blockedNotional: 8100,
      ghostPnl: null,
      preventedLossUsd: null,
      lastAt: null,
    },
    {
      rule: 'min_council_confidence',
      count: 3,
      blockedNotional: 5200,
      ghostPnl: 120,
      preventedLossUsd: 0,
      lastAt: null,
    },
  ],
  trims: [{ rule: 'max_position_pct_trim', count: 6 }],
  totalTrims: 6,
  riskProfile: 'aggressive_paper',
};

const SCORECARD = {
  windowDays: 180,
  agreementPct: 0,
  months: [],
  overrides: { count: 0, operatorWins: 0, reflectionWins: 0, operatorWinRatePct: 0 },
};

function renderScreen(): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(<InsightsScreen />);
  });
  return tree;
}

describe('InsightsScreen — veto ledger honesty rules', () => {
  beforeEach(() => {
    mockUseVetoLedger.mockReturnValue({ data: LEDGER, isLoading: false, isError: false, refetch: jest.fn() });
    mockUseGhostSummary.mockReturnValue({
      data: GHOST_SUMMARY,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    mockUseCalibrationScorecard.mockReturnValue({
      data: SCORECARD,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    mockUseFunnel.mockReturnValue({
      data: { windowDays: 30, aggregate: { stages: [], runs: 0, bought: 0, topRejectionReasons: [] }, recent: [] },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    mockUseScanFunnel.mockReturnValue({
      data: {
        universe: { eligibleCount: null, examinedCount: null, refreshedAt: null },
        sweep: null,
        chainPreflight: null,
        generatedAt: '2026-08-31T00:00:00Z',
      },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('never sums trims into the veto count', () => {
    const tree = renderScreen();
    const json = JSON.stringify(tree.toJSON());

    // 13 is the real totalVetoes; 19 would be totalVetoes + totalTrims.
    expect(json).toContain('13');
    expect(json).not.toContain('19');
  });

  it('renders trims in their own section, never inside the rules table', () => {
    const tree = renderScreen();
    const json = JSON.stringify(tree.toJSON());

    expect(json).toContain('Risk also shrank 6 trades');
    // "Position size trimmed" is the trim rule's label; none of the three
    // veto rules in this fixture map to it, so a match can only come from
    // the separate trims section.
    expect(json).toContain('Position size trimmed');
  });

  it('renders a null ghost as the literal word "pending", never $0', () => {
    const tree = renderScreen();

    const pendingSpans = tree.root.findAll(
      (node) => node.props?.className === 'pg-caption pg-dim' && node.props?.children === 'pending',
    );
    expect(pendingSpans.length).toBe(1);
  });

  it('renders a ghost that would have made money as amber "missed", not hidden', () => {
    const tree = renderScreen();

    const missedPills = tree.root.findAll(
      (node) => node.type === Pill && node.props.tone === 'warn' && node.props.children === 'missed',
    );
    expect(missedPills.length).toBe(1);
  });

  it('renders a ghost that would have lost money as green "saved"', () => {
    const tree = renderScreen();

    const savedPills = tree.root.findAll(
      (node) => node.type === Pill && node.props.tone === 'bull' && node.props.children === 'saved',
    );
    expect(savedPills.length).toBe(1);
  });

  it('stamps the risk profile in force as a caption', () => {
    const tree = renderScreen();
    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('under the 2.5%/7.5% aggressive caps');
  });

  it('clicking a rule row opens the story-trade exemplar for that rule', () => {
    const tree = renderScreen();

    const firstRow = tree.root.findAll(
      (node) => node.type === 'tr' && node.props.className === 'pg-row-btn',
    )[0];
    act(() => {
      firstRow.props.onClick();
    });

    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('exemplar:pdt_block');
  });

  // A user reacted to the Dashboard's identical tiles reading as broken —
  // "$— · N marks pending" alone gives no sense of whether the
  // measurement is real or just never built. These tiles live on the
  // Ghost P&L tab, so switch to it first.
  function openGhostTab(tree: ReactTestRenderer): void {
    const ghostTabButton = tree.root.findAll(
      (node) => node.type === 'button' && node.props.children === 'Ghost P&L',
    )[0];
    act(() => {
      ghostTabButton.props.onClick();
    });
  }

  it('keeps the "Upside missed" value line short and moves the finalize countdown to its caption', () => {
    const tree = renderScreen();
    openGhostTab(tree);

    const json = JSON.stringify(tree.toJSON());
    // declined.pendingCount=4, missedUsd=0 -> the StatTile's value takes
    // the pending branch of pendingAwareUsd, which must stay exactly this
    // short: verified live that a longer form wraps a 3-column Dashboard
    // tile's value line across three lines, so the countdown belongs only
    // in the caption (pendingAwareCaption), never appended here.
    expect(json).toContain('$— · 4 marks pending');
    expect(json).not.toContain('$— · 4 marks pending —');
    // oldestPendingRemainingTradingDays=1 must render the singular
    // "trading day" in the caption, capitalized as a standalone sentence.
    expect(json).toContain('Finalizes in 1 trading day');
    expect(json).not.toContain('Finalizes in 1 trading days');
  });

  it('names the same countdown on the "Vetoed by risk" still-marking caption', () => {
    const tree = renderScreen();
    openGhostTab(tree);

    const json = JSON.stringify(tree.toJSON());
    // vetoed.count=10 is the bucket TOTAL, pendingCount=2,
    // oldestPendingRemainingTradingDays=3 — so 8 have finalised, not 10.
    // Reading `count` as the finalised subset is what rendered "6
    // finalised · 6 still marking" live for a bucket where nothing had.
    expect(json).toContain('8 finalised · 2 still marking — finalizes in 3 trading days');
  });

  it('renders the scan funnel and contract funnel cards with distinct titles', () => {
    const tree = renderScreen();
    const json = JSON.stringify(tree.toJSON());

    // The scan funnel answers "how many symbols reach the LLM at all";
    // the contract funnel (aggregate mode, since this screen passes
    // windowDays) answers "which contract survived for one that did" —
    // distinct strings so a reader never mistakes one for the other.
    expect(json).toContain('Symbol scan funnel');
    expect(json).toContain('Contracts considered');
  });
});
