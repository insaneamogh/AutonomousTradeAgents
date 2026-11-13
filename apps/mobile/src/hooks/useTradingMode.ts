/**
 * Global paper/live trading mode (`/api/v1/admin/trading-mode`).
 *
 * The endpoint may not exist on older servers: a failed load is surfaced as
 * `isError` and the card renders nothing. Setting the mode is deliberately
 * NOT optimistic — the UI only ever shows what the server persisted.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export interface TradingModeState {
  mode: 'paper' | 'live';
  source: 'env' | 'admin' | 'default';
  locked: boolean;
  isAdmin: boolean;
  updatedBy: string | null;
  updatedAt: string | null;
}

export const LIVE_CONFIRM_PHRASE = 'ENABLE LIVE TRADING';

export const tradingModeKey = (userId: string | null | undefined) =>
  ['admin', 'trading-mode', userId ?? 'anon'] as const;

export function useTradingMode() {
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  return useQuery({
    queryKey: tradingModeKey(userId),
    queryFn: () => request<TradingModeState>('/api/v1/admin/trading-mode'),
    enabled: Boolean(userId),
    staleTime: 30_000,
    retry: false,
  });
}

export function useSetTradingMode() {
  const queryClient = useQueryClient();
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  return useMutation({
    mutationFn: ({ mode, confirm }: { mode: 'paper' | 'live'; confirm?: string }) =>
      request<TradingModeState>('/api/v1/admin/trading-mode', {
        method: 'POST',
        body: confirm ? { mode, confirm } : { mode },
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(tradingModeKey(userId), updated);
      void queryClient.invalidateQueries({ queryKey: tradingModeKey(userId) });
    },
  });
}
