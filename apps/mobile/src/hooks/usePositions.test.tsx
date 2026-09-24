/**
 * useFlattenAll: the kill switch posts to the flatten-all route and, once it
 * settles, drops every cache it made stale. That includes the broker
 * connections, because the server revoked auto-approve consent and the
 * Settings toggle must not keep showing it armed.
 */
import { act, create } from 'react-test-renderer';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { FlattenAllResponse } from '@app/shared-types';

import { useFlattenAll } from './usePositions';

jest.mock('@/lib/api', () => ({ request: jest.fn() }));

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { request } = jest.requireMock('@/lib/api') as { request: jest.Mock };

describe('useFlattenAll', () => {
  let qc: QueryClient;

  afterEach(() => {
    qc.unmount();
    jest.clearAllMocks();
  });

  it('POSTs flatten-all and invalidates positions and broker connections', async () => {
    qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const invalidateSpy = jest.spyOn(qc, 'invalidateQueries');
    const response: FlattenAllResponse = {
      positions: [{ symbol: 'NVDA', closed: true, error: null }],
      autoApproveRevoked: 1,
    };
    request.mockResolvedValue(response);

    let mutateAsync!: ReturnType<typeof useFlattenAll>['mutateAsync'];
    function Harness() {
      mutateAsync = useFlattenAll().mutateAsync;
      return null;
    }

    await act(async () => {
      create(
        <QueryClientProvider client={qc}>
          <Harness />
        </QueryClientProvider>,
      );
    });

    let result: FlattenAllResponse | undefined;
    await act(async () => {
      result = await mutateAsync();
    });

    expect(request).toHaveBeenCalledWith(
      '/api/v1/positions/flatten-all',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(result).toEqual(response);
    const keys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey);
    expect(keys).toContainEqual(['positions']);
    expect(keys).toContainEqual(['broker', 'connections']);
  });
});
