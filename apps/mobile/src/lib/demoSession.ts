/**
 * Read-only demo session helpers (docs/IMPL_DEMO_SESSION.md).
 *
 * A demo session is minted for hackathon judges: it signs in through the
 * EXACT SAME `authStore.signIn()` path a magic-link login uses (see
 * `app/_layout.tsx`'s demo-link handling and `app/auth/verify.tsx`) — there
 * is no parallel storage. The only client-visible difference is
 * `user.authMethod === 'demo'`, mirrored from the server's
 * `AuthedUser.auth_method` (`apps/api/app/middleware/auth.py`) through the
 * `/auth/demo` exchange response.
 *
 * The actual enforcement is entirely server-side: `require_real_auth`
 * refuses any identity minted with `is_dev_bypass=True`, which every
 * mutating route already depends on (see the doc's §1). Everything here is
 * UI-only — it exists so a judge who taps "Approve" sees a stated reason
 * instead of a silent 401.
 */

import { Platform } from 'react-native';

import { useAuthStore } from '@/stores/authStore';

export const DEMO_DISABLED_REASON = 'Disabled in read-only demo mode';

/** True once the current session was minted via the demo-link exchange. */
export function useIsDemoSession(): boolean {
  return useAuthStore((s) => s.user?.authMethod === 'demo');
}

/**
 * Whether the browser's CURRENT address bar carries a `?demo=` param.
 *
 * Read directly from `window.location` (not through Expo Router's own
 * param resolution) so it reflects reality at the instant it's called —
 * including immediately after `readAndStripDemoParam()` has removed it.
 * Native has no address bar and no `window`; always false there.
 */
export function hasDemoParamInUrl(): boolean {
  if (Platform.OS !== 'web' || typeof window === 'undefined') return false;
  try {
    return new URLSearchParams(window.location.search).has('demo');
  } catch {
    return false;
  }
}

/**
 * Read the `demo` token out of the current URL and strip it immediately —
 * before the caller does anything else with it — via `history.replaceState`
 * so the token never lingers in the address bar or rides along in a
 * `Referer` header on whatever navigation happens next. Returns `null` on
 * native (no address bar) or when there's no `demo` param to begin with.
 *
 * `replaceState` (not `pushState`): this must not add a back-button entry
 * that still has the token in it.
 */
export function readAndStripDemoParam(): string | null {
  if (Platform.OS !== 'web' || typeof window === 'undefined') return null;
  try {
    const url = new URL(window.location.href);
    const token = url.searchParams.get('demo');
    if (!token) return null;
    url.searchParams.delete('demo');
    window.history.replaceState({}, '', url.toString());
    return token;
  } catch {
    return null;
  }
}
