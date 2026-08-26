// Watchlist — the symbols the agent tracks for this user.
//
// The daily council runs over this list (or the default 10 names when
// it's empty). Stocks + ETFs only in v1 — the server rejects anything
// else with a clear message, mirrored here in the helper copy.

import { useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import { ApiError } from '@/lib/api';
import { EmptyState, ErrorState, Skeleton, cn } from '@app/ui';

import { HeroHeadline, HeroSub, Tile, TileLabel } from '@/components/bento';
import { SymbolTypeahead } from '@/components/SymbolTypeahead';
import {
  useAddWatchlistSymbol,
  useRemoveWatchlistSymbol,
  useWatchlist,
} from '@/hooks/useWatchlist';

const SYMBOL_PATTERN = /^[A-Z][A-Z0-9.-]{0,9}$/;

export default function WatchlistScreen() {
  const router = useRouter();
  const { data: items, isLoading, isError, refetch } = useWatchlist();
  const addSymbol = useAddWatchlistSymbol();
  const removeSymbol = useRemoveWatchlistSymbol();

  // Bumped on a successful add to remount SymbolTypeahead with a fresh,
  // empty combobox — the uncontrolled-input equivalent of the old
  // `setDraft('')`. It only bumps in `onSuccess`, so a failed add leaves
  // the attempted text in place next to the error, same as before.
  const [addKey, setAddKey] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Shared by both the dropdown-select path and the type-exact-ticker
  // fallback (SymbolTypeahead calls this for either) — one validate +
  // mutate + success/error handler, not two copies of it.
  const commitSymbol = (raw: string) => {
    const symbol = raw.trim().toUpperCase();
    if (!SYMBOL_PATTERN.test(symbol)) {
      setError(`"${symbol}" isn't a valid US stock/ETF ticker.`);
      return;
    }
    setError(null);
    addSymbol.mutate(symbol, {
      onSuccess: () => setAddKey((k) => k + 1),
      onError: (err) => {
        const detail =
          err instanceof ApiError && typeof (err.body as { detail?: string })?.detail === 'string'
            ? (err.body as { detail: string }).detail
            : "Couldn't add the symbol - try again.";
        setError(detail);
      },
    });
  };

  const list = items ?? [];

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView
        contentContainerClassName="px-4 pb-16 pt-4 gap-3"
        keyboardShouldPersistTaps="handled"
      >
        <Pressable
          onPress={() => router.back()}
          accessibilityRole="button"
          accessibilityLabel="Back"
          className="min-h-[44px] justify-center"
        >
          <Text className="text-[13px] text-text-secondary dark:text-text-secondary-dark">
            ← Back
          </Text>
        </Pressable>

        <View>
          <HeroHeadline>Watchlist</HeroHeadline>
          <HeroSub>
            {list.length > 0
              ? `The agent evaluates these ${list.length} name${list.length === 1 ? '' : 's'} every trading day.`
              : 'Empty - the agent falls back to its default 10-name list.'}
          </HeroSub>
        </View>

        <Tile className="gap-2">
          <TileLabel>Add a symbol</TileLabel>
          <SymbolTypeahead key={addKey} onCommitSymbol={commitSymbol} isPending={addSymbol.isPending} />
          {error != null && (
            <Text className="text-[11px] text-rose dark:text-rose-dark">{error}</Text>
          )}
          <Text className="text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
            US stocks &amp; ETFs only in v1 - options and futures are out of scope.
          </Text>
        </Tile>

        {isLoading ? (
          <Tile className="gap-3">
            <Skeleton className="h-5 w-full" />
            <Skeleton className="h-5 w-2/3" />
          </Tile>
        ) : isError ? (
          <Tile>
            <ErrorState
              title="Couldn't load the watchlist"
              description="The agent server isn't reachable. Try again in a moment."
              onRetry={() => refetch()}
            />
          </Tile>
        ) : list.length === 0 ? (
          <Tile>
            <EmptyState
              title="No symbols yet"
              description="Add the names you want the agent to track. Until then it watches its default list (SPY, QQQ, AAPL, …)."
            />
          </Tile>
        ) : (
          list.map((item) => (
            <Tile key={item.id} inset className="flex-row items-center justify-between">
              <View>
                <Text
                  className="text-[15px] font-medium text-text-primary dark:text-text-primary-dark"
                  style={{ fontVariant: ['tabular-nums'] }}
                >
                  {item.symbol}
                </Text>
                <Text className="text-[10px] uppercase tracking-[1px] text-text-tertiary dark:text-text-tertiary-dark">
                  {item.assetClass}
                </Text>
              </View>
              {removeSymbol.isPending && removeSymbol.variables === item.symbol ? (
                <ActivityIndicator accessibilityLabel={`Removing ${item.symbol}`} />
              ) : (
                <Pressable
                  onPress={() => removeSymbol.mutate(item.symbol)}
                  accessibilityRole="button"
                  accessibilityLabel={`Remove ${item.symbol} from the watchlist`}
                  className={cn(
                    'min-h-[44px] min-w-[44px] items-center justify-center rounded-full',
                    'border border-hairline dark:border-hairline-dark',
                  )}
                >
                  <Text className="text-[13px] text-text-secondary dark:text-text-secondary-dark">
                    ✕
                  </Text>
                </Pressable>
              )}
            </Tile>
          ))
        )}
      </ScrollView>
    </SafeAreaView>
  );
}
