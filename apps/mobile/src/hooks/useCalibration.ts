/** useCalibrationScorecard — monthly agreement + override outcomes. */

import { useQuery } from '@tanstack/react-query';
import type { ScorecardResponse } from '@app/shared-types';

import { request } from '@/lib/api';

export function useCalibrationScorecard(windowDays = 180) {
  return useQuery<ScorecardResponse>({
    queryKey: ['calibrationScorecard', windowDays],
    queryFn: ({ signal }) =>
      request<ScorecardResponse>(`/api/v1/review/scorecard?windowDays=${windowDays}`, { signal }),
    staleTime: 60_000,
    retry: false,
  });
}
