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
import type { ClosePositionResponse } from '@app/shared-types';

import { DirectionPill, HeroHeadline, HeroSub, Tile, TileLabel } from '@/components/bento';
import { ClosePositionButton } from '@/components/ClosePositionButton';
import { useCloseUnmanagedPosition, useClosePosition, useOpenPositions } from '@/hooks/usePositions';
import { DEMO_DISABLED_REASON, useIsDemoSession } from '@/lib/demoSession';

function money(n: number | null): string {
  if (n == null) return '—';
  const sign = n < 0 ? '-' : '';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

/** 409 error codes from /positions/{id}/close, in plain English. A close
 * and a cancel share this one endpoint (the server decides which based
 * on whether the entry filled), so this covers both outcomes. */
const CLOSE_ERROR_COPY: Record<string, string> = {
  not_found: "That position or order isn't there any more.",
  not_owner: "That position or order isn't there any more.",
  already_closed: 'Already closed.',
  no_open_position: 'This position was already closed.',
  no_pending_order: 'The order already filled or was already cancelled.',
  close_in_flight: 'A close is already in progress for this position.',
  risk_vetoed: 'A risk rule blocked the close — try again shortly.',
};

function closeErrorDetail(err: unknown): string {
  const code =
    err instanceof ApiError && typeof (err.body as { detail?: string })?.detail === 'string'
      ? (err.body as { detail: string }).detail
      : null;
  if (code && CLOSE_ERROR_COPY[code]) return CLOSE_ERROR_COPY[code];
  if (code) return code;
  return "Couldn't reach the agent server.";
}

export default function PositionsScreen() {
  const router = useRouter();
  const { data, isLoading, isError, refetch } = useOpenPositions();
  const close = useClosePosition();
  const closeUnmanaged = useCloseUnmanagedPosition();
  const isDemo = useIsDemoSession();
  const list = data ?? [];

  // Two close mutations, one confirm flow. `target` picks which one fires:
  // a decisionId-keyed row uses the decision route (also doubles as
  // "cancel" for a not-yet-filled order); a decisionId-less (unmanaged) row
  // has no decision to close "through", so it uses the symbol-keyed route.
  const confirmClose = (
    target: { kind: 'decision'; decisionId: string } | { kind: 'unmanaged'; symbol: string },
    symbol: string,
    qty: number,
    pending: boolean,
  ) => {
    Alert.alert(
      pending ? `Cancel the ${symbol} order?` : `Close ${qty} ${symbol}?`,
      pending
        ? "This cancels the order at the broker before it fills. Nothing was ever bought or sold."
        : target.kind === 'unmanaged'
          ? 'This position has no council decision behind it. Closing places a market sell now, through the same risk checks as any other close. Resting stop/target orders are cancelled first.'
          : 'This places a market sell now, through the same risk checks the agent uses. Resting stop/target orders are cancelled first.',
      [
        { text: pending ? 'Keep order' : 'Cancel', style: 'cancel' },
        {
          text: pending ? 'Cancel order' : 'Close position',
          style: 'destructive',
          onPress: () => {
            Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});
            const onSuccess = (res: ClosePositionResponse) => {
              if (!res.closed) {
                Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(
                  () => {},
                );
                Alert.alert(
                  pending ? 'Not cancelled' : 'Not closed',
                  res.detail ?? CLOSE_ERROR_COPY[res.error ?? ''] ?? 'Try again shortly.',
                );
              }
            };
            const onError = (err: unknown) => {
              Alert.alert(pending ? 'Cancel failed' : 'Close failed', closeErrorDetail(err));
            };
            if (target.kind === 'decision') {
              close.mutate(target.decisionId, { onSuccess, onError });
            } else {
              closeUnmanaged.mutate(target.symbol, { onSuccess, onError });
            }
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
            {isDemo
              ? `${DEMO_DISABLED_REASON} — closing and cancelling are turned off.`
              : list.length > 0
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
            const busy =
              (close.isPending && close.variables === p.decisionId) ||
              (closeUnmanaged.isPending && closeUnmanaged.variables === p.symbol);
            // Options are always `side: BUY` (Phase A never sells to open),
            // so the header reads the contract instead of a side that
            // can't tell a call from a put apart.
            const isOption = p.isOption === true;
            return (
              <Tile key={p.decisionId ?? `broker:${p.symbol}`} className="gap-2">
                <View className="flex-row items-center justify-between">
                  <Text
                    className="text-[16px] font-semibold text-text-primary dark:text-text-primary-dark"
                    style={{ fontVariant: ['tabular-nums'] }}
                  >
                    {isOption
                      ? `${p.symbol} $${p.strike?.toFixed(2) ?? '—'} ${(p.contractType ?? '').toUpperCase()} x${p.qty}`
                      : `${p.side} ${p.qty} ${p.symbol}`}
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
                        {p.status === 'pending_fill'
                          ? 'Awaiting fill'
                          : !p.managed
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
                    {p.status === 'pending_fill' ? 'order working — not filled yet' : `${money(p.avgEntryPrice)} → ${money(p.lastPrice)}`}
                  </Text>
                </View>

                {p.status !== 'pending_fill' && (
                  <View className="flex-row justify-between">
                    <TileLabel>Unrealized</TileLabel>
                    <Text className={cn('text-[13px] font-medium', pnlClass)} style={{ fontVariant: ['tabular-nums'] }}>
                      {money(pnl)}
                    </Text>
                  </View>
                )}

                {isOption ? (
                  <Text className="text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
                    {p.expiryDate ? `Expires ${p.expiryDate}` : 'Expiry unknown'} · no bracket on
                    options — expiry sweep + time-stop own the close
                    {p.timeStopDays != null ? ` · time-stop ${p.timeStopDays}d` : ''}
                  </Text>
                ) : (
                  (p.stopLoss != null || p.targetPrice != null || p.timeStopDays != null) && (
                    <Text className="text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
                      Plan: stop {money(p.stopLoss)} · target {money(p.targetPrice)}
                      {p.timeStopDays != null ? ` · time-stop ${p.timeStopDays}d` : ''}
                    </Text>
                  )
                )}

                {!p.managed && (
                  <Text className="text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
                    Held at the broker with no council decision behind it.
                  </Text>
                )}

                {p.decisionId ? (
                  <ClosePositionButton
                    symbol={p.symbol}
                    qty={p.qty}
                    pending={p.status === 'pending_fill'}
                    busy={busy}
                    disabledForDemo={isDemo}
                    onPress={() =>
                      confirmClose(
                        { kind: 'decision', decisionId: p.decisionId! },
                        p.symbol,
                        p.qty,
                        p.status === 'pending_fill',
                      )
                    }
                  />
                ) : (
                  <ClosePositionButton
                    symbol={p.symbol}
                    qty={p.qty}
                    pending={false}
                    busy={busy}
                    disabledForDemo={isDemo}
                    onPress={() =>
                      confirmClose({ kind: 'unmanaged', symbol: p.symbol }, p.symbol, p.qty, false)
                    }
                  />
                )}
              </Tile>
            );
          })
        )}
      </ScrollView>
    </SafeAreaView>
  );
}
