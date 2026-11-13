/**
 * Phone Settings: broker connect/verify flows and the trading-mode card.
 * Hooks are mocked; the screen's own state logic is what is under test.
 * (Lives outside app/ so expo-router does not treat it as a route.)
 */
import { AppState, Linking } from 'react-native';
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';

import { BrokersCard, TradingModeCard } from '../../app/(tabs)/settings';

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
jest.mock('expo-router', () => ({ useRouter: () => ({ push: jest.fn(), back: jest.fn() }) }));
jest.mock('react-native-safe-area-context', () => ({ SafeAreaView: 'SafeAreaView' }));
jest.mock('@/hooks/usePushRegistration', () => ({ revokeRegisteredDevice: jest.fn() }));
jest.mock('@/hooks/useBrokerConnections');
jest.mock('@/hooks/useTradingMode', () => ({
  LIVE_CONFIRM_PHRASE: 'ENABLE LIVE TRADING',
  useTradingMode: jest.fn(),
  useSetTradingMode: jest.fn(),
}));
jest.mock('@/lib/demoSession', () => ({
  DEMO_DISABLED_REASON: 'Disabled in read-only demo mode',
  useIsDemoSession: jest.fn(() => false),
}));

/* eslint-disable @typescript-eslint/no-var-requires */
const bc = require('@/hooks/useBrokerConnections') as Record<string, jest.Mock>;
const tmHooks = require('@/hooks/useTradingMode') as Record<string, jest.Mock>;
/* eslint-enable @typescript-eslint/no-var-requires */

const baseConn = {
  id: 'c1',
  broker: 'alpaca',
  isPaper: true,
  accountNumber: 'PA123',
  status: 'active',
  connectionSource: 'oauth',
  autoApproveConsent: false,
  createdAt: '2026-09-01T00:00:00Z',
  lastUsedAt: null,
  needsReconnect: false,
  tokenExpiresAt: null,
  market: 'US',
  currency: 'USD',
  liveTradingConsent: false,
};

const refetch = jest.fn();
const verifyAsync = jest.fn();

function setConnections(list: unknown[]) {
  bc.useBrokerConnections.mockReturnValue({
    data: list,
    isLoading: false,
    isError: false,
    refetch,
  });
}

const mounted: ReactTestRenderer[] = [];
function render(el: React.ReactElement): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(el);
  });
  mounted.push(tree);
  return tree;
}

afterEach(() => {
  act(() => {
    mounted.splice(0).forEach((t) => t.unmount());
  });
});

const text = (tree: ReactTestRenderer) => JSON.stringify(tree.toJSON());
const byId = (tree: ReactTestRenderer, id: string) => tree.root.findByProps({ testID: id });
const has = (tree: ReactTestRenderer, id: string) => tree.root.findAllByProps({ testID: id }).length > 0;

beforeEach(() => {
  jest.clearAllMocks();
  bc.useBrokerCatalog.mockReturnValue({ data: undefined, isError: true });
  bc.useRevokeBrokerConnection.mockReturnValue({ mutate: jest.fn(), isPending: false });
  bc.useVerifyBrokerConnection.mockReturnValue({ mutateAsync: verifyAsync, isPending: false });
  bc.useSetLiveTradingConsent.mockReturnValue({ mutate: jest.fn(), isPending: false });
  bc.startAlpacaOAuth.mockResolvedValue({
    authorizeUrl: 'https://alpaca.test/auth',
    state: 's1',
    expiresAt: '',
    devWarning: null,
    oauthNotConfigured: false,
  });
  bc.startZerodhaConnect.mockResolvedValue({ loginUrl: 'https://kite.test', state: 'z', expiresAt: '', devWarning: null });
  jest.spyOn(Linking, 'canOpenURL').mockResolvedValue(true);
  jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined);
});

describe('BrokersCard', () => {
  it('clears "Waiting for browser" and refetches when the app returns to the foreground', async () => {
    setConnections([]);
    let handler: ((s: string) => void) | undefined;
    jest.spyOn(AppState, 'addEventListener').mockImplementation(((_: string, h: (s: string) => void) => {
      handler = h;
      return { remove: jest.fn() };
    }) as never);

    const tree = render(<BrokersCard />);
    await act(async () => {
      byId(tree, 'connect-alpaca-paper').props.onPress();
    });
    expect(text(tree)).toContain('Waiting for browser');
    expect(byId(tree, 'connect-alpaca-paper').props.disabled).toBe(true);
    expect(has(tree, 'cancel-alpaca-pending')).toBe(true);

    await act(async () => {
      handler?.('active');
    });
    expect(text(tree)).not.toContain('Waiting for browser');
    expect(byId(tree, 'connect-alpaca-paper').props.disabled).toBe(false);
    expect(refetch).toHaveBeenCalled();
  });

  it('Cancel stops waiting', async () => {
    setConnections([]);
    const tree = render(<BrokersCard />);
    await act(async () => {
      byId(tree, 'connect-alpaca-paper').props.onPress();
    });
    act(() => {
      byId(tree, 'cancel-alpaca-pending').props.onPress();
    });
    expect(text(tree)).not.toContain('Waiting for browser');
  });

  it('a needsReconnect connection shows "Session expired" and Reconnect, not the green dot', () => {
    setConnections([{ ...baseConn, broker: 'zerodha', isPaper: false, market: 'IN', currency: 'INR', needsReconnect: true }]);
    const tree = render(<BrokersCard />);
    expect(text(tree)).toContain('Session expired');
    expect(has(tree, 'reconnect-zerodha')).toBe(true);
    expect(has(tree, 'broker-status-ok-zerodha')).toBe(false);
  });

  it('a healthy connection shows the green dot and no Reconnect', () => {
    setConnections([baseConn]);
    const tree = render(<BrokersCard />);
    expect(has(tree, 'broker-status-ok-alpaca')).toBe(true);
    expect(has(tree, 'reconnect-alpaca')).toBe(false);
  });

  it('verify failure shows the server error text', async () => {
    setConnections([baseConn]);
    verifyAsync.mockResolvedValue({ ok: false, error: 'Invalid API key', currency: 'USD' });
    const tree = render(<BrokersCard />);
    await act(async () => {
      await byId(tree, 'verify-alpaca').props.onPress();
    });
    expect(text(tree)).toContain('Invalid API key');
  });

  it('verify success shows equity and buying power in the connection currency', async () => {
    setConnections([{ ...baseConn, broker: 'zerodha', isPaper: false, market: 'IN', currency: 'INR' }]);
    verifyAsync.mockResolvedValue({ ok: true, equity: 123456.5, buyingPower: 5000, currency: 'INR', error: null });
    const tree = render(<BrokersCard />);
    await act(async () => {
      await byId(tree, 'verify-zerodha').props.onPress();
    });
    const out = text(tree);
    expect(out).toContain('₹1,23,456.50');
    expect(out).not.toContain('$');
  });

  it('renders needs_server_config and planned catalog entries disabled', () => {
    setConnections([]);
    bc.useBrokerCatalog.mockReturnValue({
      data: [
        { id: 'alpaca', name: 'Alpaca', market: 'US', currency: 'USD', auth: 'oauth', status: 'available', supportsPaper: true, supportsOptions: true, note: null, missingConfig: [] },
        { id: 'upstox', name: 'Upstox', market: 'IN', currency: 'INR', auth: 'oauth', status: 'needs_server_config', supportsPaper: false, supportsOptions: false, note: null, missingConfig: ['UPSTOX_API_KEY'] },
        { id: 'dhan', name: 'Dhan', market: 'IN', currency: 'INR', auth: 'token', status: 'planned', supportsPaper: false, supportsOptions: false, note: 'Coming later', missingConfig: [] },
      ],
      isError: false,
    });
    const tree = render(<BrokersCard />);
    expect(text(tree)).toContain('UPSTOX_API_KEY');
    expect(byId(tree, 'connect-upstox-disabled').props.disabled).toBe(true);
    expect(text(tree)).toContain('Coming later');
    expect(has(tree, 'connect-zerodha')).toBe(false);
  });

  it('real-money consent toggle appears only for non-paper connections', () => {
    setConnections([baseConn]);
    expect(has(render(<BrokersCard />), 'live-consent-alpaca')).toBe(false);
    setConnections([{ ...baseConn, isPaper: false }]);
    expect(has(render(<BrokersCard />), 'live-consent-alpaca')).toBe(true);
  });
});

describe('TradingModeCard', () => {
  const mutate = jest.fn();
  const mode = (over: Record<string, unknown> = {}) =>
    tmHooks.useTradingMode.mockReturnValue({
      data: { mode: 'paper', source: 'admin', locked: false, isAdmin: true, updatedBy: null, updatedAt: null, ...over },
    });

  beforeEach(() => {
    tmHooks.useSetTradingMode.mockReturnValue({ mutate, isPending: false });
  });

  it('renders nothing when the endpoint errors', () => {
    tmHooks.useTradingMode.mockReturnValue({ data: undefined, isError: true });
    expect(render(<TradingModeCard />).toJSON()).toBeNull();
  });

  it('is read-only for non-admins', () => {
    mode({ isAdmin: false });
    const tree = render(<TradingModeCard />);
    expect(text(tree)).toContain('PAPER');
    expect(has(tree, 'trading-mode-readonly')).toBe(true);
    expect(has(tree, 'trading-mode-to-live')).toBe(false);
  });

  it('live switch stays disabled until the phrase matches exactly', () => {
    mode();
    const tree = render(<TradingModeCard />);
    act(() => {
      byId(tree, 'trading-mode-to-live').props.onPress();
    });
    expect(byId(tree, 'trading-mode-confirm-live').props.disabled).toBe(true);
    act(() => {
      byId(tree, 'trading-mode-phrase').props.onChangeText('enable live trading');
    });
    expect(byId(tree, 'trading-mode-confirm-live').props.disabled).toBe(true);
    act(() => {
      byId(tree, 'trading-mode-phrase').props.onChangeText('ENABLE LIVE TRADING');
    });
    expect(byId(tree, 'trading-mode-confirm-live').props.disabled).toBe(false);
    act(() => {
      byId(tree, 'trading-mode-confirm-live').props.onPress();
    });
    expect(mutate).toHaveBeenCalledWith({ mode: 'live', confirm: 'ENABLE LIVE TRADING' }, expect.anything());
  });

  it('is disabled when locked', () => {
    mode({ locked: true });
    const tree = render(<TradingModeCard />);
    expect(byId(tree, 'trading-mode-to-live').props.disabled).toBe(true);
  });
});
