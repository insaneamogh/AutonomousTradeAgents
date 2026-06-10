/** Regret analytics — ghost P&L summary + veto ledger. */

import { useQuery } from '@tanstack/react-query';
import type { GhostSummaryResponse, VetoLedgerResponse } from '@app/shared-types';

import { request } from '@/lib/api';

export function useGhostSummary(windowDays = 30) {
  return useQuery<GhostSummaryResponse>({
    queryKey: ['ghostSummary', windowDays],
    queryFn: ({ signal }) =>
      request<GhostSummaryResponse>(`/api/v1/ghost/summary?windowDays=${windowDays}`, { signal }),
    staleTime: 60_000,
    retry: false,
  });
}

export function useVetoLedger(windowDays = 30) {
  return useQuery<VetoLedgerResponse>({
    queryKey: ['vetoLedger', windowDays],
    queryFn: ({ signal }) =>
      request<VetoLedgerResponse>(`/api/v1/risk/vetoes?windowDays=${windowDays}`, { signal }),
    staleTime: 60_000,
    retry: false,
  });
}
