// Equity today. Mock data must never look real: with no active broker
// connection the tile says so and points to Settings instead of a number.

import { Text, View } from 'react-native';
import { useRouter } from 'expo-router';

import { ErrorState, HapticPressable, Skeleton, cn } from '@app/ui';

import { Tile, TileLabel } from '@/components/bento';
import { useAccount } from '@/hooks/useAccount';
import { useBrokerConnections } from '@/hooks/useBrokerConnections';

import { formatMoney } from './format';

export function EquityTile() {
  const router = useRouter();
  const conns = useBrokerConnections();
  const account = useAccount();

  const noBroker = conns.data != null && !conns.data.some((c) => c.status === 'active');
  if (noBroker) {
    return (
      <HapticPressable
        haptic="light"
        onPress={() => router.push('/settings')}
        accessibilityRole="button"
        accessibilityLabel="Connect a broker in Settings"
        testID="equity-no-broker"
      >
        <Tile className="gap-1.5">
          <TileLabel>Equity today</TileLabel>
          <Text className="text-[28px] font-medium leading-[32px] text-text-primary dark:text-text-primary-dark">
            No broker connected
          </Text>
          <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">
            Connect one in Settings to see your real equity. Nothing here is a live number until you do.
          </Text>
          <Text className="text-[12px] font-medium text-text-primary dark:text-text-primary-dark">
            Open Settings →
          </Text>
        </Tile>
      </HapticPressable>
    );
  }

  if (account.isLoading || conns.isLoading) {
    return (
      <Tile className="gap-2">
        <TileLabel>Equity today</TileLabel>
        <Skeleton className="h-12 w-52" />
        <Skeleton className="h-4 w-28" />
      </Tile>
    );
  }
  if (account.isError || !account.data) {
    return (
      <Tile>
        <ErrorState
          title="Account unavailable"
          description="The agent server isn't reachable. Make sure the API is running."
          onRetry={() => account.refetch()}
        />
      </Tile>
    );
  }

  const a = account.data;
  const cur = a.currency ?? 'USD';
  const positive = a.todayPnl >= 0;
  const sign = positive ? '+' : '−';
  return (
    <Tile className="gap-1">
      <TileLabel>
        Equity today · {a.brokerName} {a.isPaper ? 'paper' : 'live'}
      </TileLabel>
      <Text
        className="text-[48px] font-medium leading-[52px] text-text-primary dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'], letterSpacing: -1.2 }}
        testID="equity-value"
      >
        {formatMoney(a.equity, cur)}
      </Text>
      <View>
        <Text
          className={cn(
            'text-[14px] font-medium',
            positive ? 'text-mint dark:text-mint-dark' : 'text-rose dark:text-rose-dark',
          )}
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {sign}
          {formatMoney(Math.abs(a.todayPnl), cur)} · {sign}
          {Math.abs(a.todayPnlPct).toFixed(1)}%
        </Text>
      </View>
    </Tile>
  );
}
