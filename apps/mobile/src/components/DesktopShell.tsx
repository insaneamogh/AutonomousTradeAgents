/**
 * DesktopShell — centres the app in a phone-width column on wide screens.
 *
 * The UI is one codebase for phone and desktop (react-native-web renders
 * the same components). Left alone, every screen stretches edge-to-edge on
 * a 1280px monitor: bento tiles become letterboxes, the tab bar spreads
 * across a metre of pixels, and the hero numerals float in whitespace.
 *
 * Rather than fork a second desktop layout, we frame the existing one —
 * the same trick native apps use when they land on iPad. Below the
 * breakpoint (phones, and the browser at phone width) this renders
 * nothing but its children, so the mobile build is untouched.
 */

import { Platform, useWindowDimensions, View } from 'react-native';

/** Above this width we frame rather than stretch. */
const DESKTOP_BREAKPOINT = 700;

/** Phone-ish column width — matches the design system's 375–430pt target. */
const APP_MAX_WIDTH = 460;

export function DesktopShell({ children }: { children: React.ReactNode }) {
  const { width } = useWindowDimensions();

  // Native is always a phone form factor here; skip the wrapper entirely
  // so we don't add a View to every native render.
  if (Platform.OS !== 'web' || width < DESKTOP_BREAKPOINT) {
    return <>{children}</>;
  }

  return (
    <View className="flex-1 flex-row justify-center bg-bg-canvas dark:bg-bg-canvas-dark">
      <View
        className="flex-1 border-x border-hairline dark:border-hairline-dark"
        style={{ maxWidth: APP_MAX_WIDTH }}
      >
        {children}
      </View>
    </View>
  );
}
