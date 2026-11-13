/**
 * The web surface is the DOM desktop tree at EVERY width. Before this, a
 * phone-width browser fell through to the React Native screens.
 */
import { Platform } from 'react-native';
import { act, create } from 'react-test-renderer';

import { useIsDesktopSurface } from '@/components/DesktopShell';

jest.mock('@/stores/authStore', () => ({
  useAuthStore: (sel: (s: unknown) => unknown) => sel({ status: 'authenticated' }),
}));

function setOS(os: string) {
  Object.defineProperty(Platform, 'OS', { get: () => os, configurable: true });
}

function readSurface(): boolean {
  let value: boolean | null = null;
  function Probe() {
    value = useIsDesktopSurface();
    return null;
  }
  act(() => {
    create(<Probe />);
  });
  return value as unknown as boolean;
}

describe('useIsDesktopSurface', () => {
  const realWindow = (global as { window?: unknown }).window;
  afterEach(() => {
    (global as { window?: unknown }).window = realWindow;
    setOS('ios');
  });

  it('is true on web at 375px', () => {
    setOS('web');
    (global as { window?: unknown }).window = {
      innerWidth: 375,
      addEventListener: () => {},
      removeEventListener: () => {},
    };
    expect(readSurface()).toBe(true);
  });

  it('is true on web at 1440px', () => {
    setOS('web');
    (global as { window?: unknown }).window = {
      innerWidth: 1440,
      addEventListener: () => {},
      removeEventListener: () => {},
    };
    expect(readSurface()).toBe(true);
  });

  it('stays false on native', () => {
    setOS('ios');
    expect(readSurface()).toBe(false);
  });
});
