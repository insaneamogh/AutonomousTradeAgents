import { useQuery } from '@tanstack/react-query';
import type { ActivityEntryDto } from '@app/shared-types';

import { request } from '@/lib/api';
import { QK } from '@/lib/queryClient';

export function useActivity(limit: number = 50) {
  return useQuery<ActivityEntryDto[]>({
    queryKey: QK.activity(limit),
    queryFn: ({ signal }) =>
      request<ActivityEntryDto[]>(`/api/v1/activity?limit=${limit}`, { signal }),
  });
}
