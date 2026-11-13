/**
 * GET /api/v1/agent/autonomy — why the desk is or is not trading on its
 * own right now. One verdict plus a checklist; each failing check carries
 * the exact fix. Read-only.
 */
import { useQuery } from '@tanstack/react-query';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export type AutonomyState = 'running' | 'idle' | 'blocked';

export interface AutonomyCheck {
  id: string;
  label: string;
  ok: boolean;
  severity: 'blocker' | 'warning' | 'info';
  detail: string;
  fix: string | null;
}

export interface AutonomyStatus {
  state: AutonomyState;
  headline: string;
  checks: AutonomyCheck[];
  lastSweepAt: string | null;
  nextSweepAt: string | null;
  asOf: string;
}

export function useAutonomy() {
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  return useQuery({
    queryKey: ['agent', 'autonomy', userId ?? 'anon'],
    queryFn: () => request<AutonomyStatus>('/api/v1/agent/autonomy'),
    enabled: Boolean(userId),
    refetchInterval: 60_000,
    retry: false,
  });
}
