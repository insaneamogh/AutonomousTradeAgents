/**
 * Per-strategy performance hook — wraps GET /api/v1/strategies/performance.
 *
 * Refetches lazily (60s stale time) — the underlying data only changes
 * once per council pass and once per Reflection cycle, so we don't need
 * tight polling.
 */

import { useQuery } from '@tanstack/react-query';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export interface StrategyPerformance {
  strategyId: string;
  displayName: string;
  confidence: number;
  decisionsInWindow: number;
  wins: number;
  losses: number;
  realizedPnl: number;
  avgWinnerPct: number | null;
  avgLoserPct: number | null;
  lastDecisionAt: string | null;
  lastReflectionAt: string | null;
}

export interface StrategiesPerformanceResponse {
  windowDays: number;
  strategies: StrategyPerformance[];
  generatedAt: string;
}

export const strategiesPerformanceKey = (
  userId: string | null | undefined,
  windowDays: number,
) => ['strategies', 'performance', userId ?? 'anon', windowDays] as const;

export function useStrategiesPerformance(windowDays: number = 30) {
  const userId = useAuthStore((s) => s.user?.userId ?? null);

  return useQuery({
    queryKey: strategiesPerformanceKey(userId, windowDays),
    queryFn: () =>
      request<StrategiesPerformanceResponse>(
        `/api/v1/strategies/performance?windowDays=${windowDays}`,
      ),
    enabled: Boolean(userId),
    staleTime: 60_000,
  });
}
