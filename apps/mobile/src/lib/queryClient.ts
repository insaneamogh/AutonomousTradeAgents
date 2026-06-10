/**
 * TanStack Query client + sensible defaults.
 *
 * Stale times reflect what we're showing:
 *   - Account: 30s. Equity moves but not by enough to need real-time.
 *   - Activity: 30s. Newest items appear on user action (POST decision invalidates).
 *   - Pending approvals: 10s. Proposals expire — fresher is better.
 */

import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      retry: 2,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
});

export const QK = {
  account: ['account'] as const,
  activity: (limit: number = 50) => ['activity', { limit }] as const,
  pendingApprovals: ['approvals', 'pending'] as const,
};
