/**
 * useRunAgent — triggers a council run via POST /api/v1/agent/run.
 *
 * On success the API has already appended any approved proposal to the
 * pending queue and the activity feed, so we just invalidate the relevant
 * queries. The Approvals screen then re-renders with the new proposal.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { AgentRunRequest, AgentRunResponse } from '@app/shared-types';

import { request } from '@/lib/api';
import { QK } from '@/lib/queryClient';

export function useRunAgent() {
  const qc = useQueryClient();
  return useMutation<AgentRunResponse, Error, AgentRunRequest>({
    mutationFn: (body) =>
      request<AgentRunResponse>('/api/v1/agent/run', { method: 'POST', body }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.pendingApprovals });
      qc.invalidateQueries({ queryKey: ['activity'] });
    },
  });
}
