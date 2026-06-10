// Strategies — Design D bento.
//
// Each strategy = one tile: name + activity chip, confidence bar,
// 3-up mini stat tiles (win rate / realized P&L / decisions).
// Dashed "Backtest new strategy" placeholder points at the Phase 1
// backtester — intentionally inert until that ships.

import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ConfidenceBar, ErrorState, Skeleton, formatRelative } from '@app/ui';

import {
  DirectionPill,
  HeroHeadline,
  HeroSub,
  Tile,
  TileLabel,
  TileValue,
} from '@/components/bento';
import {
  StrategyPerformance,
  useStrategiesPerformance,
} from '@/hooks/useStrategiesPerformance';

export default function StrategiesScreen() {
  const { data, isLoading, isError, refetch } = useStrategiesPerformance(30);

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-3">
        <View>
          <HeroHeadline>Strategies</HeroHeadline>
          <HeroSub>
            {data?.windowDays ?? 30}-day track record. Confidence shifts as Reflection grades
            closed trades.
          </HeroSub>
        </View>

        {isLoading ? (
          <>
            <TileSkeleton />
            <TileSkeleton />
          </>
        ) : isError ? (
          <Tile>
            <ErrorState
              title="Couldn't load strategies"
              description="The agent server isn't reachable. Try again in a moment."
              onRetry={() => refetch()}
            />
          </Tile>
        ) : (
          (data?.strategies ?? []).map((s) => <StrategyTile key={s.strategyId} s={s} />)
        )}

        <View className="items-center justify-center rounded-lg border border-dashed border-hairline px-4 py-5 dark:border-hairline-dark">
          <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
            + Backtest new strategy — ships with the Phase 1 backtester
          </Text>
        </View>

        <Text className="mt-1 text-center text-[10px] leading-[14px] text-text-tertiary dark:text-text-tertiary-dark">
          Confidence drifts ±0.10 per Reflection cycle. Cold-start prior is 0.50.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

function StrategyTile({ s }: { s: StrategyPerformance }) {
  const completed = s.wins + s.losses;
  const winRate = completed > 0 ? (s.wins / completed) * 100 : null;
  const pnlPositive = s.realizedPnl >= 0;

  const chip =
    s.decisionsInWindow === 0
      ? { label: 'IDLE', tone: 'muted' as const }
      : completed === 0
        ? { label: `${s.decisionsInWindow} OPEN`, tone: 'muted' as const }
        : winRate !== null && winRate >= 50
          ? { label: 'ACTIVE', tone: 'mint' as const }
          : { label: 'ACTIVE', tone: 'rose' as const };

  return (
    <Tile className="gap-3">
      <View className="flex-row items-center justify-between gap-3">
        <Text
          className="flex-1 text-[15px] font-medium text-text-primary dark:text-text-primary-dark"
          numberOfLines={1}
        >
          {s.displayName}
        </Text>
        <DirectionPill label={chip.label} tone={chip.tone} />
      </View>

      <ConfidenceBar value={s.confidence} />

      <View className="flex-row gap-2">
        <Tile inset className="flex-1 gap-0.5 p-2.5">
          <TileLabel>Win rate</TileLabel>
          <TileValue tone={winRate === null ? 'default' : winRate >= 50 ? 'mint' : 'rose'}>
            {winRate === null ? '—' : `${winRate.toFixed(0)}%`}
          </TileValue>
        </Tile>
        <Tile inset className="flex-1 gap-0.5 p-2.5">
          <TileLabel>Realized</TileLabel>
          <TileValue tone={pnlPositive ? 'mint' : 'rose'}>
            {pnlPositive ? '+' : '−'}$
            {Math.abs(s.realizedPnl).toLocaleString('en-US', { maximumFractionDigits: 0 })}
          </TileValue>
        </Tile>
        <Tile inset className="flex-1 gap-0.5 p-2.5">
          <TileLabel>Decisions</TileLabel>
          <TileValue>{s.decisionsInWindow}</TileValue>
        </Tile>
      </View>

      <View className="flex-row gap-4">
        <Foot
          label="Last decision"
          value={s.lastDecisionAt ? formatRelative(s.lastDecisionAt) : 'never'}
        />
        <Foot
          label="Last reflection"
          value={s.lastReflectionAt ? formatRelative(s.lastReflectionAt) : 'never'}
        />
        {s.avgWinnerPct !== null && (
          <Foot label="Avg winner" value={`+${s.avgWinnerPct.toFixed(1)}%`} />
        )}
      </View>
    </Tile>
  );
}

function Foot({ label, value }: { label: string; value: string }) {
  return (
    <View className="gap-0.5">
      <Text className="text-[9px] font-semibold uppercase tracking-[1px] text-text-tertiary dark:text-text-tertiary-dark">
        {label}
      </Text>
      <Text className="text-[11px] text-text-secondary dark:text-text-secondary-dark">
        {value}
      </Text>
    </View>
  );
}

function TileSkeleton() {
  return (
    <Tile className="gap-3">
      <View className="flex-row items-center justify-between">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-4 w-14" />
      </View>
      <Skeleton className="h-2 w-full" />
      <View className="flex-row gap-2">
        <Skeleton className="h-12 flex-1" />
        <Skeleton className="h-12 flex-1" />
        <Skeleton className="h-12 flex-1" />
      </View>
    </Tile>
  );
}
