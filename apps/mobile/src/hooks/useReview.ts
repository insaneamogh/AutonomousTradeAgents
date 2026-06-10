/**
 * TanStack Query bindings for /api/v1/review.
 *
 * The queue query is the primary one — the Review screen mounts it and
 * the swipe-deck consumes it. The grade mutation invalidates the queue
 * + the agreement query so the Home strip's stat stays current.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export type Grade = 'good' | 'bad' | 'skip';

export interface ReviewQueueItem {
  decisionId: string;
  triggeredAt: string;
  symbol: string;
  side: string;
  qty: number | null;
  fillQty: number | null;
  fillAvgPrice: number | null;
  realizedPnl: number | null;
  selectedStrategy: string | null;
  selectorConfidence: number;
  bullCase: string;
  bearCase: string;
  regime: string | null;
}

export interface ReviewQueueResponse {
  items: ReviewQueueItem[];
  totalInWindow: number;
  gradedInWindow: number;
}

export interface AgreementBucket {
  operatorGrade: Grade;
  reflectionDirection: 'positive' | 'negative' | 'neutral';
  count: number;
}

export interface AgreementResponse {
  windowDays: number;
  totalReviewed: number;
  agreementPct: number;
  buckets: AgreementBucket[];
}

export const reviewQueueKey = (userId: string | null | undefined, windowDays: number) =>
  ['review', 'queue', userId ?? 'anon', windowDays] as const;

export const reviewAgreementKey = (userId: string | null | undefined, windowDays: number) =>
  ['review', 'agreement', userId ?? 'anon', windowDays] as const;

export function useReviewQueue(windowDays = 30) {
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');

  return useQuery({
    queryKey: reviewQueueKey(userId, windowDays),
    queryFn: () =>
      request<ReviewQueueResponse>(
        `/api/v1/review/queue?windowDays=${windowDays}`,
      ),
    enabled: isAuthed,
    staleTime: 60_000,
  });
}

export function useReviewAgreement(windowDays = 30) {
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');

  return useQuery({
    queryKey: reviewAgreementKey(userId, windowDays),
    queryFn: () =>
      request<AgreementResponse>(
        `/api/v1/review/agreement?windowDays=${windowDays}`,
      ),
    enabled: isAuthed,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

export function useGradeDecision(windowDays = 30) {
  const queryClient = useQueryClient();
  const userId = useAuthStore((s) => s.user?.userId ?? null);

  return useMutation({
    mutationFn: ({
      decisionId,
      grade,
      notes,
    }: {
      decisionId: string;
      grade: Grade;
      notes?: string;
    }) =>
      request<{ id: string; grade: Grade; reviewedAt: string }>(
        `/api/v1/review/${decisionId}`,
        { method: 'POST', body: { grade, notes } },
      ),
    onMutate: async ({ decisionId }) => {
      // Optimistic: remove the graded item from the queue so the swipe
      // deck visibly advances even before the round-trip finishes.
      await queryClient.cancelQueries({ queryKey: reviewQueueKey(userId, windowDays) });
      const prev = queryClient.getQueryData<ReviewQueueResponse>(
        reviewQueueKey(userId, windowDays),
      );
      if (prev) {
        queryClient.setQueryData<ReviewQueueResponse>(
          reviewQueueKey(userId, windowDays),
          {
            ...prev,
            items: prev.items.filter((i) => i.decisionId !== decisionId),
            gradedInWindow: prev.gradedInWindow + 1,
          },
        );
      }
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      // Rollback on failure.
      if (ctx?.prev) {
        queryClient.setQueryData(
          reviewQueueKey(userId, windowDays),
          ctx.prev,
        );
      }
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: reviewQueueKey(userId, windowDays) });
      queryClient.invalidateQueries({ queryKey: reviewAgreementKey(userId, windowDays) });
    },
  });
}
