/** Regret analytics — ghost P&L summary + veto ledger. */

import { useQuery } from '@tanstack/react-query';
import type { GhostSummaryResponse, VetoExemplarResponse, VetoLedgerResponse } from '@app/shared-types';

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

/** The story trade for one rule (docs/IMPL_REFUSAL_LEDGER.md §2.2) — the
 * single most extreme finalized refusal under it. `rule` null disables the
 * query, for "no row selected". */
export function useVetoExemplar(rule: string | null) {
  return useQuery<VetoExemplarResponse>({
    queryKey: ['vetoExemplar', rule],
    enabled: rule != null,
    queryFn: ({ signal }) =>
      request<VetoExemplarResponse>(`/api/v1/risk/vetoes/${encodeURIComponent(rule ?? '')}/exemplar`, {
        signal,
      }),
    staleTime: 60_000,
    retry: false,
  });
}
