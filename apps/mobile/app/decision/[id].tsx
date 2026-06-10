// Trade biography — Design D bento vertical timeline.
//
// Every closed (or vetoed, or pending) trade as a story: proposed →
// risk verdict → your decision → fills → close → grade. Pure read view
// over /api/v1/decisions/{id}/timeline.

import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';

import type { TimelineEventDto } from '@app/shared-types';
import { EmptyState, Skeleton, cn, formatRelative } from '@app/ui';

import { DirectionPill, HeroHeadline, HeroSub, Tile } from '@/components/bento';
import { useDecisionTimeline } from '@/hooks/useDecisionTimeline';

const STATUS_PILL: Record<string, { label: string; tone: 'mint' | 'rose' | 'muted' }> = {
  pending: { label: 'PENDING', tone: 'muted' },
  approved: { label: 'APPROVED', tone: 'mint' },
  declined: { label: 'PASSED', tone: 'muted' },
  expired: { label: 'EXPIRED', tone: 'muted' },
  vetoed: { label: 'VETOED', tone: 'rose' },
  closed: { label: 'CLOSED', tone: 'mint' },
};

export default function DecisionBiographyScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const { data, isLoading, isError } = useDecisionTimeline(id ?? null);

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

        {isLoading ? (
          <View className="gap-3">
            <Skeleton className="h-9 w-32" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </View>
        ) : isError || !data ? (
          <Tile>
            <EmptyState
              title="No biography available"
              description="This decision may predate the audit upgrade, or the server isn't reachable."
            />
          </Tile>
        ) : (
          <>
            <View className="flex-row items-end justify-between">
              <View>
                <HeroHeadline>{data.symbol}</HeroHeadline>
                <HeroSub>The life of this trade, from council to close</HeroSub>
              </View>
              <DirectionPill
                label={STATUS_PILL[data.status]?.label ?? data.status.toUpperCase()}
                tone={STATUS_PILL[data.status]?.tone ?? 'muted'}
              />
            </View>
            <View className="gap-0">
              {data.events.map((e, i) => (
                <EventRow key={`${e.kind}-${i}`} e={e} isLast={i === data.events.length - 1} />
              ))}
            </View>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

const KIND_TONE: Record<string, 'mint' | 'rose' | 'default'> = {
  proposed: 'default',
  risk_verdict: 'default',
  user_decision: 'default',
  order_submitted: 'default',
  filled: 'mint',
  closed: 'default',
  review_grade: 'default',
  reflection: 'default',
  ghost: 'default',
};

function EventRow({ e, isLast }: { e: TimelineEventDto; isLast: boolean }) {
  const vetoed = e.kind === 'risk_verdict' && e.data.approved === false;
  const pnl = typeof e.data.realizedPnl === 'number' ? (e.data.realizedPnl as number) : null;
  const dotTone = vetoed
    ? 'bg-rose dark:bg-rose-dark'
    : KIND_TONE[e.kind] === 'mint'
      ? 'bg-mint dark:bg-mint-dark'
      : 'bg-text-tertiary dark:bg-text-tertiary-dark';

  return (
    <View className="flex-row gap-3">
      <View className="w-4 items-center">
        <View className={cn('mt-2 h-2.5 w-2.5 rounded-full', dotTone)} />
        {!isLast && <View className="w-[1.5px] flex-1 bg-hairline dark:bg-hairline-dark" />}
      </View>
      <Tile className="mb-3 flex-1 gap-1.5">
        <View className="flex-row items-baseline justify-between gap-2">
          <Text
            className={cn(
              'flex-1 text-[14px] font-medium',
              vetoed
                ? 'text-rose dark:text-rose-dark'
                : pnl != null
                  ? pnl >= 0
                    ? 'text-mint dark:text-mint-dark'
                    : 'text-rose dark:text-rose-dark'
                  : 'text-text-primary dark:text-text-primary-dark',
            )}
            style={{ fontVariant: ['tabular-nums'] }}
          >
            {e.title}
          </Text>
          {e.at && (
            <Text className="text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
              {formatRelative(e.at)}
            </Text>
          )}
        </View>
        {e.detail ? (
          <Text
            className="text-[12px] leading-[17px] text-text-secondary dark:text-text-secondary-dark"
            numberOfLines={4}
          >
            {e.detail}
          </Text>
        ) : null}
        {e.kind === 'proposed' && Array.isArray(e.data.analysts) && (
          <View className="mt-1 gap-1">
            {(e.data.analysts as Array<Record<string, unknown>>).map((a) => (
              <View key={String(a.role)} className="flex-row items-baseline gap-2">
                <Text className="w-24 text-[10px] uppercase tracking-[0.8px] text-text-tertiary dark:text-text-tertiary-dark">
                  {String(a.role)}
                </Text>
                <Text
                  className="text-[12px] font-medium text-text-primary dark:text-text-primary-dark"
                  style={{ fontVariant: ['tabular-nums'] }}
                >
                  {typeof a.score === 'number' ? Math.round(a.score as number) : '—'}
                </Text>
                <Text
                  className="flex-1 text-[11px] text-text-secondary dark:text-text-secondary-dark"
                  numberOfLines={1}
                >
                  {String(a.thesis ?? '')}
                </Text>
              </View>
            ))}
          </View>
        )}
        {vetoed && e.data.vetoRule ? (
          <Text className="text-[11px] font-medium text-rose dark:text-rose-dark">
            Rule: {String(e.data.vetoRule)}
          </Text>
        ) : null}
      </Tile>
    </View>
  );
}
