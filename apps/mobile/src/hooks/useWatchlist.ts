/**
 * Watchlist hooks — the symbols the agent tracks for this user.
 *
 * The daily council runs over this list (or the default 10 names when the
 * user hasn't curated one). Mutations optimistically update the cache so
 * add/remove feel instant on flaky connections.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { AddWatchlistRequest, WatchlistItemDto } from '@app/shared-types';

import { request } from '@/lib/api';

const WATCHLIST_KEY = ['watchlist'] as const;

export function useWatchlist() {
  return useQuery<WatchlistItemDto[]>({
    queryKey: WATCHLIST_KEY,
    queryFn: ({ signal }) => request<WatchlistItemDto[]>('/api/v1/watchlist', { signal }),
    staleTime: 30_000,
  });
}

export function useAddWatchlistSymbol() {
  const qc = useQueryClient();
  return useMutation<WatchlistItemDto, Error, string>({
    mutationFn: (symbol) =>
      request<WatchlistItemDto>('/api/v1/watchlist', {
        method: 'POST',
        body: { symbol } satisfies AddWatchlistRequest,
      }),
    onSettled: () => qc.invalidateQueries({ queryKey: WATCHLIST_KEY }),
  });
}

export function useRemoveWatchlistSymbol() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (symbol) =>
      request<void>(`/api/v1/watchlist/${encodeURIComponent(symbol)}`, {
        method: 'DELETE',
      }),
    onMutate: async (symbol) => {
      await qc.cancelQueries({ queryKey: WATCHLIST_KEY });
      const previous = qc.getQueryData<WatchlistItemDto[]>(WATCHLIST_KEY);
      qc.setQueryData<WatchlistItemDto[]>(WATCHLIST_KEY, (old) =>
        (old ?? []).filter((i) => i.symbol !== symbol),
      );
      return { previous };
    },
    onError: (_err, _symbol, ctx) => {
      const previous = (ctx as { previous?: WatchlistItemDto[] } | undefined)?.previous;
      if (previous) qc.setQueryData(WATCHLIST_KEY, previous);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: WATCHLIST_KEY }),
  });
}
