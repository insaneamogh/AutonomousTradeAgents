import { act, create } from 'react-test-renderer';

import VetoLedgerScreen from '../../app/vetoes';

// The @app/ui barrel pulls in reanimated + gesture-handler (native worklets),
// which this environment can't initialise. Load only the primitives used.
jest.mock('@app/ui', () => {
  const dir = '../../../../packages/ui/src/';
  return {
    ...require(dir + 'Button'),
    ...require(dir + 'Card'),
    ...require(dir + 'Toggle'),
    ...require(dir + 'HapticPressable'),
    ...require(dir + 'EmptyState'),
    ...require(dir + 'ErrorState'),
    ...require(dir + 'utils'),
    Skeleton: () => null,
  };
});
jest.mock('expo-router', () => ({ useRouter: () => ({ back: jest.fn() }) }));
jest.mock('react-native-safe-area-context', () => ({ SafeAreaView: 'SafeAreaView' }));
jest.mock('@/hooks/useInsights');

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { useVetoLedger } = require('@/hooks/useInsights') as { useVetoLedger: jest.Mock };

const ledger = (currency: string) => ({
  windowDays: 30,
  totalVetoes: 2,
  totalBlockedNotional: 120000,
  currency,
  rules: [{ rule: 'max_premium_pct', count: 2, blockedNotional: 120000, preventedLossUsd: 5000 }],
});

describe('VetoLedgerScreen', () => {
  it('switches market, passes it to the hook, and formats with the returned currency', () => {
    useVetoLedger.mockImplementation((_d: number, market: string) => ({
      data: market === 'IN' ? ledger('INR') : ledger('USD'),
      isLoading: false,
    }));
    let tree!: ReturnType<typeof create>;
    act(() => {
      tree = create(<VetoLedgerScreen />);
    });
    expect(useVetoLedger).toHaveBeenLastCalledWith(30, 'US');
    expect(JSON.stringify(tree.toJSON())).toContain('$120,000');

    act(() => {
      tree.root.findByProps({ testID: 'veto-market-IN' }).props.onPress();
    });
    expect(useVetoLedger).toHaveBeenLastCalledWith(30, 'IN');
    const out = JSON.stringify(tree.toJSON());
    expect(out).toContain('₹1,20,000');
    expect(out).not.toContain('$120,000');
    act(() => tree.unmount());
  });
});
