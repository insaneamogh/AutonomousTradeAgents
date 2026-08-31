/**
 * demoSession.ts tests (docs/IMPL_DEMO_SESSION.md).
 *
 * This project's Jest environment is plain `jest-environment-node` (see
 * `react-native/jest/react-native-env.js`, which jest-expo's preset uses) —
 * not jsdom — so `window` does not exist by default. `URL`/`URLSearchParams`
 * ARE real Node globals and need no mocking; only `window.location` /
 * `window.history` need a minimal stand-in, built to actually reflect a
 * `history.replaceState` call back into `window.location` the way a real
 * browser would, so `hasDemoParamInUrl()` can be checked before AND after
 * stripping.
 */
import { createElement } from 'react';
import { Platform } from 'react-native';
import { act, create } from 'react-test-renderer';

import { useAuthStore } from '@/stores/authStore';

import { hasDemoParamInUrl, readAndStripDemoParam, useIsDemoSession } from './demoSession';

type FakeWindow = {
  location: { href: string; search: string };
  history: { replaceState: jest.Mock<void, [unknown, string, string]> };
};

/** Installs `global.window` with the given URL, wired so `replaceState`
 * actually updates `window.location` (a real browser does this too). */
function installWindow(href: string): FakeWindow {
  const state = { href };
  const win: FakeWindow = {
    get location() {
      const u = new URL(state.href);
      return { href: u.href, search: u.search };
    },
    history: {
      replaceState: jest.fn((_data: unknown, _title: string, url: string) => {
        state.href = new URL(url, state.href).toString();
      }),
    },
  };
  (global as unknown as { window: FakeWindow }).window = win;
  return win;
}

describe('hasDemoParamInUrl / readAndStripDemoParam', () => {
  const originalOS = Platform.OS;
  const originalWindow = (global as unknown as { window?: FakeWindow }).window;

  afterEach(() => {
    (Platform as { OS: string }).OS = originalOS;
    (global as unknown as { window?: FakeWindow }).window = originalWindow;
  });

  it('is false/null on native, even with a demo param in the URL', () => {
    (Platform as { OS: string }).OS = 'ios';
    installWindow('https://judge.example.com/?demo=secret-token');

    expect(hasDemoParamInUrl()).toBe(false);
    expect(readAndStripDemoParam()).toBeNull();
  });

  it('is false on web with no demo param', () => {
    (Platform as { OS: string }).OS = 'web';
    installWindow('https://judge.example.com/');

    expect(hasDemoParamInUrl()).toBe(false);
    expect(readAndStripDemoParam()).toBeNull();
  });

  it('reads the token and strips ONLY the demo param via history.replaceState, preserving the rest of the URL', () => {
    (Platform as { OS: string }).OS = 'web';
    const win = installWindow('https://judge.example.com/approvals?demo=secret-token&utm_source=x');

    expect(hasDemoParamInUrl()).toBe(true);

    const token = readAndStripDemoParam();

    expect(token).toBe('secret-token');
    expect(win.history.replaceState).toHaveBeenCalledTimes(1);
    const strippedUrl = win.history.replaceState.mock.calls[0][2];
    expect(strippedUrl).not.toContain('demo=');
    expect(strippedUrl).toContain('utm_source=x');
    expect(strippedUrl).toContain('/approvals');
  });

  it('reports false once the param has actually been removed from window.location (replaceState, not pushState)', () => {
    (Platform as { OS: string }).OS = 'web';
    installWindow('https://judge.example.com/?demo=secret-token');

    readAndStripDemoParam();

    expect(hasDemoParamInUrl()).toBe(false);
  });

  it('returns null on a second read — nothing left to strip', () => {
    (Platform as { OS: string }).OS = 'web';
    const win = installWindow('https://judge.example.com/?demo=secret-token');

    expect(readAndStripDemoParam()).toBe('secret-token');
    expect(readAndStripDemoParam()).toBeNull();
    // Only the first read touched history — the second found nothing to strip.
    expect(win.history.replaceState).toHaveBeenCalledTimes(1);
  });
});

describe('useIsDemoSession', () => {
  beforeEach(() => {
    useAuthStore.setState({ status: 'idle', user: null, accessToken: null });
  });

  // A hook can only run inside a component's render — mount a throwaway
  // probe via react-test-renderer (this repo's established style, see
  // SymbolResultsList.test.tsx) rather than calling the hook bare. Plain
  // `createElement` (not JSX) so this file can stay a `.ts`, not `.tsx`.
  function readHookValue(): boolean {
    let captured: boolean | undefined;
    function Probe() {
      captured = useIsDemoSession();
      return null;
    }
    act(() => {
      create(createElement(Probe));
    });
    return captured as boolean;
  }

  it('is false with no signed-in user', () => {
    expect(readHookValue()).toBe(false);
  });

  it('is false for a normal (non-demo) session', () => {
    useAuthStore.setState({
      status: 'authenticated',
      accessToken: 'a',
      user: { userId: 'u1', email: 'judge@example.com' },
    });
    expect(readHookValue()).toBe(false);
  });

  it('is true only when authMethod is exactly "demo"', () => {
    useAuthStore.setState({
      status: 'authenticated',
      accessToken: 'a',
      user: { userId: 'u1', email: 'judge@example.com', authMethod: 'demo' },
    });
    expect(readHookValue()).toBe(true);
  });
});
