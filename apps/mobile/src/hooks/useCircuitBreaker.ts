/**
 * Circuit-breaker hooks — drives the persistent drawdown danger banner.
 *
 * Polls status on an interval so the banner appears within a tick of the
 * reconciler tripping the breaker. Acknowledging flips the breaker to
 * manual_override server-side (resume trading) and clears the banner.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { CircuitBreakerResponse } from '@app/shared-types';

import { request } from '@/lib/api';

const KEY = ['circuit-breaker'] as const;

export function useCircuitBreaker() {
  return useQuery<CircuitBreakerResponse>({
    queryKey: KEY,
    queryFn: ({ signal }) => request<CircuitBreakerResponse>('/api/v1/circuit-breaker', { signal }),
    staleTime: 20_000,
    refetchInterval: 30_000,
  });
}

export function useAcknowledgeBreaker() {
  const qc = useQueryClient();
  return useMutation<CircuitBreakerResponse, Error, void>({
    mutationFn: () =>
      request<CircuitBreakerResponse>('/api/v1/circuit-breaker/acknowledge', {
        method: 'POST',
      }),
    onSuccess: (res) => qc.setQueryData(KEY, res),
    onSettled: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}
