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
//   - Push registration         requests OS permission + posts device token
//   - Notification handler      foreground display + tap → /approvals
//
// Order matters: registerAuthSnapshot() must run BEFORE any TanStack Query
// fetch fires (the queries read the access token via the interceptor).
//
// Deferred:
//   - Theme provider (system / light / dark override from Settings)

import { useEffect } from 'react';
import { QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { Slot, useRootNavigationState, useRouter, useSegments } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { Platform } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import * as Linking from 'expo-linking';
import * as Notifications from 'expo-notifications';

import { BiometricGate } from '@/components/BiometricGate';
import { completeAlpacaOAuth, brokerConnectionsKey } from '@/hooks/useBrokerConnections';
import { usePushRegistration } from '@/hooks/usePushRegistration';
import { registerAuthSnapshot } from '@/lib/api';
import { queryClient } from '@/lib/queryClient';
import { useAuthStore } from '@/stores/authStore';

import '../src/global.css';

// Foreground notification policy — show heads-up banner + play sound. We
// configure once at module-eval time so the policy is in place before the
// first push arrives. Per Expo Notifications API. No-op on web — the
// module has no web implementation.
if (Platform.OS !== 'web') {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowAlert: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
      shouldShowBanner: true,
      shouldShowList: true,
    }),
  });
}

// Wire the auth store into the API client ONCE at module-eval time. Subsequent
// calls into ``request()`` will read the current access token via the
// snapshot closure + trigger refresh on 401s.
registerAuthSnapshot(() => {
  const state = useAuthStore.getState();
  return {
    accessToken: state.accessToken,
    refresh: state.refresh,
  };
});

export default function RootLayout() {
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

  return (
    <AuthBootstrap>
      <DeepLinkHandler />
      <PushTapHandler />
      <AuthRouteGuard>
        <BiometricGate enabled={isAuthed}>
          <Slot />
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
 */
function AuthBootstrap({ children }: { children: React.ReactNode }) {
  const restore = useAuthStore((s) => s.restore);
  useEffect(() => {
    void restore();
    // restore is stable (Zustand setter); intentional one-shot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <>{children}</>;
}

/**
 * Redirects /(tabs) → /auth/login when unauthenticated, and /auth/* →
 * /(tabs) when authenticated.
 */
function AuthRouteGuard({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((s) => s.status);
  const segments = useSegments();
  const router = useRouter();
  // Navigating before the root navigator mounts throws on web (native
  // happens to mount earlier). Gate on the navigation state's key.
  const rootNavigationState = useRootNavigationState();

  useEffect(() => {
    if (!rootNavigationState?.key) return;
    const inAuthGroup = segments[0] === 'auth';

    if (status === 'unauthenticated' && !inAuthGroup) {
      router.replace('/auth/login');
    } else if (status === 'authenticated' && inAuthGroup) {
      router.replace('/');
    }
  }, [status, segments, router, rootNavigationState?.key]);

  return <>{children}</>;
}

/**
 * Listens for deep links the app supports:
 *   - ``autotrader://auth/verify?email=...&token=...``   magic-link login
 *   - ``autotrader://broker/callback?code=...&state=...`` Alpaca OAuth callback
 *
 * Auth/verify pushes the verify screen with params; the screen auto-submits.
 *
 * Broker/callback is handled INLINE here — we don't push a dedicated screen
 * because the system browser is still focused at the moment the redirect
 * fires. We POST to the API + invalidate the broker-connections query so
 * the Settings tab refreshes when the user returns. If the POST fails we
 * route to Settings to show the error.
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
