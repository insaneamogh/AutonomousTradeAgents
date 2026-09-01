/**
 * DesktopShell — the single switch point between the two design systems.
 *
 *   native, OR web below 1024px
 *     → renders `children` untouched (the expo-router `<Slot />`),
 *       regardless of session — this covers the mobile login/verify
 *       screens too. The phone UI is byte-identical on this path: no
 *       extra View, no extra style, no desktop module ever evaluated.
 *
 *   web at ≥ 1024px, no session yet
 *     → renders `DesktopAuth`, the Platinum Glass pre-auth screen (a
 *       single combined email + access-token form). Also replaces the
 *       router subtree — the mobile login/verify screens never mount on
 *       this surface either, not even before sign-in.
 *
 *   web at ≥ 1024px with a session
 *     → renders `DesktopApp`, the Platinum Glass desktop tree under
 *       `src/desktop/**`. It REPLACES the router subtree rather than
 *       wrapping it, so no mobile screen mounts on desktop either.
 *
 * Two design systems, deliberately not blended:
 *   - phone  → `DESIGN.md` (calm, muted, Inter, accent-primary blue)
 *   - desktop → `STITCH_DESIGN_SYSTEM.md` (Platinum Glass)
 *
 * The desktop module is pulled in with a guarded `require` so it is never
 * evaluated in the native bundle — it is plain DOM + CSS and has no
 * meaning outside a browser.
 *
 * Width source, and why it's not `useWindowDimensions()`: on a *fresh* web
 * load (not a warm reload), react-native-web's `Dimensions` singleton has
 * been observed reporting `width: 0` on first render and never correcting
 * itself — there's no subsequent native `resize` event to trigger a re-read
 * when the viewport was already at its final size before the bundle ran.
 * That silently stuck every first-time visitor on a wide screen with the
 * phone UI. `useWebViewportWidth()` below reads `window.innerWidth`
 * directly (the DOM's own value, not RN's shimmed cache) and only falls
 * back to `useWindowDimensions()` on native, where this bug doesn't apply.
 */

import { useEffect, useState } from 'react';
import { Platform, useWindowDimensions, View } from 'react-native';

import { useAuthStore } from '@/stores/authStore';

/** Below this width the phone layout is still the better layout. */
const DESKTOP_BREAKPOINT = 1024;

/** Defensive-fallback-only column width — see the `!isAuthed` branch below. */
const AUTH_COLUMN_WIDTH = 460;

type DesktopAppComponent = () => React.ReactElement;

let cachedDesktopApp: DesktopAppComponent | null = null;

/** Web-only, lazily evaluated: keeps the desktop tree out of native runtime. */
function loadDesktopApp(): DesktopAppComponent | null {
  if (Platform.OS !== 'web') return null;
  if (cachedDesktopApp == null) {
    // Guarded dynamic require, deliberate: keeps the desktop tree out of the native bundle.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    cachedDesktopApp = (require('@/desktop/DesktopApp') as { default: DesktopAppComponent })
      .default;
  }
  return cachedDesktopApp;
}

type DesktopAuthComponent = () => React.ReactElement;

let cachedDesktopAuth: DesktopAuthComponent | null = null;

/** Same guarded-require treatment as `loadDesktopApp`, and for the same
 * reason — the pre-auth Platinum Glass screen is just as web-only. */
function loadDesktopAuth(): DesktopAuthComponent | null {
  if (Platform.OS !== 'web') return null;
  if (cachedDesktopAuth == null) {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    cachedDesktopAuth = (require('@/desktop/DesktopAuth') as { default: DesktopAuthComponent })
      .default;
  }
  return cachedDesktopAuth;
}

/**
 * Viewport width, read from the DOM directly on web (see the file-level
 * note on why `useWindowDimensions()` alone isn't reliable here) and kept
 * current via the native `resize` event. Native platforms never run the
 * web branch at all — `useWindowDimensions()` has no equivalent bug there.
 */
function useWebViewportWidth(): number {
  const rnWidth = useWindowDimensions().width;
  const [domWidth, setDomWidth] = useState(() =>
    Platform.OS === 'web' && typeof window !== 'undefined' ? window.innerWidth : rnWidth,
  );

  useEffect(() => {
    if (Platform.OS !== 'web' || typeof window === 'undefined') return;
    const onResize = () => setDomWidth(window.innerWidth);
    onResize(); // correct once more post-mount in case layout settled after first read
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  return Platform.OS === 'web' ? domWidth : rnWidth;
}

/**
 * True when the Platinum Glass tree is what's on screen — i.e. the
 * expo-router `<Slot />` is NOT mounted, whether that's `DesktopAuth`
 * (pre-auth) or `DesktopApp` (authenticated). The root layout's
 * auth-route guard reads this so it doesn't try to `router.replace()`
 * into a navigator that doesn't exist on this surface.
 */
export function useIsDesktopSurface(): boolean {
  const width = useWebViewportWidth();
  return Platform.OS === 'web' && width >= DESKTOP_BREAKPOINT;
}

export function DesktopShell({ children }: { children: React.ReactNode }) {
  const width = useWebViewportWidth();
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');

  const wideWeb = Platform.OS === 'web' && width >= DESKTOP_BREAKPOINT;

  if (!wideWeb) {
    return <>{children}</>;
  }

  // Wide web, no session yet: DesktopAuth (a Platinum Glass pre-auth
  // screen), not the mobile login/verify screens — see this file's
  // docstring. Falling back to the old phone-width-column framing of
  // `children` only if the lazy require somehow comes back empty, which
  // can't happen off-web and this branch is already web-only.
  if (!isAuthed) {
    const DesktopAuth = loadDesktopAuth();
    if (DesktopAuth == null) {
      return (
        <View className="flex-1 flex-row justify-center bg-bg-canvas dark:bg-bg-canvas-dark">
          <View className="flex-1" style={{ maxWidth: AUTH_COLUMN_WIDTH }}>
            {children}
          </View>
        </View>
      );
    }
    return <DesktopAuth />;
  }

  const DesktopApp = loadDesktopApp();
  if (DesktopApp == null) return <>{children}</>;

  return <DesktopApp />;
}
