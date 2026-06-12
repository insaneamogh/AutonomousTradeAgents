/**
 * Approval hooks — pending list + decision mutation.
 *
 * The mutation optimistically removes the proposal from the cached list,
 * then invalidates account + activity so the related views reflect the
 * decision without a manual refetch.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  ApprovalProposalDto,
  DecisionRequest,
  DecisionResponse,
  ExitMode,
} from '@app/shared-types';

import { request } from '@/lib/api';
import { QK } from '@/lib/queryClient';

export function usePendingApprovals() {
  return useQuery<ApprovalProposalDto[]>({
    queryKey: QK.pendingApprovals,
    queryFn: ({ signal }) =>
      request<ApprovalProposalDto[]>('/api/v1/approvals/pending', { signal }),
    staleTime: 10_000,
  });
}

interface DecideArgs {
  proposalId: string;
  outcome: DecisionRequest['outcome'];
  /** Close delegation. Only meaningful on approvals; defaults to 'agent'. */
  exitMode?: ExitMode;
  note?: string;
}

export function useDecideApproval() {
  const qc = useQueryClient();
  return useMutation<DecisionResponse, Error, DecideArgs>({
    mutationFn: ({ proposalId, outcome, exitMode, note }) =>
      request<DecisionResponse>(`/api/v1/approvals/${proposalId}/decision`, {
        method: 'POST',
        body: { outcome, exitMode, note } satisfies DecisionRequest,
      }),
    onMutate: async ({ proposalId }) => {
      await qc.cancelQueries({ queryKey: QK.pendingApprovals });
      const previous = qc.getQueryData<ApprovalProposalDto[]>(QK.pendingApprovals);
      // Optimistic: drop the proposal immediately.
      qc.setQueryData<ApprovalProposalDto[]>(
        QK.pendingApprovals,
        (old) => (old ?? []).filter((p) => p.id !== proposalId),
      );
      return { previous };
    },
    onError: (_err, _vars, ctx) => {
      const previous = (ctx as { previous?: ApprovalProposalDto[] } | undefined)?.previous;
      if (previous) qc.setQueryData(QK.pendingApprovals, previous);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.pendingApprovals });
      qc.invalidateQueries({ queryKey: QK.account });
      qc.invalidateQueries({ queryKey: ['activity'] });
    },
  });
}
