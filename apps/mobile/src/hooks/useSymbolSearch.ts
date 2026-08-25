/**
 * useSymbolSearch — ticker typeahead against the broker's own universe.
 *
 * Debounced so a fast typist doesn't fire a request per keystroke. The
 * server holds the ~13.4k-symbol list in memory, so a query is a few
 * milliseconds once warm; the debounce is about request volume, not
 * server cost.
 *
 * Returns [] rather than erroring when the deployment has no data keys —
 * the caller falls back to plain text entry, which the run endpoint
 * still validates before spending a council pass.
 */

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { request } from '@/lib/api';

export interface SymbolHit {
  symbol: string;
  name: string;
  fractionable: boolean;
}

/** Debounce a fast-changing value. */
export function useDebounced<T>(value: T, ms = 180): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return settled;
}

export function useSymbolSearch(query: string, { limit = 8 }: { limit?: number } = {}) {
  const debounced = useDebounced(query.trim(), 180);

  return useQuery<SymbolHit[]>({
    queryKey: ['symbolSearch', debounced, limit],
    enabled: debounced.length > 0,
    queryFn: ({ signal }) =>
      request<SymbolHit[]>(
        `/api/v1/symbols/search?q=${encodeURIComponent(debounced)}&limit=${limit}`,
        { signal },
      ),
    // The tradable universe barely moves; keep results across re-opens
    // of the dropdown so re-typing a query is instant.
    staleTime: 10 * 60_000,
    retry: false,
  });
}
