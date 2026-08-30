/**
 * TanStack Query bindings for /api/v1/broker.
 *
 * The list query keys on the auth-store user id so that switching accounts
 * (theoretical — Phase 3.1 is single-user) doesn't surface another user's
 * connections from the cache.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export interface BrokerConnection {
  id: string;
  broker: string;
  isPaper: boolean;
  accountNumber: string | null;
  status: 'active' | 'revoked' | 'expired';
  /**
   * Not persisted server-side — recomputed on every response (see
   * `_connection_source` in `apps/api/app/routers/broker.py`). "environment"
   * means the API auto-created this connection from its own Alpaca API
   * keys rather than the user completing OAuth; revoking one of these will
   * relink automatically on the next server boot while those keys are
   * still configured.
   */
  connectionSource: 'environment' | 'oauth';
  /**
   * Per-connection consent for the auto-approve sweeper (see
   * `docs/PLAN_AUTO_APPROVE.md`). When true — AND the server's
   * `AUTO_APPROVE_ENABLED` flag is on — the reconciler's sweeper may
   * execute a pending proposal on this connection with no human tap, up
   * to the daily cap, stamping `approvalMode: 'auto'` on the decision row.
   * Paper-only is enforced server-side (plan §2, gate 2) and is NOT
   * something this flag can override.
   */
  autoApproveConsent: boolean;
  createdAt: string;
  lastUsedAt: string | null;
}

export const brokerConnectionsKey = (userId: string | null | undefined) =>
  ['broker', 'connections', userId ?? 'anon'] as const;

export function useBrokerConnections() {
  const userId = useAuthStore((s) => s.user?.userId ?? null);

  return useQuery({
    queryKey: brokerConnectionsKey(userId),
    queryFn: () => request<BrokerConnection[]>('/api/v1/broker/connections'),
    enabled: Boolean(userId),
    staleTime: 60_000,
  });
}

export function useRevokeBrokerConnection() {
  const queryClient = useQueryClient();
  const userId = useAuthStore((s) => s.user?.userId ?? null);

  return useMutation({
    mutationFn: (connectionId: string) =>
      request<BrokerConnection>(`/api/v1/broker/connections/${connectionId}`, {
        method: 'DELETE',
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: brokerConnectionsKey(userId) });
    },
  });
}

export interface SetAutoApproveConsentArgs {
  connectionId: string;
  enabled: boolean;
}

/**
 * Arm/disarm the auto-approve sweeper for one connection
 * (`docs/PLAN_AUTO_APPROVE.md`).
 *
 * Deliberately NOT optimistic. This flips a real "the agent may open a
 * position with no human in the loop" switch, so the UI must only ever
 * show what the server actually persisted — never a local guess. Callers
 * should render a busy/pending state from `isPending` rather than
 * flipping local state ahead of the response; on failure there is
 * nothing to revert, because nothing was changed ahead of time — the
 * cached connection (and therefore the calling pill) simply still shows
 * its last-known-good `autoApproveConsent` value, and `isError` is there
 * to surface the failure.
 */
export function useSetAutoApproveConsent() {
  const queryClient = useQueryClient();
  const userId = useAuthStore((s) => s.user?.userId ?? null);

  return useMutation({
    mutationFn: ({ connectionId, enabled }: SetAutoApproveConsentArgs) =>
      request<BrokerConnection>(
        `/api/v1/broker/connections/${connectionId}/auto-approve-consent`,
        { method: 'POST', body: { enabled } },
      ),
    onSuccess: (updated) => {
      queryClient.setQueryData<BrokerConnection[]>(brokerConnectionsKey(userId), (prev) =>
        prev ? prev.map((c) => (c.id === updated.id ? updated : c)) : prev,
      );
      void queryClient.invalidateQueries({ queryKey: brokerConnectionsKey(userId) });
    },
  });
}

export interface StartOAuthResponse {
  authorizeUrl: string;
  state: string;
  expiresAt: string;
  /** Human-readable, for display only — may combine multiple unrelated
   * warnings. Don't string-match it; check `oauthNotConfigured` instead. */
  devWarning: string | null;
  /** True when the server has no real Alpaca OAuth app configured — the
   * dev placeholder client id in `authorizeUrl` is guaranteed to be
   * rejected by Alpaca's own page with a generic "unknown client" error.
   * Callers should show `devWarning` instead of navigating. */
  oauthNotConfigured: boolean;
}

/**
 * `platform: 'web'` tells the server to hand back an authorize URL whose
 * `redirect_uri` is the HTTPS browser-redirect landing page
 * (`GET /connect/alpaca/redirect`) instead of the native `autotrader://`
 * deep link — the desktop/web build can't catch a custom URL scheme.
 * Omit it (the native call site does) to keep today's deep-link default.
 */
export async function startAlpacaOAuth(
  isPaper: boolean,
  platform?: 'web',
): Promise<StartOAuthResponse> {
  return request<StartOAuthResponse>('/api/v1/broker/connect/alpaca/start', {
    method: 'POST',
    body: { isPaper, ...(platform ? { platform } : {}) },
  });
}

export interface StartZerodhaResponse {
  loginUrl: string;
  state: string;
  expiresAt: string;
  devWarning: string | null;
}

/**
 * Kite Connect is not OAuth: the user logs in at kite.zerodha.com and
 * Zerodha redirects to the API's registered redirect URL, which completes
 * the connection server-side. The app only needs to open `loginUrl` and
 * refetch the connections list afterwards.
 */
export async function startZerodhaConnect(): Promise<StartZerodhaResponse> {
  return request<StartZerodhaResponse>('/api/v1/broker/connect/zerodha/start', {
    method: 'POST',
  });
}

export interface CallbackResponse {
  connection: BrokerConnection;
}

export async function completeAlpacaOAuth(
  code: string,
  state: string,
): Promise<CallbackResponse> {
  return request<CallbackResponse>('/api/v1/broker/connect/alpaca/callback', {
    method: 'POST',
    body: { code, state },
  });
}
