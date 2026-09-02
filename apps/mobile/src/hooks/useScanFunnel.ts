/** Symbol-scan funnel — eligible universe -> examined -> cleared the math -> admitted to the LLM. */

import { useQuery } from '@tanstack/react-query';
import type { ScanFunnelResponse } from '@app/shared-types';

import { request } from '@/lib/api';

export function useScanFunnel() {
  return useQuery<ScanFunnelResponse>({
    queryKey: ['scanFunnel'],
    queryFn: ({ signal }) => request<ScanFunnelResponse>('/api/v1/insights/scan-funnel', { signal }),
    staleTime: 60_000,
    retry: false,
  });
}
