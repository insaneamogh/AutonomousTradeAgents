import { useQuery } from '@tanstack/react-query';
import type { AccountResponse } from '@app/shared-types';

import { request } from '@/lib/api';
import { QK } from '@/lib/queryClient';

export function useAccount() {
  return useQuery<AccountResponse>({
    queryKey: QK.account,
    queryFn: ({ signal }) => request<AccountResponse>('/api/v1/account', { signal }),
  });
}
