/**
 * DesktopShell — the single switch point between the two design systems.
 *
 *   native, OR web below 1024px, OR no session
 *     → renders `children` untouched (the expo-router `<Slot />`).
 *       The phone UI is byte-identical on this path: no extra View, no
 *       extra style, no desktop module ever evaluated.
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
 */

import { Platform, useWindowDimensions, View } from 'react-native';

import { useAuthStore } from '@/stores/authStore';

/** Below this width the phone layout is still the better layout. */
const DESKTOP_BREAKPOINT = 1024;

/** Phone-ish column for the pre-session (auth) screens on a wide browser. */
const AUTH_COLUMN_WIDTH = 460;

type DesktopAppComponent = () => React.ReactElement;

let cachedDesktopApp: DesktopAppComponent | null = null;

/** Web-only, lazily evaluated: keeps the desktop tree out of native runtime. */
function loadDesktopApp(): DesktopAppComponent | null {
  if (Platform.OS !== 'web') return null;
  if (cachedDesktopApp == null) {
    // eslint-disable-next-line @typescript-eslint/no-var-requires, global-require
    cachedDesktopApp = (require('@/desktop/DesktopApp') as { default: DesktopAppComponent }).default;
  }
  return cachedDesktopApp;
}

/**
 * True when the Platinum Glass tree is what's on screen — i.e. the
 * expo-router `<Slot />` is NOT mounted. The root layout's auth-route
 * guard reads this so it doesn't try to `router.replace()` into a
 * navigator that doesn't exist on this surface.
 */
export function useIsDesktopSurface(): boolean {
  const { width } = useWindowDimensions();
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');
  return Platform.OS === 'web' && width >= DESKTOP_BREAKPOINT && isAuthed;
}

export function DesktopShell({ children }: { children: React.ReactNode }) {
  const { width } = useWindowDimensions();
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');

  const wideWeb = Platform.OS === 'web' && width >= DESKTOP_BREAKPOINT;

  if (!wideWeb) {
    return <>{children}</>;
  }

  // Wide web, no session yet: the auth screens are the mobile ones. Frame
  // them in a phone-width column rather than stretching them across a
  // metre of pixels. (Web-only branch — native never reaches here.)
  if (!isAuthed) {
    return (
      <View className="flex-1 flex-row justify-center bg-bg-canvas dark:bg-bg-canvas-dark">
        <View className="flex-1" style={{ maxWidth: AUTH_COLUMN_WIDTH }}>
          {children}
        </View>
      </View>
    );
  }

  const DesktopApp = loadDesktopApp();
  if (DesktopApp == null) return <>{children}</>;

  return <DesktopApp />;
}
