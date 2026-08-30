/**
 * useCouncilRun — start a theater run + poll its progress.
 *
 * Transport: POST /agent/run/start → 202 {runId}, then GET
 * /agent/run/{id}/progress on a 600ms interval while status==='running'.
 * The progress response is cumulative (we poll with after=0 and let the
 * server return everything) — simpler client, the payload is tiny.
 *
 * On completion we invalidate pending approvals + activity so the feed
 * reflects the new pick the moment the user leaves the theater.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  AgentRunRequest,
  AgentRunStartResponse,
  CouncilProgressResponse,
} from '@app/shared-types';

import { request } from '@/lib/api';
import { QK } from '@/lib/queryClient';

export function useStartCouncilRun() {
  return useMutation<AgentRunStartResponse, Error, AgentRunRequest>({
    mutationFn: (body) =>
      request<AgentRunStartResponse>('/api/v1/agent/run/start', {
        method: 'POST',
        body,
        // Safe here and nowhere else in this file: starting a council run
        // places no order, so the worst case of a duplicate is one extra
        // pass (~$0.04). Without it, a container restart mid-click shows a
        // hard red error on the first thing a new visitor ever clicks —
        // every QUERY on that screen already retries twice and heals, but
        // mutations retry zero times, so this one call was the only place
        // the blip was visible.
        retryOnNetworkError: true,
      }),
  });
}

export function useCouncilProgress(runId: string | null) {
  const qc = useQueryClient();
  return useQuery<CouncilProgressResponse>({
    queryKey: ['councilRun', runId],
    enabled: runId != null,
    queryFn: async ({ signal }) => {
      const data = await request<CouncilProgressResponse>(
        `/api/v1/agent/run/${runId}/progress`,
        { signal },
      );
      if (data.status === 'completed') {
        // New pick (or veto row) exists server-side — refresh the feeds.
        void qc.invalidateQueries({ queryKey: QK.pendingApprovals });
        void qc.invalidateQueries({ queryKey: ['activity'] });
      }
      return data;
    },
    refetchInterval: (query) =>
      query.state.data?.status === 'running' || query.state.data == null ? 600 : false,
    staleTime: 0,
    gcTime: 5 * 60_000,
  });
}
