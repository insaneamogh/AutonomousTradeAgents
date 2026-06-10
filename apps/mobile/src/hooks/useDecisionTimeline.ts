/** useDecisionTimeline — the trade biography for one decision. */

import { useQuery } from '@tanstack/react-query';
import type { DecisionTimelineResponse } from '@app/shared-types';

import { request } from '@/lib/api';

export function useDecisionTimeline(decisionId: string | null) {
  return useQuery<DecisionTimelineResponse>({
    queryKey: ['decisionTimeline', decisionId],
    enabled: decisionId != null,
    queryFn: ({ signal }) =>
      request<DecisionTimelineResponse>(`/api/v1/decisions/${decisionId}/timeline`, { signal }),
    staleTime: 30_000,
  });
}
