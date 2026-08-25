/**
 * Desktop navigation.
 *
 * The desktop tree does NOT use expo-router: it replaces `<Slot />`
 * wholesale on wide web, so there is no navigator mounted underneath it.
 * A tiny reducer + context is the whole router — seven top-level sections
 * plus two detail routes, all rendered inside one persistent shell (which
 * is what makes the sidebar + header stay put between sections).
 */

import { createContext, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

export type DesktopRoute =
  | { name: 'dashboard' }
  | { name: 'picks' }
  | { name: 'pick'; id: string }
  | { name: 'council'; runId: string; symbol: string }
  | { name: 'positions' }
  | { name: 'strategies' }
  | { name: 'review' }
  | { name: 'insights' }
  | { name: 'settings' };

/** Sidebar sections (§ the ask). Detail routes map back onto one of these. */
export type SectionId =
  | 'dashboard'
  | 'picks'
  | 'positions'
  | 'strategies'
  | 'review'
  | 'insights'
  | 'settings';

export function sectionOf(route: DesktopRoute): SectionId {
  switch (route.name) {
    case 'pick':
    case 'council':
      return 'picks';
    default:
      return route.name;
  }
}

interface NavValue {
  route: DesktopRoute;
  go: (route: DesktopRoute) => void;
  back: () => void;
  canGoBack: boolean;
}

const NavContext = createContext<NavValue | null>(null);

export function NavProvider({ children }: { children: ReactNode }) {
  const [stack, setStack] = useState<DesktopRoute[]>([{ name: 'dashboard' }]);

  const value = useMemo<NavValue>(
    () => ({
      route: stack[stack.length - 1],
      go: (route) =>
        setStack((prev) => {
          // Top-level sections reset the stack; detail routes push onto it.
          const isDetail = route.name === 'pick' || route.name === 'council';
          return isDetail ? [...prev, route] : [route];
        }),
      back: () => setStack((prev) => (prev.length > 1 ? prev.slice(0, -1) : prev)),
      canGoBack: stack.length > 1,
    }),
    [stack],
  );

  return <NavContext.Provider value={value}>{children}</NavContext.Provider>;
}

export function useNav(): NavValue {
  const value = useContext(NavContext);
  if (!value) throw new Error('useNav must be used inside <NavProvider>');
  return value;
}
