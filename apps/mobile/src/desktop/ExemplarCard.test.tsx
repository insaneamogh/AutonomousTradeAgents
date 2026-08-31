/**
 * ExemplarCard — the story-trade view (docs/IMPL_REFUSAL_LEDGER.md §2.2).
 * Same style as Insights.test.tsx: react-test-renderer + act(), with
 * useVetoExemplar mocked wholesale.
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';
import type { VetoExemplarResponse } from '@app/shared-types';

import { useVetoExemplar } from '@/hooks/useInsights';

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

function mockExemplar(data: VetoExemplarResponse, overrides: Partial<{ isLoading: boolean; isError: boolean }> = {}) {
  mockUseVetoExemplar.mockReturnValue({
    data,
    isLoading: false,
    isError: false,
    ...overrides,
  });
}

describe('ExemplarCard', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('reports the endpoint as unavailable rather than crashing when the request errors', () => {
    mockUseVetoExemplar.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    const tree = renderCard('max_premium_pct');
    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('Exemplar not available');
    expect(json).toContain('/api/v1/risk/vetoes/max_premium_pct/exemplar');
  });

  it('says so plainly when no ghost under this rule has finalized yet', () => {
    mockExemplar({ rule: 'illiquid_contract', found: false });
    const tree = renderCard('illiquid_contract');
    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('No finalized refusal yet');
  });

  it('renders a loss-preventing refusal as a green SAVED verdict', () => {
    mockExemplar({
      rule: 'max_premium_pct',
      found: true,
      occSymbol: 'NVDA260918C00225000',
      qty: 12,
      price: 2.17,
      estimatedNotional: 2604,
      notionalPctOfEquity: 2.6,
      capPct: 2.5,
      bullCase: 'Breakout continuation.',
      bearCase: 'Overbought on the daily.',
      markPrice: 0.94,
      tradingDaysElapsed: 5,
      ghostPnl: -1476,
      preventedLossUsd: 1476,
      riskProfile: 'aggressive_paper',
    });
    const tree = renderCard('max_premium_pct');
    const json = JSON.stringify(tree.toJSON());

    expect(json).toContain('NVDA260918C00225000');
    expect(json).toContain('That refusal saved $1,476');
    const savedPills = tree.root.findAll(
      (node) => node.type === Pill && node.props.tone === 'bull' && node.props.children === 'SAVED',
    );
    expect(savedPills.length).toBe(1);
  });

  it('renders a refusal that would have made money as an amber MISSED verdict, not hidden', () => {
    mockExemplar({
      rule: 'min_council_confidence',
      found: true,
      symbol: 'TSLA',
      ghostPnl: 120,
      preventedLossUsd: 0,
      riskProfile: 'aggressive_paper',
    });
    const tree = renderCard('min_council_confidence');
    const json = JSON.stringify(tree.toJSON());

    expect(json).toContain('That refusal would have made $120');
    const missedPills = tree.root.findAll(
      (node) => node.type === Pill && node.props.tone === 'warn' && node.props.children === 'MISSED',
    );
    expect(missedPills.length).toBe(1);
  });

  it('renders a still-marking ghost as pending, never a fabricated verdict', () => {
    mockExemplar({ rule: 'pdt_block', found: true, symbol: 'AAPL', ghostPnl: null, riskProfile: null });
    const tree = renderCard('pdt_block');
    const json = JSON.stringify(tree.toJSON());

    expect(json).toContain('pending — still marking, no verdict yet');
    expect(json).toContain('risk profile disclosure pending');
  });

  it('shows the AUTO pill only when the exemplar decision was auto-approved', () => {
    mockExemplar({
      rule: 'max_premium_pct',
      found: true,
      symbol: 'AAPL',
      ghostPnl: -50,
      preventedLossUsd: 50,
      approvalMode: 'auto',
    });
    const tree = renderCard('max_premium_pct');

    const autoPills = tree.root.findAll(
      (node) => node.type === Pill && node.props.children === 'AUTO',
    );
    expect(autoPills.length).toBe(1);
  });
});
