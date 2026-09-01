/**
 * BiometricGate's web/native split for the background→foreground relock.
 *
 * react-native-web's `AppState` is a thin polyfill over the Page Visibility
 * API (see node_modules/react-native-web/dist/exports/AppState/index.js):
 * switching browser tabs OR switching to a different application window BOTH
 * map to a 'background'/'active' transition — there is no real OS-level
 * "backgrounded" concept on web, only "not currently the focused tab". The
 * gate's background→foreground effect used to react to this on every
 * platform, calling `lock()` then `prompt()` (which unlocks again on web).
 * Locking flips `unlocked` to `false`, which unmounts `children` (see the
 * component's `if (unlocked)` branch) — and on the desktop web build,
 * `children` is everything below the gate, including the Platinum Glass
 * tree's `NavProvider` (`src/desktop/nav.tsx`), whose route stack is a bare
 * `useState` with no persistence. Unmounting it resets navigation to the
 * hardcoded dashboard default — reproducing "leave the Insights tab for
 * another window, come back, always on Dashboard" on every screen, every
 * time a browser tab lost and regained focus.
 *
 * These tests drive `AppState.addEventListener` directly (a spy captures
 * the registered handler) rather than the DOM Visibility API, so they don't
 * depend on a jsdom `document` or on react-native-web's polyfill at all —
 * they pin the exact contract BiometricGate has with `AppState`: it must
 * not register a background/foreground listener on web, and must keep
 * registering one on native (regression).
 */
import type { AppStateStatus } from 'react-native';
import { AppState, Platform } from 'react-native';
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';
import * as LocalAuthentication from 'expo-local-authentication';

import { setTradingUnlocked } from '@/lib/api';
import { useBiometricStore } from '@/stores/biometricStore';

import { BiometricGate } from './BiometricGate';

jest.mock('@/lib/api', () => ({
  setTradingUnlocked: jest.fn(),
  isTradingUnlocked: jest.fn(() => false),
}));

// @app/ui's Button drags in react-native-reanimated (via SkeletonLoader),
// which needs a native worklets module this test environment doesn't
// initialize. None of these tests touch the locked/read-only UI (they
// assert on AppState subscriptions + the biometric store), so a trivial
// stand-in is enough — this mirrors how other tests in this file mock
// unrelated native modules rather than pulling in the real thing.
jest.mock('@app/ui', () => ({
  Button: () => null,
}));

// expo-local-authentication is never called on the web branch of prompt()
// (it returns before touching it), and none of these tests exercise the
// native branch's real hardware probe — but the module still needs a
// harmless shape for `import * as LocalAuthentication` to resolve.
jest.mock('expo-local-authentication', () => ({
  hasHardwareAsync: jest.fn().mockResolvedValue(false),
  isEnrolledAsync: jest.fn().mockResolvedValue(false),
  authenticateAsync: jest.fn().mockResolvedValue({ success: false }),
}));

const mockHasHardwareAsync = jest.mocked(LocalAuthentication.hasHardwareAsync);

const MARKER_TEXT = 'gate-children-marker';

function Marker() {
  return null;
}
Marker.displayName = MARKER_TEXT;

function renderGate(enabled = true): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(
      <BiometricGate enabled={enabled}>
        <Marker />
      </BiometricGate>,
    );
  });
  return tree;
}

describe('BiometricGate — background/foreground relock is web/native-split', () => {
  const originalOS = Platform.OS;
  let addEventListenerSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    useBiometricStore.setState({ unlocked: false, requireOnLaunch: true });
    addEventListenerSpy = jest.spyOn(AppState, 'addEventListener');
  });

  afterEach(() => {
    (Platform as { OS: string }).OS = originalOS;
    addEventListenerSpy.mockRestore();
  });

  it('does NOT subscribe to AppState changes on web', async () => {
    (Platform as { OS: string }).OS = 'web';

    const tree = renderGate();
    // prompt()'s web branch unlocks synchronously-ish (no real await needed
    // for the mocked path); flush the microtask queue.
    await act(async () => {
      await Promise.resolve();
    });

    const changeSubscriptions = addEventListenerSpy.mock.calls.filter(
      ([type]) => type === 'change',
    );
    expect(changeSubscriptions).toHaveLength(0);

    // And children are actually mounted (the gate unlocked and rendered
    // through) — this is the state a real background/foreground cycle
    // must NOT be able to disturb on web.
    expect(tree.root.findAllByType(Marker)).toHaveLength(1);
    expect(useBiometricStore.getState().unlocked).toBe(true);
  });

  it('still subscribes to AppState changes on native (regression)', async () => {
    (Platform as { OS: string }).OS = 'ios';

    renderGate();
    await act(async () => {
      await Promise.resolve();
    });

    const changeSubscriptions = addEventListenerSpy.mock.calls.filter(
      ([type]) => type === 'change',
    );
    expect(changeSubscriptions.length).toBeGreaterThanOrEqual(1);
  });

  it('on native, backgrounding then foregrounding still relocks then re-prompts (regression)', async () => {
    (Platform as { OS: string }).OS = 'ios';
    // Start unlocked (as if a prior prompt already succeeded), so we can
    // observe the background transition actually flip it back to locked.
    useBiometricStore.setState({ unlocked: true, requireOnLaunch: true });

    renderGate();
    await act(async () => {
      await Promise.resolve();
    });

    const changeCall = addEventListenerSpy.mock.calls.find(([type]) => type === 'change') as
      | [string, (state: AppStateStatus) => void]
      | undefined;
    const handle = changeCall?.[1];
    expect(handle).toBeDefined();

    // The component's own `appState` ref starts from `AppState.currentState`,
    // which the RN jest mock never resolves to a real value (it stays
    // `null`) — seed it to 'active' first so the *next* transition is a
    // genuine active→background edge, matching what a real app sees.
    act(() => {
      handle?.('active');
    });

    act(() => {
      handle?.('background');
    });
    expect(useBiometricStore.getState().unlocked).toBe(false);
    expect(setTradingUnlocked).toHaveBeenCalledWith(false, expect.any(String));

    act(() => {
      handle?.('active');
    });
    await act(async () => {
      await Promise.resolve();
    });
    // Native's prompt() branch (mocked LocalAuthentication) fails closed —
    // the point here is only that re-foregrounding attempted a fresh
    // prompt at all (proving the listener is live), not that it succeeds.
    expect(mockHasHardwareAsync).toHaveBeenCalled();
  });
});
