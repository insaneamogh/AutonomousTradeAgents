// Calibration scorecard — Design D bento.
//
// Monthly agreement bars (you vs. the Reflection agent) plus the
// override verdict: when you disagreed with the agent and the trade
// closed, who was right?

import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import { EmptyState, Skeleton, cn } from '@app/ui';

import { HeroHeadline, HeroSub, Tile, TileLabel, TileValue } from '@/components/bento';
import { useCalibrationScorecard } from '@/hooks/useCalibration';

export default function CalibrationScreen() {
  const router = useRouter();
  const { data, isLoading } = useCalibrationScorecard(180);

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView contentContainerClassName="px-4 pb-16 pt-4 gap-3">
        <Pressable
          onPress={() => router.back()}
          accessibilityRole="button"
          accessibilityLabel="Go back"
          className="min-h-[44px] justify-center"
        >
          <Text className="text-[13px] text-text-secondary dark:text-text-secondary-dark">
            ← Back
          </Text>
        </Pressable>

        <View>
          <HeroHeadline>Calibration</HeroHeadline>
          <HeroSub>You vs. the council, over the last {data?.windowDays ?? 180} days</HeroSub>
        </View>

        {isLoading ? (
          <>
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-24 w-full" />
          </>
        ) : !data || data.months.length === 0 ? (
          <Tile>
            <EmptyState
              title="No graded trades yet"
              description="Grade closed trades in the Review tab — the scorecard builds itself from there."
            />
          </Tile>
        ) : (
          <>
            <Tile className="gap-1">
              <TileLabel>Overall agreement</TileLabel>
              <TileValue size="lg" tone={data.agreementPct >= 65 ? 'mint' : 'default'}>
                {Math.round(data.agreementPct)}%
              </TileValue>
            </Tile>

            <Tile className="gap-3">
              <TileLabel>By month</TileLabel>
              {data.months.map((m) => (
                <View key={m.month} className="gap-1">
                  <View className="flex-row items-baseline justify-between">
                    <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">
                      {m.month}
                    </Text>
                    <Text
                      className="text-[12px] font-medium text-text-primary dark:text-text-primary-dark"
                      style={{ fontVariant: ['tabular-nums'] }}
                    >
                      {Math.round(m.agreementPct)}% · {m.totalReviewed} graded
                    </Text>
                  </View>
                  <View className="h-1.5 overflow-hidden rounded-full bg-bg-tile-inset dark:bg-bg-tile-inset-dark">
                    <View
                      className={cn(
                        'h-full rounded-full',
                        m.agreementPct >= 65
                          ? 'bg-mint dark:bg-mint-dark'
                          : 'bg-text-tertiary dark:bg-text-tertiary-dark',
                      )}
                      style={{ width: `${Math.min(100, Math.max(2, m.agreementPct))}%` }}
                    />
                  </View>
                </View>
              ))}
            </Tile>

            <Tile className="gap-2">
              <TileLabel>When you overrode the agent</TileLabel>
              {data.overrides.count === 0 ? (
                <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
                  No scoreable overrides yet — they need a disagreement AND a closed trade.
                </Text>
              ) : (
                <>
                  <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
                    You disagreed {data.overrides.count} time
                    {data.overrides.count === 1 ? '' : 's'} on closed trades — and were right{' '}
                    <Text
                      className={cn(
                        'font-medium',
                        data.overrides.operatorWinRatePct >= 50
                          ? 'text-mint dark:text-mint-dark'
                          : 'text-rose dark:text-rose-dark',
                      )}
                      style={{ fontVariant: ['tabular-nums'] }}
                    >
                      {Math.round(data.overrides.operatorWinRatePct)}%
                    </Text>{' '}
                    of the time.
                  </Text>
                  <View className="flex-row gap-2">
                    <Tile inset className="flex-1 gap-0.5 p-2.5">
                      <TileLabel>You won</TileLabel>
                      <TileValue tone="mint">{data.overrides.operatorWins}</TileValue>
                    </Tile>
                    <Tile inset className="flex-1 gap-0.5 p-2.5">
                      <TileLabel>Council won</TileLabel>
                      <TileValue>{data.overrides.reflectionWins}</TileValue>
                    </Tile>
                  </View>
                </>
              )}
            </Tile>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}
