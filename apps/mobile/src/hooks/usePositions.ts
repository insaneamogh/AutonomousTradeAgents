/**
 * Positions hooks — open agent-managed positions + user-initiated close +
 * closed-position history.
 *
 * "Close now" is the in-app counterpart to letting the agent handle the
 * exit: the user can flatten any position themselves. It routes through
 * the server's deterministic risk gate (same path as the agent's own
 * closes), so the mutation can come back closed=false with a risk reason.
 *
 * Two close mutations, matching the two server routes:
 *   - useClosePosition          POST /positions/{decisionId}/close
 *     For any row with a decisionId — agent-managed or manual-mode-with-
 *     a-decision. Also doubles as "Cancel order" for a not-yet-filled row.
 *   - useCloseUnmanagedPosition POST /positions/unmanaged/{symbol}/close
 *     For a row with NO decisionId at all (`managed: false`) — a position
 *     opened outside this app, or predating this deployment's decision
 *     history. There is no decision to close "through", so this is keyed
 *     by the broker's own symbol instead.
 *
 * useClosedPositions wraps GET /positions/history — the other half of the
 * lifecycle: what was opened, when it closed, why, and what it realized.
 * No polling (unlike open positions) — history doesn't change on its own
 * between an open position closing and the next fetch invalidation below.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  ClosedPositionListResponse,
  ClosePositionResponse,
  OpenPositionDto,
} from '@app/shared-types';

import { request } from '@/lib/api';

const POSITIONS_KEY = ['positions'] as const;

export function useOpenPositions() {
  return useQuery<OpenPositionDto[]>({
    queryKey: POSITIONS_KEY,
    queryFn: ({ signal }) => request<OpenPositionDto[]>('/api/v1/positions', { signal }),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export interface ClosedPositionsFilter {
  symbol?: string;
  limit?: number;
  offset?: number;
}

export function useClosedPositions(filter: ClosedPositionsFilter = {}) {
  const { symbol, limit = 50, offset = 0 } = filter;
  const params = new URLSearchParams();
  if (symbol) params.set('symbol', symbol);
  params.set('limit', String(limit));
  params.set('offset', String(offset));

  return useQuery<ClosedPositionListResponse>({
    queryKey: ['positions', 'history', { symbol, limit, offset }],
    queryFn: ({ signal }) =>
      request<ClosedPositionListResponse>(
        `/api/v1/positions/history?${params.toString()}`,
        { signal },
      ),
    staleTime: 30_000,
  });
}

export function useClosePosition() {
  const qc = useQueryClient();
  return useMutation<ClosePositionResponse, Error, string>({
    mutationFn: (decisionId) =>
      request<ClosePositionResponse>(
        `/api/v1/positions/${encodeURIComponent(decisionId)}/close`,
        { method: 'POST' },
      ),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: POSITIONS_KEY });
      qc.invalidateQueries({ queryKey: ['portfolio'] });
      // The decision doesn't get closed_at until order_sync confirms the
      // fill (a later reconciler tick), so this row won't move to history
      // instantly — but invalidating now means the NEXT time the user
      // looks at the history tab after that tick, it's not sitting on a
      // stale 30s cache for no reason. Not applicable to the unmanaged
      // close below: that path has no decision row to ever gain a
      // closed_at in the first place.
      qc.invalidateQueries({ queryKey: ['positions', 'history'] });
    },
  });
}

export function useCloseUnmanagedPosition() {
  const qc = useQueryClient();
  return useMutation<ClosePositionResponse, Error, string>({
    mutationFn: (symbol) =>
      request<ClosePositionResponse>(
        `/api/v1/positions/unmanaged/${encodeURIComponent(symbol)}/close`,
        { method: 'POST' },
      ),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: POSITIONS_KEY });
      qc.invalidateQueries({ queryKey: ['portfolio'] });
    },
  });
}
