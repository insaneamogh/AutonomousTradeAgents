// Root layout.
//
// Owns:
//   - SafeAreaProvider          notch / home-bar awareness
//   - QueryClientProvider       TanStack Query for all API calls
//   - Auth bootstrap            tries silent refresh on launch
//   - Auth-route gating         redirects to /auth/login when no session
//   - Biometric gate            Face ID / Touch ID unlock on launch + resume
//   - Deep-link handler         autotrader://auth/verify?... → /auth/verify
//                               autotrader://broker/callback?... → /settings
//   - Demo-session redemption   ?demo=<token> → POST /auth/demo → signIn()
//                               (docs/IMPL_DEMO_SESSION.md)
//   - Push registration         requests OS permission + posts device token
//   - Notification handler      foreground display + tap → /approvals
//   - Theme                     applies the persisted light/dark/system
//                               preference on boot (Settings › Appearance)
//
// Order matters: registerAuthSnapshot() must run BEFORE any TanStack Query
// fetch fires (the queries read the access token via the interceptor).

import { useEffect, useState } from 'react';
import { QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import {
  Slot,
  useGlobalSearchParams,
  useRootNavigationState,
  useRouter,
  useSegments,
} from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { Platform } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import * as Linking from 'expo-linking';
import * as Notifications from 'expo-notifications';

import { BiometricGate } from '@/components/BiometricGate';
import { DemoSessionBanner } from '@/components/DemoSessionBanner';
import { DesktopShell, useIsDesktopSurface } from '@/components/DesktopShell';
import { completeAlpacaOAuth, brokerConnectionsKey } from '@/hooks/useBrokerConnections';
import { usePushRegistration } from '@/hooks/usePushRegistration';
import { registerAuthSnapshot, request } from '@/lib/api';
import { hasDemoParamInUrl, readAndStripDemoParam } from '@/lib/demoSession';
import { queryClient } from '@/lib/queryClient';
import { useAuthStore } from '@/stores/authStore';
import type { AuthStatus } from '@/stores/authStore';
import { useThemeStore } from '@/stores/themeStore';

import '../src/global.css';

// Foreground notification policy — show heads-up banner + play sound. We
// configure once at module-eval time so the policy is in place before the
// first push arrives. Per Expo Notifications API. No-op on web — the
// module has no web implementation.
if (Platform.OS !== 'web') {
  Notifications.setNotificationHandler({
    // Expo's NotificationHandler type requires a function returning a Promise.
    // eslint-disable-next-line @typescript-eslint/require-await
    handleNotification: async () => ({
      shouldShowAlert: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
      shouldShowBanner: true,
      shouldShowList: true,
    }),
  });
}

/** 'idle' / 'restoring' = authStore.restore() hasn't resolved yet. */
function isAuthBootstrapping(status: AuthStatus): boolean {
  return status === 'idle' || status === 'restoring';
}

// Wire the auth store into the API client ONCE at module-eval time. Subsequent
// calls into ``request()`` will read the current access token via the
// snapshot closure + trigger refresh on 401s.
registerAuthSnapshot(() => {
  const state = useAuthStore.getState();
  return {
    accessToken: state.accessToken,
    refresh: state.refresh,
    isBootstrapping: isAuthBootstrapping(state.status),
    // See api.ts's `waitUntilBootstrapped` docstring for why callers need
    // this at all. Zustand's `subscribe` is the store's own change feed —
    // safe to use directly here (unlike importing the store into api.ts
    // itself, which would create the cycle the lazy-getter pattern above
    // this file's docstring already calls out).
    waitUntilBootstrapped: () =>
      new Promise<void>((resolve) => {
        if (!isAuthBootstrapping(useAuthStore.getState().status)) {
          resolve();
          return;
        }
        const unsubscribe = useAuthStore.subscribe((next) => {
          if (!isAuthBootstrapping(next.status)) {
            unsubscribe();
            resolve();
          }
        });
      }),
  };
});

export default function RootLayout() {
  // Apply the persisted appearance preference before the tree renders so the
  // first paint is already in the right theme (no flash). The store's initial
  // value is read synchronously from MMKV.
  useEffect(() => {
    useThemeStore.getState().applyTheme();
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <SafeAreaProvider>
        <StatusBar style="auto" />
        <RootGate />
      </SafeAreaProvider>
    </QueryClientProvider>
  );
}

/**
 * Layered gates:
 *
 *   1. ``AuthBootstrap``   — calls ``authStore.restore()`` once.
 *   2. ``AuthRouteGuard``  — redirects /auth ↔ /(tabs) based on status.
 *   3. ``BiometricGate``   — Face ID / Touch ID on top of authenticated screens.
 */
function RootGate() {
  const status = useAuthStore((s) => s.status);
  const isAuthed = status === 'authenticated';

  // Push registration lifecycle. Runs as a hook here so it sits inside the
  // QueryClientProvider + can read the auth store; the hook itself is a
  // no-op until the user is authenticated + biometric-unlocked.
  usePushRegistration();

  // True for exactly the render(s) between "the page loaded with `?demo=`
  // in the URL" and "DemoSessionHandler's exchange settled" — see that
  // component's docstring for why AuthBootstrap/AuthRouteGuard both need
  // to know this. Computed once from the raw URL (not Expo Router's own
  // param resolution — see `hasDemoParamInUrl`), so it's already correct
  // before any child (including a data-fetching screen under `Slot`) gets
  // a chance to mount. Always false on native and for every normal load.
  const [demoPending, setDemoPending] = useState(() => hasDemoParamInUrl());

  return (
    <AuthBootstrap demoPending={demoPending}>
      <DeepLinkHandler />
      <PushTapHandler />
      <DemoSessionHandler onSettled={() => setDemoPending(false)} />
      <DemoSessionBanner />
      <AuthRouteGuard demoPending={demoPending}>
        <BiometricGate enabled={isAuthed}>
          {/* Wide web + a live session → the Platinum Glass desktop tree
              REPLACES the router subtree. Everything else (native, narrow
              web, the auth screens) renders `<Slot />` untouched. The
              switch sits here — inside AuthBootstrap — so the session
              still restores on the desktop path. */}
          <DesktopShell>
            <Slot />
          </DesktopShell>
        </BiometricGate>
      </AuthRouteGuard>
    </AuthBootstrap>
  );
}

/**
 * Calls ``restore()`` exactly once when the root mounts. Until it
 * completes, the auth status sits at 'idle' or 'restoring'; ``AuthRouteGuard``
 * doesn't redirect during that window so a fresh launch with a valid
 * refresh token doesn't briefly flash the login screen.
 *
 * Skipped when we land directly on `/auth/verify` with a token already in
 * the URL: that screen redeems the magic link and calls `signIn()` itself,
 * and it's the one that should win. Racing an unrelated `restore()` against
 * it was a real bug — `restore()`'s own `/auth/refresh` call reads whatever
 * refresh token happened to already be in storage (e.g. an older session
 * on a shared or re-used browser); if that independent call resolved
 * *after* the verify screen's `signIn()` had already set the new session,
 * its failure handler ran `clearAll()` and dropped status back to
 * 'unauthenticated' — silently logging the user back out immediately after
 * a successful login, with no error shown anywhere to explain why.
 *
 * Also skipped while `demoPending` — a `?demo=` judge link lands on a
 * normal data-bearing route (Home), not a dedicated intermediary screen
 * like `/auth/verify`, so `restore()` would otherwise race
 * `DemoSessionHandler`'s exchange the exact same way. A demo session never
 * has a stored refresh token to restore anyway (docs/IMPL_DEMO_SESSION.md
 * §2.2 — "no refresh token"), so skipping `restore()` here costs nothing.
 */
function AuthBootstrap({
  children,
  demoPending,
}: {
  children: React.ReactNode;
  demoPending: boolean;
}) {
  const restore = useAuthStore((s) => s.restore);
  const segments = useSegments();
  const params = useGlobalSearchParams<{ email?: string; token?: string }>();
  const isRedeemingMagicLink =
    (segments as readonly string[]).join('/') === 'auth/verify' &&
    Boolean(params.email && params.token);

  useEffect(() => {
    if (isRedeemingMagicLink || demoPending) return;
    void restore();
    // restore is stable (Zustand setter); isRedeemingMagicLink/demoPending
    // are read once at mount (this route doesn't change under us, and
    // demoPending's initial value is exactly what we want here) —
    // intentional one-shot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <>{children}</>;
}

/**
 * Redirects /(tabs) → /auth/login when unauthenticated, and /auth/* →
 * /(tabs) when authenticated.
 */
function AuthRouteGuard({
  children,
  demoPending,
}: {
  children: React.ReactNode;
  demoPending: boolean;
}) {
  const status = useAuthStore((s) => s.status);
  const segments = useSegments();
  const router = useRouter();
  // Navigating before the root navigator mounts throws on web (native
  // happens to mount earlier). Gate on the navigation state's key.
  const rootNavigationState = useRootNavigationState();
  // On the desktop surface the router subtree isn't rendered at all
  // (DesktopShell replaces it), so there is nothing to redirect into.
  const isDesktop = useIsDesktopSurface();

  useEffect(() => {
    if (isDesktop) return;
    if (!rootNavigationState?.key) return;
    // A `?demo=` link lands on a normal route (e.g. Home), whose own
    // queries fire with no access token yet and can flip `status` to
    // 'unauthenticated' well before DemoSessionHandler's POST /auth/demo
    // round-trip resolves (there's no stored refresh token to race
    // against, so that flip is fast). Without this guard that transient
    // 'unauthenticated' bounces the judge to /auth/login and back the
    // instant the exchange succeeds — a visible flash this guard exists
    // to prevent.
    if (demoPending) return;
    const inAuthGroup = segments[0] === 'auth';

    if (status === 'unauthenticated' && !inAuthGroup) {
      router.replace('/auth/login');
    } else if (status === 'authenticated' && inAuthGroup) {
      router.replace('/');
    }
  }, [status, segments, router, rootNavigationState?.key, isDesktop, demoPending]);

  return <>{children}</>;
}

/**
 * Listens for deep links the app supports:
 *   - ``autotrader://auth/verify?email=...&token=...``   magic-link login
 *   - ``autotrader://broker/callback?code=...&state=...`` Alpaca OAuth callback
 *   - ``autotrader://auth/google/callback?code=...``      Google sign-in callback
 *
 * Auth/verify pushes the verify screen with params; the screen auto-submits.
 *
 * Broker/callback is handled INLINE here — we don't push a dedicated screen
 * because the system browser is still focused at the moment the redirect
 * fires. We POST to the API + invalidate the broker-connections query so
 * the Settings tab refreshes when the user returns. If the POST fails we
 * route to Settings to show the error.
 *
 * Auth/google/callback is a SAFETY NET, not the primary completion path.
 * The happy path resolves entirely in-process: ``useGoogleSignIn``'s
 * ``promptAsync()`` awaits the redirect itself and never needs this
 * handler at all. Unlike broker/callback (where Alpaca's code_verifier is
 * stashed server-side, keyed by ``state``, so any handler can complete it),
 * Google's client-side PKCE exchange needs the code_verifier that lives
 * only in that hook's in-memory ``AuthRequest`` — this top-level handler
 * has no access to it and can't safely reconstruct it. So if Android
 * backgrounds the app mid-redirect and the callback lands here instead of
 * resolving ``promptAsync()`` directly, the in-process attempt is a dead
 * end either way — this just routes back to login so the user isn't
 * stranded, rather than pretend to finish an exchange it can't.
 */
function DeepLinkHandler() {
  const router = useRouter();
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  const queryClientInstance = useQueryClient();

  useEffect(() => {
    async function handle(url: string) {
      const parsed = Linking.parse(url);
      if (parsed.path === 'auth/verify' && parsed.queryParams) {
        const { email, token } = parsed.queryParams as { email?: string; token?: string };
        if (email && token) {
          router.push({ pathname: '/auth/verify', params: { email, token } });
        }
        return;
      }
      if (parsed.path === 'broker/callback' && parsed.queryParams) {
        const { code, state } = parsed.queryParams as { code?: string; state?: string };
        if (!code || !state) return;
        try {
          await completeAlpacaOAuth(code, state);
        } catch {
          // Swallow — the Settings screen reads the connection list + will
          // either show the new connection or stay in the "Connect" state.
        }
        await queryClientInstance.invalidateQueries({
          queryKey: brokerConnectionsKey(userId),
        });
        // (tabs)/settings.tsx exposes the route at /settings — group
        // segments don't appear in the URL.
        router.push('/settings');
        return;
      }
      if (parsed.path === 'auth/google/callback') {
        // See the docstring above: we deliberately do NOT attempt the PKCE
        // exchange here (no access to the code_verifier). Just make sure
        // the user lands somewhere sane instead of stuck on a blank tab.
        router.replace('/auth/login');
        return;
      }
    }

    // Cold-start case — the app was launched FROM a deep link.
    void Linking.getInitialURL().then((url) => {
      if (url) void handle(url);
    });

    // Warm-start case — already running, a new deep link arrives.
    const sub = Linking.addEventListener('url', (event) => void handle(event.url));
    return () => sub.remove();
  }, [router, queryClientInstance, userId]);

  return null;
}

/**
 * Demo-session redemption (docs/IMPL_DEMO_SESSION.md §2.4).
 *
 * On load, a `?demo=<token>` query param (the judge link) is exchanged via
 * POST /auth/demo for a real session, stored through the EXACT SAME
 * `signIn()` path a magic-link redemption uses (see auth/verify.tsx) — no
 * parallel storage. The query param is stripped from the address bar the
 * instant it's read (`readAndStripDemoParam`, before the POST is even
 * awaited) so it never lingers in the URL or rides along in a `Referer`
 * header on whatever navigation happens next, win or lose.
 *
 * `onSettled` clears the parent's `demoPending` flag once the exchange
 * resolves either way — see `AuthRouteGuard`'s docstring for why that flag
 * needs to exist at all.
 *
 * Module-level (not component state) `demoExchangeAttempted` guard for the
 * same reason auth/verify.tsx's `redeemedPairs` is module-level: a
 * `useRef` does not survive a remount (Fast Refresh in dev, or a
 * re-render of the router tree before this component settles), and this
 * must fire at most once per page load regardless. Unlike a magic-link
 * token, the demo token is meant to be reused (one link, many judges) —
 * this guard is only about not double-firing within a single page load,
 * not about one-shot redemption.
 */
let demoExchangeAttempted = false;

interface DemoIssuedTokensResponse {
  userId: string;
  email: string;
  accessToken: string;
  refreshToken: string;
  accessExpiresInSeconds: number;
  refreshExpiresInSeconds: number;
  /** Set by the demo exchange; absent from every normal login response. */
  authMethod?: string;
}

function DemoSessionHandler({ onSettled }: { onSettled: () => void }) {
  const params = useGlobalSearchParams<{ demo?: string }>();
  const signIn = useAuthStore((s) => s.signIn);
  const restore = useAuthStore((s) => s.restore);

  useEffect(() => {
    const token = params.demo;
    if (!token || demoExchangeAttempted) return;
    demoExchangeAttempted = true;

    // Strip first — before the exchange even starts — so the token is out
    // of the address bar as early as possible, regardless of how long the
    // POST takes or whether it succeeds.
    readAndStripDemoParam();

    void (async () => {
      try {
        const issued = await request<DemoIssuedTokensResponse>('/api/v1/auth/demo', {
          method: 'POST',
          body: { token },
          skipAuth: true,
        });
        await signIn(issued);
      } catch {
        // Dead/expired/malformed link — the judge just re-clicks it (the
        // link is reusable, unlike a one-shot magic link). Fall back to the
        // normal restore path so an unrelated existing session (or a clean
        // "please log in") still resolves instead of hanging at 'idle'.
        void restore();
      } finally {
        onSettled();
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.demo]);

  return null;
}

/**
 * Push-notification tap handler.
 *
 * When the user taps a proposal-pending push (from background OR a cold-
 * start tap), route them straight to the Approvals tab. The payload's
 * ``kind`` field discriminates: ``proposal_pending`` → Approvals,
 * ``zerodha_reconnect`` (the 9:00 IST daily-token reminder) → Settings.
 *
 * We also invalidate the approvals query so the inbox shows the new row
 * even if it was cached.
 */
function PushTapHandler() {
  const router = useRouter();
  const queryClientInstance = useQueryClient();

  useEffect(() => {
    // expo-notifications has no web implementation — push taps are a
    // native-only entry point.
    if (Platform.OS === 'web') return;

    function routeForKind(kind: unknown) {
      if (kind === 'proposal_pending') {
        // Invalidate so the Approvals tab fetches the new pending row.
        void queryClientInstance.invalidateQueries({ queryKey: ['approvals'] });
        router.push('/approvals');
      } else if (kind === 'zerodha_reconnect') {
        router.push('/settings');
      }
    }

    // Cold-start: the app was launched by tapping a notification.
    void Notifications.getLastNotificationResponseAsync().then((resp) => {
      if (resp?.notification?.request?.content?.data) {
        routeForKind(resp.notification.request.content.data.kind);
      }
    });

    // Warm-start: user taps a notification while the app is already in
    // memory (foreground OR background).
    const sub = Notifications.addNotificationResponseReceivedListener((resp) => {
      routeForKind(resp.notification.request.content.data?.kind);
    });
    return () => sub.remove();
  }, [router, queryClientInstance]);

  return null;
}
