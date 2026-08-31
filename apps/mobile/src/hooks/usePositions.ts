/**
 * Positions hooks — open agent-managed positions + user-initiated close.
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
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { ClosePositionResponse, OpenPositionDto } from '@app/shared-types';

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
