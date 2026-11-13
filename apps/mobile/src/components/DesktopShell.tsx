/**
 * DesktopShell — the single switch point between the two design systems.
 *
 *   native
 *     → renders `children` untouched (the expo-router `<Slot />`),
 *       regardless of session — this covers the mobile login/verify
 *       screens too. The phone UI is byte-identical on this path: no
 *       extra View, no extra style, no desktop module ever evaluated.
 *
 *   web at any width, no session yet
 *     → renders `DesktopAuth`, the Platinum Glass pre-auth screen (a
 *       single combined email + access-token form). Also replaces the
 *       router subtree — the mobile login/verify screens never mount on
 *       this surface either, not even before sign-in.
 *
 *   web at any width with a session
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
 * The surface no longer depends on viewport width: the desktop tree
 * is responsive itself (src/desktop/theme.ts, <768px bottom tab bar).
 */

import { Platform, View } from 'react-native';

import { useAuthStore } from '@/stores/authStore';

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
 * True when the Platinum Glass tree is what's on screen — i.e. the
 * expo-router `<Slot />` is NOT mounted, whether that's `DesktopAuth`
 * (pre-auth) or `DesktopApp` (authenticated). The root layout's
 * auth-route guard reads this so it doesn't try to `router.replace()`
 * into a navigator that doesn't exist on this surface.
 */
export function useIsDesktopSurface(): boolean {
  // Every web width gets the DOM tree: it is responsive (bottom tab bar under
  // 768px), and the React Native screens it used to fall back to on a phone
  // browser were the bad UI. Native is untouched.
  return Platform.OS === 'web';
}

export function DesktopShell({ children }: { children: React.ReactNode }) {
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');

  const wideWeb = Platform.OS === 'web';

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
