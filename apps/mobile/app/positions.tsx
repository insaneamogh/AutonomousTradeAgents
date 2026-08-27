// Positions — open agent-managed holdings + a manual "Close now" control.
//
// Each row is an open agent decision (approved + filled + not closed). It
// shows the disclosed exit plan (who closes it + when) and unrealized P&L
// from the latest reconciler mark. "Close now" flattens the position via
// the server's risk-gated close — the in-app counterpart to letting the
// agent handle the exit. Entries are never auto-placed; this is exit-only.

import { Alert, Pressable, RefreshControl, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import * as Haptics from 'expo-haptics';

import { ApiError } from '@/lib/api';
import { EmptyState, ErrorState, Skeleton, cn } from '@app/ui';

import { DirectionPill, HeroHeadline, HeroSub, Tile, TileLabel } from '@/components/bento';
import { useClosePosition, useOpenPositions } from '@/hooks/usePositions';

function money(n: number | null): string {
  if (n == null) return '—';
  const sign = n < 0 ? '-' : '';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

export default function PositionsScreen() {
  const router = useRouter();
  const { data, isLoading, isError, refetch } = useOpenPositions();
  const close = useClosePosition();
  const list = data ?? [];

  const confirmClose = (decisionId: string, symbol: string, qty: number) => {
    Alert.alert(
      `Close ${qty} ${symbol}?`,
      'This places a market sell now, through the same risk checks the agent uses. Resting stop/target orders are cancelled first.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Close position',
          style: 'destructive',
          onPress: () => {
            Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});
            close.mutate(decisionId, {
              onSuccess: (res) => {
                if (!res.closed) {
                  Haptics.notificationAsync(
                    Haptics.NotificationFeedbackType.Warning,
                  ).catch(() => {});
                  Alert.alert(
                    'Not closed',
                    res.detail ?? 'A risk rule blocked the close — try again shortly.',
                  );
                }
              },
              onError: (err) => {
                const detail =
                  err instanceof ApiError &&
                  typeof (err.body as { detail?: string })?.detail === 'string'
                    ? (err.body as { detail: string }).detail
                    : "Couldn't reach the agent server.";
                Alert.alert('Close failed', detail);
              },
            });
          },
        },
      ],
    );
  };

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView
        contentContainerClassName="px-4 pb-16 pt-4 gap-3"
        refreshControl={
          <RefreshControl refreshing={isLoading} onRefresh={() => refetch()} />
        }
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
          <HeroHeadline>Positions</HeroHeadline>
          <HeroSub>
            {list.length > 0
              ? `${list.length} open position${list.length === 1 ? '' : 's'} the agent is managing.`
              : 'No open agent positions right now.'}
          </HeroSub>
        </View>

        {isLoading ? (
          <Tile className="gap-3">
            <Skeleton className="h-5 w-full" />
            <Skeleton className="h-5 w-2/3" />
          </Tile>
        ) : isError ? (
          <Tile>
            <ErrorState
              title="Couldn't load positions"
              description="The agent server isn't reachable. Try again in a moment."
              onRetry={() => refetch()}
            />
          </Tile>
        ) : list.length === 0 ? (
          <Tile>
            <EmptyState
              title="Nothing open"
              description="When you approve a trade, it shows here with its exit plan and a close-now control."
            />
          </Tile>
        ) : (
          list.map((p) => {
            const pnl = p.unrealizedPnl;
            const pnlClass =
              pnl == null
                ? 'text-text-tertiary dark:text-text-tertiary-dark'
                : pnl >= 0
                  ? 'text-gain dark:text-gain-dark'
                  : 'text-rose dark:text-rose-dark';
            const busy = close.isPending && close.variables === p.decisionId;
            return (
              <Tile key={p.decisionId ?? `broker:${p.symbol}`} className="gap-2">
                <View className="flex-row items-center justify-between">
                  <Text
                    className="text-[16px] font-semibold text-text-primary dark:text-text-primary-dark"
                    style={{ fontVariant: ['tabular-nums'] }}
                  >
                    {p.side} {p.qty} {p.symbol}
                  </Text>
                  <View className="flex-row items-center gap-2">
                    <DirectionPill
                      label={p.direction.toUpperCase()}
                      tone={p.direction === 'short' ? 'rose' : 'mint'}
                    />
                    <View
                      className={cn(
                        'rounded-full px-2 py-0.5',
                        'border border-hairline dark:border-hairline-dark',
                      )}
                    >
                      <Text className="text-[10px] uppercase tracking-[1px] text-text-secondary dark:text-text-secondary-dark">
                        {!p.managed
                          ? 'Unmanaged'
                          : p.exitMode === 'agent'
                            ? 'Agent exit'
                            : 'Manual exit'}
                      </Text>
                    </View>
                  </View>
                </View>

                <View className="flex-row justify-between">
                  <TileLabel>Entry</TileLabel>
                  <Text
                    className="text-[13px] text-text-secondary dark:text-text-secondary-dark"
                    style={{ fontVariant: ['tabular-nums'] }}
                  >
                    {money(p.avgEntryPrice)} → {money(p.lastPrice)}
                  </Text>
                </View>

                <View className="flex-row justify-between">
                  <TileLabel>Unrealized</TileLabel>
                  <Text className={cn('text-[13px] font-medium', pnlClass)} style={{ fontVariant: ['tabular-nums'] }}>
                    {money(pnl)}
                  </Text>
                </View>

                {(p.stopLoss != null || p.targetPrice != null || p.timeStopDays != null) && (
                  <Text className="text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
                    Plan: stop {money(p.stopLoss)} · target {money(p.targetPrice)}
                    {p.timeStopDays != null ? ` · time-stop ${p.timeStopDays}d` : ''}
                  </Text>
                )}

                {p.decisionId ? (
                  <Pressable
                    onPress={() => confirmClose(p.decisionId!, p.symbol, p.qty)}
                    disabled={busy}
                    accessibilityRole="button"
                    accessibilityLabel={`Close ${p.qty} ${p.symbol} now`}
                    className={cn(
                      'mt-1 min-h-[44px] items-center justify-center rounded-lg',
                      'border border-hairline dark:border-hairline-dark',
                      busy && 'opacity-50',
                    )}
                  >
                    <Text className="text-[13px] font-medium text-text-primary dark:text-text-primary-dark">
                      {busy ? 'Closing…' : 'Close now'}
                    </Text>
                  </Pressable>
                ) : (
                  <Text className="mt-1 text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
                    Held at the broker with no council decision behind it — close it
                    at the broker.
                  </Text>
                )}
              </Tile>
            );
          })
        )}
      </ScrollView>
    </SafeAreaView>
  );
}
