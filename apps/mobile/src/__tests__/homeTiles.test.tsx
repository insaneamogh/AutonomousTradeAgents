/** Home tiles + decision screen states. Hooks mocked; component logic under test. */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';

import DecisionBiographyScreen from '../../app/decision/[id]';
import { AutonomyTile } from '@/components/home/AutonomyTile';
import { EquityTile } from '@/components/home/EquityTile';

jest.mock('@app/ui', () => {
  const dir = '../../../../packages/ui/src/';
  return {
    ...require(dir + 'Button'),
    ...require(dir + 'Card'),
    ...require(dir + 'HapticPressable'),
    ...require(dir + 'EmptyState'),
    ...require(dir + 'ErrorState'),
    ...require(dir + 'utils'),
    Skeleton: () => null,
  };
});
jest.mock('expo-router', () => ({
  useRouter: () => ({ push: jest.fn(), back: jest.fn() }),
  useLocalSearchParams: () => ({ id: 'abc' }),
}));
jest.mock('react-native-safe-area-context', () => ({ SafeAreaView: 'SafeAreaView' }));
jest.mock('@/hooks/useAutonomy');
jest.mock('@/hooks/useAccount');
jest.mock('@/hooks/useBrokerConnections');
jest.mock('@/hooks/useDecisionTimeline');

/* eslint-disable @typescript-eslint/no-var-requires */
const { useAutonomy } = require('@/hooks/useAutonomy') as Record<string, jest.Mock>;
const { useAccount } = require('@/hooks/useAccount') as Record<string, jest.Mock>;
const { useBrokerConnections } = require('@/hooks/useBrokerConnections') as Record<string, jest.Mock>;
const { useDecisionTimeline } = require('@/hooks/useDecisionTimeline') as Record<string, jest.Mock>;
/* eslint-enable @typescript-eslint/no-var-requires */

const mounted: ReactTestRenderer[] = [];
function render(el: React.ReactElement) {
  let t!: ReactTestRenderer;
  act(() => {
    t = create(el);
  });
  mounted.push(t);
  return t;
}
afterEach(() => act(() => mounted.splice(0).forEach((t) => t.unmount())));
const text = (t: ReactTestRenderer) => JSON.stringify(t.toJSON());

const blocked = {
  state: 'blocked',
  headline: 'Nothing can trade',
  checks: [
    { id: 'llm', label: 'LLM usable', ok: false, severity: 'blocker', detail: 'MOCK', fix: 'set GLM_API_KEY on Railway' },
    { id: 'mkt', label: 'Market open', ok: true, severity: 'info', detail: 'open', fix: null },
  ],
  lastSweepAt: null,
  nextSweepAt: null,
  asOf: '',
};

describe('AutonomyTile', () => {
  it('shows BLOCKED and, once expanded, the failing check fix', () => {
    useAutonomy.mockReturnValue({ data: blocked, isLoading: false, isError: false });
    const t = render(<AutonomyTile />);
    expect(text(t)).toContain('BLOCKED');
    expect(text(t)).not.toContain('GLM_API_KEY');
    act(() => t.root.findByProps({ testID: 'autonomy-toggle' }).props.onPress());
    expect(text(t)).toContain('set GLM_API_KEY on Railway');
  });

  it('survives an errored query', () => {
    useAutonomy.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    const t = render(<AutonomyTile />);
    expect(text(t)).toContain('Status unavailable');
  });
});

describe('EquityTile', () => {
  const account = { equity: 102847, todayPnl: 10, todayPnlPct: 0.1, brokerName: 'Alpaca', isPaper: true, openPositions: 1, currency: 'USD' };

  it('with no active broker shows the connect prompt and no dollar figure', () => {
    useBrokerConnections.mockReturnValue({ data: [], isLoading: false, isError: false });
    useAccount.mockReturnValue({ data: account, isLoading: false, isError: false, refetch: jest.fn() });
    const t = render(<EquityTile />);
    expect(text(t)).toContain('No broker connected');
    expect(text(t)).not.toContain('$');
    expect(text(t)).not.toContain('102');
  });

  it('with an active broker shows equity in the account currency', () => {
    useBrokerConnections.mockReturnValue({ data: [{ status: 'active' }], isLoading: false, isError: false });
    useAccount.mockReturnValue({ data: { ...account, currency: 'INR' }, isLoading: false, isError: false, refetch: jest.fn() });
    const t = render(<EquityTile />);
    expect(text(t)).toContain('₹1,02,847');
    expect(text(t)).not.toContain('$');
  });
});

describe('DecisionBiographyScreen', () => {
  it('shows not-found on a 404 instead of a blank page', () => {
    useDecisionTimeline.mockReturnValue({ data: undefined, isLoading: false, isError: true, error: { status: 404 }, refetch: jest.fn() });
    const t = render(<DecisionBiographyScreen />);
    expect(text(t)).toContain('Decision not found');
  });

  it('shows an error with retry on a server failure', () => {
    useDecisionTimeline.mockReturnValue({ data: undefined, isLoading: false, isError: true, error: { status: 500 }, refetch: jest.fn() });
    const t = render(<DecisionBiographyScreen />);
    expect(text(t)).toContain("Couldn't load this trade");
  });
});
