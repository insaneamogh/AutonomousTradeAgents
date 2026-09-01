/**
 * ExemplarCard — the story-trade view (docs/IMPL_REFUSAL_LEDGER.md §2.2).
 * Same style as Insights.test.tsx: react-test-renderer + act(), with
 * useVetoExemplar mocked wholesale.
 *
 * Mocks the REAL response shape (`apps/api/app/routers/insights.py`'s
 * `VetoExemplarResponse`) and the REAL "nothing finalized yet" signal (a
 * 404 `ApiError`, not a `found: false` field an earlier version of this
 * file guessed at before the endpoint existed — see ExemplarCard.tsx's
 * module docstring).
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';
import type { VetoExemplarResponse } from '@app/shared-types';

import { useVetoExemplar } from '@/hooks/useInsights';
import { ApiError } from '@/lib/api';

import { ExemplarCard } from './ExemplarCard';
import { Pill } from './primitives';

jest.mock('@/hooks/useInsights');

const mockUseVetoExemplar = useVetoExemplar as jest.Mock;

function renderCard(rule = 'max_premium_pct'): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(<ExemplarCard rule={rule} onClose={jest.fn()} />);
  });
  return tree;
}

function mockExemplar(data: VetoExemplarResponse) {
  mockUseVetoExemplar.mockReturnValue({ data, isLoading: false, isError: false, error: null });
}

describe('ExemplarCard', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('reports the endpoint as unavailable, not "not finalized yet", on a non-404 error', () => {
    mockUseVetoExemplar.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(500, null),
    });
    const tree = renderCard('max_premium_pct');
    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('Exemplar not available');
    expect(json).not.toContain('No finalized refusal yet');
  });

  it('says so plainly on the real "nothing finalized yet" signal — a 404, not a found:false field', () => {
    mockUseVetoExemplar.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(404, { detail: "no finalized ghost outcome yet for rule 'illiquid_contract'" }),
    });
    const tree = renderCard('illiquid_contract');
    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('No finalized refusal yet');
  });

  it('renders a loss-preventing refusal as a green SAVED verdict, from the real field names', () => {
    mockExemplar({
      decisionId: 'd1',
      rule: 'max_premium_pct',
      symbol: 'NVDA',
      side: 'BUY',
      occSymbol: 'NVDA260918C00225000',
      qty: 12,
      entryPrice: 2.17,
      estimatedNotional: 2604,
      isOption: true,
      bullCase: 'Breakout continuation.',
      bearCase: 'Overbought on the daily.',
      rationale: 'Momentum thesis.',
      lastPrice: 0.94,
      horizonDays: 5,
      ghostPnl: -1476,
      preventedLossUsd: 1476,
      triggeredAt: '2026-08-20T14:00:00Z',
    });
    const tree = renderCard('max_premium_pct');
    const json = JSON.stringify(tree.toJSON());

    expect(json).toContain('NVDA260918C00225000');
    expect(json).toContain('$2.17');
    expect(json).toContain('$0.94');
    expect(json).toContain('That refusal saved $1,476');
    const savedPills = tree.root.findAll(
      (node) => node.type === Pill && node.props.tone === 'bull' && node.props.children === 'SAVED',
    );
    expect(savedPills.length).toBe(1);
  });

  it('renders a refusal that would have made money as an amber MISSED verdict, not hidden', () => {
    mockExemplar({
      decisionId: 'd2',
      rule: 'min_council_confidence',
      symbol: 'TSLA',
      side: 'BUY',
      qty: 10,
      entryPrice: 250,
      isOption: false,
      bullCase: '',
      bearCase: '',
      rationale: '',
      horizonDays: 5,
      ghostPnl: 120,
      preventedLossUsd: 0,
      triggeredAt: '2026-08-20T14:00:00Z',
    });
    const tree = renderCard('min_council_confidence');
    const json = JSON.stringify(tree.toJSON());

    expect(json).toContain('That refusal would have made $120');
    const missedPills = tree.root.findAll(
      (node) => node.type === Pill && node.props.tone === 'warn' && node.props.children === 'MISSED',
    );
    expect(missedPills.length).toBe(1);
  });
});
