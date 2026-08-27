/**
 * The browsable decision list — wraps GET /api/v1/decisions.
 *
 * Every council pass writes exactly one decision row, whether or not it
 * ever became a proposal. Before this hook existed, a HOLD from a
 * strategy-fit short-circuit — most council runs on any sweep — had no
 * id reachable from anywhere in the app once the sweep moved past it.
 */

import { useQuery } from '@tanstack/react-query';
import type { DecisionListResponse } from '@app/shared-types';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export interface DecisionsFilter {
  symbol?: string;
  action?: 'BUY' | 'SELL' | 'HOLD';
  limit?: number;
  offset?: number;
}

export const decisionsKey = (
  userId: string | null | undefined,
  filter: DecisionsFilter,
) => ['decisions', 'list', userId ?? 'anon', filter] as const;

export function useDecisions(filter: DecisionsFilter = {}) {
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  const { symbol, action, limit = 50, offset = 0 } = filter;

  const params = new URLSearchParams();
  if (symbol) params.set('symbol', symbol);
  if (action) params.set('action', action);
  params.set('limit', String(limit));
  params.set('offset', String(offset));

  return useQuery({
    queryKey: decisionsKey(userId, { symbol, action, limit, offset }),
    queryFn: () => request<DecisionListResponse>(`/api/v1/decisions?${params.toString()}`),
    enabled: Boolean(userId),
    staleTime: 30_000,
  });
}
