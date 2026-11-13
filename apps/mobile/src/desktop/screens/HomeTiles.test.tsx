import { act, create } from 'react-test-renderer';
import type { VetoRuleDto } from '@app/shared-types';

import { useAutonomy } from '@/hooks/useAutonomy';
import type { AutonomyStatus } from '@/hooks/useAutonomy';

import { AutonomyTileView, HomeTiles, LedgerDotGrid, countdown, ledgerDots } from './HomeTiles';

jest.mock('@/hooks/useAutonomy');
jest.mock('@/hooks/useAccount', () => ({ useAccount: () => ({ data: undefined, isLoading: true }) }));
jest.mock('@/hooks/useApprovals', () => ({ usePendingApprovals: () => ({ data: [], isLoading: false }) }));
jest.mock('@/hooks/usePositions', () => ({ useOpenPositions: () => ({ data: [], isLoading: false }) }));
jest.mock('@/hooks/useTradingMode', () => ({ useTradingMode: () => ({ isError: true }) }));
jest.mock('@/hooks/useInsights', () => ({
  useVetoLedger: () => ({ isLoading: true }),
  useGhostSummary: () => ({ isLoading: true }),
}));
jest.mock('../nav', () => ({ useNav: () => ({ go: () => {} }) }));

const BLOCKED: AutonomyStatus = {
  state: 'blocked',
  headline: 'The desk cannot trade on its own',
  checks: [
    { id: 'llm', label: 'LLM usable', ok: true, severity: 'blocker', detail: 'glm', fix: null },
    {
      id: 'breaker',
      label: 'Breaker clear',
      ok: false,
      severity: 'blocker',
      detail: 'latched',
      fix: 'acknowledge the breaker in Settings',
    },
  ],
  lastSweepAt: null,
  nextSweepAt: null,
  asOf: '2026-11-13T00:00:00Z',
};

function text(r: ReturnType<typeof create>): string {
  return JSON.stringify(r.toJSON());
}

describe('Autonomy tile', () => {
  it('shows BLOCKED and, once expanded, the fix for the failing check', () => {
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(<AutonomyTileView status={BLOCKED} loading={false} failed={false} />);
    });
    expect(text(r)).toContain('BLOCKED');
    expect(text(r)).not.toContain('acknowledge the breaker');
    const tile = r.root.findAll((n) => n.props.role === 'button' && typeof n.props.onClick === 'function')[0];
    act(() => {
      tile.props.onClick();
    });
    expect(text(r)).toContain('acknowledge the breaker in Settings');
  });

  it('survives an errored query with "status unavailable"', () => {
    (useAutonomy as jest.Mock).mockReturnValue({ isError: true, isLoading: false, data: undefined });
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(<HomeTiles />);
    });
    expect(text(r)).toContain('status unavailable');
  });

  it('hides the sweep tile when nextSweepAt is null and shows it otherwise', () => {
    (useAutonomy as jest.Mock).mockReturnValue({ isLoading: false, data: BLOCKED });
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(<HomeTiles />);
    });
    expect(text(r)).not.toContain('Next sweep');
    (useAutonomy as jest.Mock).mockReturnValue({
      isLoading: false,
      data: { ...BLOCKED, nextSweepAt: new Date(Date.now() + 90_000).toISOString() },
    });
    act(() => {
      r.update(<HomeTiles />);
    });
    expect(text(r)).toContain('Next sweep');
    act(() => r.unmount());
  });

  it('formats the countdown', () => {
    const now = Date.parse('2026-11-13T10:00:00Z');
    expect(countdown('2026-11-13T10:04:12Z', now)).toBe('4m 12s');
    expect(countdown('2026-11-13T09:00:00Z', now)).toBe('due now');
  });
});

describe('Refusal Ledger dot grid', () => {
  const rules: Pick<VetoRuleDto, 'count' | 'markedCount' | 'lossAvoidedUsd' | 'upsideBlockedUsd' | 'markedPnl'>[] = [
    { count: 5, markedCount: 4, lossAvoidedUsd: 300, upsideBlockedUsd: 100, markedPnl: -200 },
    { count: 3, markedCount: 0, lossAvoidedUsd: null, upsideBlockedUsd: null, markedPnl: null },
  ];

  it('derives exactly one dot per refusal, grey while pending', () => {
    const dots = ledgerDots(rules);
    expect(dots).toHaveLength(8);
    expect(dots.filter((d) => d === 'avoided')).toHaveLength(3);
    expect(dots.filter((d) => d === 'blocked')).toHaveLength(1);
    expect(dots.filter((d) => d === 'pending')).toHaveLength(4);
  });

  it('renders one <i> per refusal', () => {
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(<LedgerDotGrid dots={ledgerDots(rules)} />);
    });
    expect(r.root.findAll((n) => n.type === 'i' && n.props['data-kind'] != null)).toHaveLength(8);
  });
});
