/**
 * SettingsScreen broker + trading-mode cards. Hooks are mocked wholesale
 * (same style as Insights.test.tsx): react-test-renderer + act().
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';

import {
  startAlpacaOAuth,
  startZerodhaConnect,
  useBrokerCatalog,
  useBrokerConnections,
  useRevokeBrokerConnection,
  useSetLiveTradingConsent,
  useVerifyBrokerConnection,
} from '@/hooks/useBrokerConnections';
import type { BrokerConnection } from '@/hooks/useBrokerConnections';
import { useSetTradingMode, useTradingMode } from '@/hooks/useTradingMode';
import { useHealthFull } from '@/hooks/useHealthFull';
import { useTickerCombobox } from '@/hooks/useTickerCombobox';
import { useAddWatchlistSymbol, useRemoveWatchlistSymbol, useWatchlist } from '@/hooks/useWatchlist';

import { SettingsScreen } from './Settings';

jest.mock('@/hooks/useBrokerConnections');
jest.mock('@/hooks/useTradingMode', () => ({
  LIVE_CONFIRM_PHRASE: 'ENABLE LIVE TRADING',
  useTradingMode: jest.fn(),
  useSetTradingMode: jest.fn(),
}));
jest.mock('@/hooks/useWatchlist');
jest.mock('@/hooks/useHealthFull');
jest.mock('@/hooks/useTickerCombobox');
jest.mock('nativewind', () => ({ useColorScheme: () => ({ colorScheme: 'dark' }) }));

const CONN: BrokerConnection = {
  id: 'c1',
  broker: 'zerodha',
  isPaper: false,
  accountNumber: 'AB1234',
  status: 'active',
  connectionSource: 'oauth',
  autoApproveConsent: false,
  createdAt: '2026-11-01T00:00:00Z',
  lastUsedAt: null,
  needsReconnect: false,
  tokenExpiresAt: null,
  market: 'IN',
  currency: 'INR',
  liveTradingConsent: false,
};

const CATALOG = [
  { id: 'alpaca', name: 'Alpaca', market: 'US', currency: 'USD', auth: 'oauth', status: 'available', supportsPaper: true, supportsOptions: true, note: null, missingConfig: [] },
  { id: 'zerodha', name: 'Zerodha', market: 'IN', currency: 'INR', auth: 'kite', status: 'available', supportsPaper: false, supportsOptions: false, note: null, missingConfig: [] },
];

const refetch = jest.fn();
const verifyAsync = jest.fn();
const openMock = jest.fn();
const assignMock = jest.fn();
const setModeMutate = jest.fn();

function setConnections(data: BrokerConnection[]) {
  (useBrokerConnections as jest.Mock).mockReturnValue({ data, isLoading: false, refetch });
}

function setTradingMode(data: unknown) {
  (useTradingMode as jest.Mock).mockReturnValue({ data, isError: !data });
}

function render(): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(<SettingsScreen />);
  });
  return tree;
}

const byLabel = (tree: ReactTestRenderer, label: string) =>
  tree.root.findAll((n) => n.props?.ariaLabel === label || n.props?.['aria-label'] === label);

async function press(tree: ReactTestRenderer, label: string) {
  const btn = tree.root.findAll(
    (n) => n.type === 'button' && (n.props['aria-label'] === label),
  )[0];
  await act(async () => {
    btn.props.onClick();
  });
}

const textOf = (tree: ReactTestRenderer) => JSON.stringify(tree.toJSON());

beforeEach(() => {
  jest.clearAllMocks();
  const g = globalThis as unknown as Record<string, unknown>;
  g.window = Object.assign((g.window as object) ?? g, {
    open: openMock,
    location: { assign: assignMock },
    confirm: jest.fn(() => true),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
  });
  g.document = { visibilityState: 'visible', addEventListener: jest.fn(), removeEventListener: jest.fn() };

  (useBrokerCatalog as jest.Mock).mockReturnValue({ data: CATALOG });
  (useRevokeBrokerConnection as jest.Mock).mockReturnValue({ mutate: jest.fn(), isPending: false });
  (useVerifyBrokerConnection as jest.Mock).mockReturnValue({ mutateAsync: verifyAsync });
  (useSetLiveTradingConsent as jest.Mock).mockReturnValue({ mutate: jest.fn(), isPending: false });
  (useSetTradingMode as jest.Mock).mockReturnValue({ mutate: setModeMutate, isPending: false });
  (useHealthFull as jest.Mock).mockReturnValue({ data: undefined, isLoading: true });
  (useWatchlist as jest.Mock).mockReturnValue({ data: [], isLoading: false });
  (useAddWatchlistSymbol as jest.Mock).mockReturnValue({ mutate: jest.fn(), isPending: false, isError: false });
  (useRemoveWatchlistSymbol as jest.Mock).mockReturnValue({ mutate: jest.fn() });
  (useTickerCombobox as jest.Mock).mockReturnValue({
    query: '', setQuery: jest.fn(), open: false, close: jest.fn(), setOpen: jest.fn(), activeIndex: 0,
    moveActive: jest.fn(), hits: [], selectIndex: jest.fn(), selectActive: jest.fn(), reset: jest.fn(),
  });
  setConnections([]);
  setTradingMode(undefined);
});

describe('SettingsScreen — broker connections', () => {
  it('Zerodha connect opens a new tab and never navigates this one away', async () => {
    (startZerodhaConnect as jest.Mock).mockResolvedValue({ loginUrl: 'https://kite.zerodha.com/connect/login?x=1' });
    const tree = render();
    await press(tree, 'Connect Zerodha');

    expect(openMock).toHaveBeenCalledWith('https://kite.zerodha.com/connect/login?x=1', '_blank', 'noopener');
    expect(assignMock).not.toHaveBeenCalled();
    expect(textOf(tree)).toContain('Finish in the other tab');
  });

  it('Alpaca connect also opens a new tab', async () => {
    (startAlpacaOAuth as jest.Mock).mockResolvedValue({ authorizeUrl: 'https://app.alpaca.markets/oauth', oauthNotConfigured: false, devWarning: null });
    const tree = render();
    await press(tree, 'Connect Alpaca paper account');
    expect(openMock).toHaveBeenCalledWith('https://app.alpaca.markets/oauth', '_blank', 'noopener');
    expect(assignMock).not.toHaveBeenCalled();
  });

  it('shows the server detail when the connect call fails', async () => {
    (startZerodhaConnect as jest.Mock).mockRejectedValue({ status: 503, body: { detail: 'KITE_API_KEY is not set' } });
    const tree = render();
    await press(tree, 'Connect Zerodha');
    expect(textOf(tree)).toContain('KITE_API_KEY is not set');
    expect(openMock).not.toHaveBeenCalled();
  });

  it('needsReconnect shows SESSION EXPIRED, a Reconnect button, and no ACTIVE pill', () => {
    setConnections([{ ...CONN, needsReconnect: true }]);
    const tree = render();
    const json = textOf(tree);
    expect(json).toContain('SESSION EXPIRED');
    expect(json).not.toContain('ACTIVE');
    expect(byLabel(tree, 'Reconnect zerodha').length).toBeGreaterThan(0);
  });

  it('verify failure shows the error text inline', async () => {
    setConnections([CONN]);
    verifyAsync.mockResolvedValue({ ok: false, broker: 'zerodha', isPaper: false, equity: null, buyingPower: null, currency: 'INR', checkedAt: 'x', error: 'Token expired at Kite' });
    const tree = render();
    await press(tree, 'Verify the zerodha connection');
    expect(textOf(tree)).toContain('Token expired at Kite');
  });

  it('verify success formats equity in the connection currency', async () => {
    setConnections([CONN]);
    verifyAsync.mockResolvedValue({ ok: true, broker: 'zerodha', isPaper: false, equity: 150000, buyingPower: 40000, currency: 'INR', checkedAt: 'x', error: null });
    const tree = render();
    await press(tree, 'Verify the zerodha connection');
    expect(textOf(tree)).toContain('₹1,50,000.00');
  });

  it('disables a needs_server_config broker and names the missing config', () => {
    (useBrokerCatalog as jest.Mock).mockReturnValue({
      data: [{ ...CATALOG[1], status: 'needs_server_config', missingConfig: ['KITE_API_KEY', 'KITE_API_SECRET'] }],
    });
    const tree = render();
    expect(textOf(tree)).toContain('Server is missing: KITE_API_KEY, KITE_API_SECRET');
    const btn = tree.root.findAll((n) => n.type === 'button' && n.props['aria-label']?.startsWith('Connect Zerodha'))[0];
    expect(btn.props.disabled).toBe(true);
  });

  it('hides revoked rows behind a toggle', async () => {
    setConnections([CONN, { ...CONN, id: 'c2', broker: 'alpaca', status: 'revoked' }]);
    const tree = render();
    expect(textOf(tree)).toContain('Show revoked (1)');
    expect(textOf(tree)).not.toContain('REVOKED');
    await press(tree, 'Show 1 revoked connections');
    expect(textOf(tree)).toContain('REVOKED');
  });
});

describe('SettingsScreen — trading mode', () => {
  const MODE = { mode: 'paper', source: 'default', locked: false, isAdmin: true, updatedBy: null, updatedAt: null };

  it('renders nothing for the card when the endpoint is unavailable', () => {
    setTradingMode(undefined);
    expect(textOf(render())).not.toContain('Trading mode');
  });

  it('keeps the live switch disabled until the phrase matches exactly', async () => {
    setTradingMode(MODE);
    const tree = render();
    await press(tree, 'Switch the trading mode to live');

    const enable = () => tree.root.findAll((n) => n.type === 'button' && n.props['aria-label'] === 'Enable live trading')[0];
    expect(enable().props.disabled).toBe(true);

    const input = tree.root.findAll((n) => n.type === 'input')[0];
    await act(async () => input.props.onChange({ target: { value: 'enable live trading' } }));
    expect(enable().props.disabled).toBe(true);
    await act(async () => input.props.onChange({ target: { value: 'ENABLE LIVE TRADING' } }));
    expect(enable().props.disabled).toBe(false);

    await act(async () => enable().props.onClick());
    expect(setModeMutate).toHaveBeenCalledWith({ mode: 'live', confirm: 'ENABLE LIVE TRADING' }, expect.any(Object));
  });

  it('is read-only for non-admins and disabled when locked', () => {
    setTradingMode({ ...MODE, isAdmin: false });
    let tree = render();
    expect(textOf(tree)).toContain('Only an administrator');
    expect(byLabel(tree, 'Switch the trading mode to live').length).toBe(0);

    setTradingMode({ ...MODE, locked: true });
    tree = render();
    expect(textOf(tree)).toContain('locks the trading mode');
    const btn = tree.root.findAll((n) => n.type === 'button' && n.props['aria-label'] === 'Switch the trading mode to live')[0];
    expect(btn.props.disabled).toBe(true);
  });
});
