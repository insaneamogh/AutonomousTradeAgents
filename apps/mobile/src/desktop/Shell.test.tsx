/**
 * Under 768px the sidebar is replaced by a bottom tab bar (five tabs, the
 * rest behind a More sheet). Hooks are mocked wholesale, same style as
 * screens/Insights.test.tsx.
 */
import { act, create } from 'react-test-renderer';

import { NavProvider } from './nav';
import { Shell, TAB_IDS } from './Shell';

jest.mock('nativewind', () => ({ useColorScheme: () => ({ colorScheme: 'dark' }) }));
jest.mock('@/hooks/useApprovals', () => ({ usePendingApprovals: () => ({ data: [] }) }));
jest.mock('@/hooks/useAccount', () => ({ useAccount: () => ({ data: undefined }) }));
jest.mock('@/hooks/useTradingMode', () => ({ useTradingMode: () => ({ data: undefined }) }));
jest.mock('@/stores/themeStore', () => ({
  useThemeStore: (sel: (s: unknown) => unknown) => sel({ setPreference: () => {} }),
}));
jest.mock('@/stores/authStore', () => ({
  useAuthStore: (sel: (s: unknown) => unknown) =>
    sel({ signOut: () => Promise.resolve(), user: { email: 'a@b.c' } }),
}));
jest.mock('./regime', () => ({ useRegime: () => ({ regime: 'neutral', label: '', loading: true }) }));

function render(width: number) {
  (global as { window?: unknown }).window = {
    innerWidth: width,
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(
      <NavProvider>
        <Shell>{null}</Shell>
      </NavProvider>,
    );
  });
  return r;
}

describe('Shell responsive navigation', () => {
  const realWindow = (global as { window?: unknown }).window;
  afterEach(() => {
    (global as { window?: unknown }).window = realWindow;
  });

  it('renders a bottom tab bar with five tabs at 375px and no sidebar', () => {
    const r = render(375);
    const bottom = r.root.findAll((n) => n.props.className === 'pg-bottom-nav');
    expect(bottom).toHaveLength(1);
    expect(r.root.findAll((n) => n.props.className === 'pg-bottom-tab')).toHaveLength(5);
    expect(TAB_IDS).toEqual(['dashboard', 'picks', 'positions', 'insights', 'settings']);
    expect(r.root.findAll((n) => n.props.className === 'pg-sidebar')).toHaveLength(0);
  });

  it('keeps the sidebar and no tab bar at 1280px', () => {
    const r = render(1280);
    expect(r.root.findAll((n) => n.props.className === 'pg-bottom-nav')).toHaveLength(0);
    expect(r.root.findAll((n) => n.props.className === 'pg-sidebar')).toHaveLength(1);
  });
});
