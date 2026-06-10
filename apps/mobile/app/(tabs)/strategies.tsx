// Strategies — per-strategy performance card list.
//
// Each row shows the current confidence (post-Reflection nudges), the
// per-strategy 30-day stats, and a small status chip indicating how
// active the strategy has been.
//
// Cold-start UX (Phase 4 day 1, fresh DB): every row sits at confidence
// 0.5 with 0 decisions. The card surfaces this with a muted chip + a
// short hint so the operator knows the screen is alive, just empty.

import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import {
  Card,
  ConfidenceBar,
  ErrorState,
  PriceDisplay,
  Skeleton,
  StatusPill,
  cn,
  formatRelative,
} from '@app/ui';

import {
  StrategyPerformance,
  useStrategiesPerformance,
} from '@/hooks/useStrategiesPerformance';

export default function StrategiesScreen() {
  const { data, isLoading, isError, refetch } = useStrategiesPerformance(30);

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-4">
        <Header windowDays={data?.windowDays} />

        {isLoading ? (
          <>
            <StrategyCardSkeleton />
            <StrategyCardSkeleton />
            <StrategyCardSkeleton />
          </>
        ) : isError ? (
          <Card variant="default">
            <ErrorState
              title="Couldn't load strategies"
              description="The agent server isn't reachable. Try again in a moment."
              onRetry={() => refetch()}
            />
          </Card>
        ) : (
          (data?.strategies ?? []).map((s) => <StrategyCard key={s.strategyId} s={s} />)
        )}

        <Footnote />
      </ScrollView>
    </SafeAreaView>
  );
}

function Header({ windowDays }: { windowDays?: number }) {
  return (
    <View className="gap-1">
      <Text className="text-[24px] font-bold text-text-primary dark:text-text-primary-dark">
        Strategies
      </Text>
      <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
        Per-strategy track record over the last {windowDays ?? 30} days. Confidence shifts as the
        Reflection Agent grades completed trades.
      </Text>
    </View>
  );
}

function StrategyCard({ s }: { s: StrategyPerformance }) {
  const completed = s.wins + s.losses;
  const winRate = completed > 0 ? (s.wins / completed) * 100 : null;
  const positive = s.realizedPnl >= 0;

  // Status chip based on activity in window.
  const chipTone =
    s.decisionsInWindow === 0
      ? 'muted'
      : completed === 0
        ? 'warning'
        : winRate !== null && winRate >= 50
          ? 'ok'
          : 'warning';
  const chipLabel =
    s.decisionsInWindow === 0
      ? 'NO DECISIONS YET'
      : completed === 0
        ? `${s.decisionsInWindow} OPEN`
        : `${s.wins}W ${s.losses}L`;

  return (
    <Card variant="default" className="gap-4">
      {/* Top row: display name + status chip */}
      <View className="flex-row items-center justify-between gap-3">
        <Text
          className="flex-1 text-[16px] font-semibold text-text-primary dark:text-text-primary-dark"
          numberOfLines={1}
        >
          {s.displayName}
        </Text>
        <StatusPill tone={chipTone} label={chipLabel} layout="chip" />
      </View>

      {/* Confidence bar */}
      <ConfidenceBar value={s.confidence} />

      {/* Stats row */}
      <View className="flex-row gap-3">
        <Stat
          label="Realized P&L"
          render={
            <PriceDisplay
              value={s.realizedPnl}
              size="md"
              signed
              tone={positive ? 'gain' : 'loss'}
            />
          }
        />
        <Stat
          label="Win rate"
          render={
            <Text
              className={cn(
                'text-[18px] font-semibold',
                winRate === null
                  ? 'text-text-tertiary dark:text-text-tertiary-dark'
                  : winRate >= 50
                    ? 'text-gain dark:text-gain-dark'
                    : 'text-loss dark:text-loss-dark',
              )}
              style={{ fontVariant: ['tabular-nums'] }}
            >
              {winRate === null ? '—' : `${winRate.toFixed(0)}%`}
            </Text>
          }
        />
        <Stat
          label="Decisions"
          render={
            <Text
              className="text-[18px] font-semibold text-text-primary dark:text-text-primary-dark"
              style={{ fontVariant: ['tabular-nums'] }}
            >
              {s.decisionsInWindow}
            </Text>
          }
        />
      </View>

      {/* Averages — only when there's something to show */}
      {s.avgWinnerPct !== null || s.avgLoserPct !== null ? (
        <View className="flex-row gap-4">
          {s.avgWinnerPct !== null ? (
            <PctRow label="Avg winner" value={s.avgWinnerPct} positive />
          ) : null}
          {s.avgLoserPct !== null ? (
            <PctRow label="Avg loser" value={s.avgLoserPct} positive={false} />
          ) : null}
        </View>
      ) : null}

      {/* Last events */}
      <View className="flex-row gap-4 pt-1">
        <FootRow
          label="Last decision"
          value={
            s.lastDecisionAt ? formatRelative(s.lastDecisionAt) : 'never'
          }
        />
        <FootRow
          label="Last reflection"
          value={
            s.lastReflectionAt ? formatRelative(s.lastReflectionAt) : 'never'
          }
        />
      </View>
    </Card>
  );
}

function Stat({ label, render }: { label: string; render: React.ReactNode }) {
  return (
    <View className="flex-1 gap-1">
      <Text className="text-[10px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
        {label}
      </Text>
      {render}
    </View>
  );
}

function PctRow({ label, value, positive }: { label: string; value: number; positive: boolean }) {
  return (
    <View className="flex-1 flex-row items-center justify-between rounded-md bg-bg-surface-muted px-3 py-2 dark:bg-bg-surface-muted-dark">
      <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">
        {label}
      </Text>
      <Text
        className={cn(
          'text-[13px] font-semibold',
          positive ? 'text-gain dark:text-gain-dark' : 'text-loss dark:text-loss-dark',
        )}
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {positive && value >= 0 ? '+' : ''}
        {value.toFixed(2)}%
      </Text>
    </View>
  );
}

function FootRow({ label, value }: { label: string; value: string }) {
  return (
    <View className="flex-1 gap-0.5">
      <Text className="text-[10px] font-semibold uppercase tracking-[1.1px] text-text-tertiary dark:text-text-tertiary-dark">
        {label}
      </Text>
      <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">
        {value}
      </Text>
    </View>
  );
}

function StrategyCardSkeleton() {
  return (
    <Card variant="default" className="gap-4">
      <View className="flex-row items-center justify-between">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-4 w-16" />
      </View>
      <Skeleton className="h-2 w-full" />
      <View className="flex-row gap-3">
        <Skeleton className="h-10 flex-1" />
        <Skeleton className="h-10 flex-1" />
        <Skeleton className="h-10 flex-1" />
      </View>
    </Card>
  );
}

function Footnote() {
  return (
    <Text className="mt-2 text-center text-[11px] leading-[15px] text-text-tertiary dark:text-text-tertiary-dark">
      Confidence drifts ±0.10 per Reflection cycle.{'\n'}
      Cold-start prior is 0.50 (the hairline marker).
    </Text>
  );
}
