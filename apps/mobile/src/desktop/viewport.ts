/**
 * Viewport width for the responsive shell.
 *
 * The desktop tree serves every web width, so the shell has to know when it
 * is on a phone (bottom tab bar, no sidebar). Layout is still done in CSS
 * (see the max-width: 767px block in theme.ts); this hook only decides WHICH
 * navigation to mount. It reads `window.innerWidth` directly rather than
 * `useWindowDimensions()`, for the reason documented in
 * src/components/DesktopShell.tsx's history: react-native-web's Dimensions
 * cache has been observed stuck at 0 on a fresh load.
 */

import { useEffect, useState } from 'react';

/** Below this width the shell is the phone layout. */
export const COMPACT_BREAKPOINT = 768;

export function isCompactWidth(width: number): boolean {
  return width < COMPACT_BREAKPOINT;
}

function readWidth(): number {
  if (typeof window !== 'undefined' && typeof window.innerWidth === 'number') {
    return window.innerWidth;
  }
  // No window (tests, SSR): assume wide, the layout the CSS defaults to.
  return 1280;
}

export function useViewportWidth(): number {
  const [width, setWidth] = useState(readWidth);
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return;
    const onResize = () => setWidth(readWidth());
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return width;
}

export function useIsCompact(): boolean {
  return isCompactWidth(useViewportWidth());
}
