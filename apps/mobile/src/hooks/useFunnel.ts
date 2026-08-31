/** Contract funnel — survivors and drop-off through each options selection stage. */

import { useQuery } from '@tanstack/react-query';
import type { FunnelResponse } from '@app/shared-types';

import { request } from '@/lib/api';

export function useFunnel(windowDays = 30) {
  return useQuery<FunnelResponse>({
    queryKey: ['funnel', windowDays],
    queryFn: ({ signal }) =>
      request<FunnelResponse>(`/api/v1/insights/funnel?windowDays=${windowDays}`, { signal }),
    staleTime: 60_000,
    retry: false,
  });
}
