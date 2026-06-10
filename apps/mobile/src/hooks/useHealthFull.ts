/**
 * /api/v1/health/full hook — drives the Home system-status strip.
 *
 * Polls every 30s while Home is visible. The endpoint is cheap (it
 * reads stores already in memory) so the cadence won't hurt anything.
 * Cache stays warm so navigating away + back doesn't re-flash skeletons.
 */

import { useQuery } from '@tanstack/react-query';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export type ComponentStatus = 'ok' | 'warning' | 'danger' | 'unknown';

export interface ComponentHealth {
  status: ComponentStatus;
  label: string;
  lastEventAt: string | null;
}

export interface HealthResponse {
  council: ComponentHealth;
  approvals: ComponentHealth;
  broker: ComponentHealth;
  reconciler: ComponentHealth;
  /** Daily-cron / Reflection-cron / on-demand council all flow through
   * this. Lights up with a real YTD spend once the LiteLLM-style ledger
   * starts writing rows (Phase 4 month-1).
   */
  llmCost: ComponentHealth;
  generatedAt: string;
}

export const healthFullKey = (userId: string | null | undefined) =>
  ['health', 'full', userId ?? 'anon'] as const;

export function useHealthFull() {
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');

  return useQuery({
    queryKey: healthFullKey(userId),
    queryFn: () => request<HealthResponse>('/api/v1/health/full'),
    enabled: isAuthed,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}
