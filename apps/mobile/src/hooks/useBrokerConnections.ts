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

export interface StartOAuthResponse {
  authorizeUrl: string;
  state: string;
  expiresAt: string;
  devWarning: string | null;
}

export async function startAlpacaOAuth(isPaper: boolean): Promise<StartOAuthResponse> {
  return request<StartOAuthResponse>('/api/v1/broker/connect/alpaca/start', {
    method: 'POST',
    body: { isPaper },
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
