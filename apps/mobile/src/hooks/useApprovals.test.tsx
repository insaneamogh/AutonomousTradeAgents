/**
 * useDecideApproval — regression test for a missing positions invalidation.
 *
 * Reported symptom: approve a pick, immediately open Positions, the new
 * position isn't there. The backend already has the row right after
 * approval (confirmed live against GET /api/v1/positions) — this is a
 * frontend cache-invalidation gap, not a backend lag. useOpenPositions
 * (usePositions.ts) has a 15s staleTime, so without an explicit invalidate,
 * a Positions screen mounted (or re-mounted) within that window keeps
 * serving the pre-approval cached list.
 *
 * This test drives the real useMutation/useQueryClient machinery (a live
 * QueryClient, no mocking of TanStack Query itself) and mocks only the
 * network boundary (@/lib/api's `request`), so it actually exercises
 * onSettled rather than asserting against a re-implemented mock of it.
 */
import { act, create } from 'react-test-renderer';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { DecisionResponse } from '@app/shared-types';

import { useDecideApproval } from './useApprovals';

jest.mock('@/lib/api', () => ({ request: jest.fn() }));

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { request } = jest.requireMock('@/lib/api') as { request: jest.Mock };

describe('useDecideApproval', () => {
  let qc: QueryClient;

  afterEach(() => {
    // unmount() stops the QueryClient's internal GC interval timer — without
    // it, an ad-hoc QueryClient per test leaves Jest reporting "did not exit
    // one second after the test run" (open handles), harmless but noisy.
    qc.unmount();
    jest.clearAllMocks();
  });

  it('invalidates the open-positions cache once the decision settles', async () => {
    qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const invalidateSpy = jest.spyOn(qc, 'invalidateQueries');

    const response: DecisionResponse = {
      proposalId: 'p1',
      outcome: 'approved',
      decidedAt: '2026-09-01T14:00:00Z',
      executed: true,
      order: {
        id: 'o1',
        proposalId: 'p1',
        clientOrderId: 'c1',
        symbol: 'AAPL',
        side: 'BUY',
        qty: 15,
        status: 'accepted',
        filledQty: 0,
      } as DecisionResponse['order'],
    };
    request.mockResolvedValue(response);

    // Minimal harness: no @testing-library/react-native `renderHook` in
    // this repo's toolchain (see BiometricGate.test.tsx / ExemplarCard.test.tsx
    // for the same react-test-renderer + act() convention used here), so a
    // tiny host component exposes the mutation via a captured setter.
    let mutateAsync!: ReturnType<typeof useDecideApproval>['mutateAsync'];
    function Harness() {
      // useMutation's mutate/mutateAsync are available synchronously on
      // the first render — no need to wait for a subsequent effect/render
      // before capturing it into the outer closure variable.
      mutateAsync = useDecideApproval().mutateAsync;
      return null;
    }

    await act(async () => {
      create(
        <QueryClientProvider client={qc}>
          <Harness />
        </QueryClientProvider>,
      );
    });

    await act(async () => {
      await mutateAsync({ proposalId: 'p1', outcome: 'approved' });
    });

    expect(request).toHaveBeenCalledWith(
      '/api/v1/approvals/p1/decision',
      expect.objectContaining({ method: 'POST' }),
    );

    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey);
    // This is the regression check: before the fix, 'positions' was absent
    // from this list (only ['approvals','pending'], ['account'], ['activity']
    // were invalidated), so the Positions screen's cache never learned a
    // decision had settled.
    expect(invalidatedKeys).toContainEqual(['positions']);
  });
});
