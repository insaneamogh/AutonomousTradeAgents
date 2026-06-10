// Review — Phase 4 month-1 hand-grading.
//
// One card at a time. Swipe right = good, swipe left = bad, swipe up =
// skip. The action bar mirrors the swipes for discoverability + a11y.
//
// Cold-start UX: no completed trades in window → friendly empty state
// explaining what gets here (closed trades that haven't been graded by
// the caller yet). Day-1-of-Phase-4 reality is the operator opens this
// tab and sees the empty state until trades start closing.

import { Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import {
  Card,
  ConfidenceBar,
  EmptyState,
  ErrorState,
  PnLPill,
  SkeletonCardStack,
  SwipeDeck,
  cn,
  formatRelative,
} from '@app/ui';

import {
  Grade,
  ReviewQueueItem,
  useGradeDecision,
  useReviewQueue,
} from '@/hooks/useReview';

const WINDOW_DAYS = 30;

export default function ReviewScreen() {
  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <View className="flex-1 px-4 pb-4 pt-4 gap-4">
        <Header />
        <Body />
      </View>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Header
// ─────────────────────────────────────────────────────────────────────

function Header() {
  const { data } = useReviewQueue(WINDOW_DAYS);

  const total = data?.totalInWindow ?? 0;
  const graded = data?.gradedInWindow ?? 0;
  const remaining = total - graded;
  const pct = total > 0 ? Math.round((graded / total) * 100) : 0;

  return (
    <View className="gap-2">
      <Text className="text-[24px] font-bold text-text-primary dark:text-text-primary-dark">
        Review
      </Text>
      <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
        Hand-grade the agent's completed trades. The agreement stat on the Home strip tracks how
        often you and the Reflection Agent see eye to eye.
      </Text>
      {total > 0 ? (
        <View className="gap-1.5 pt-1">
          <View className="flex-row items-baseline justify-between">
            <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
              {graded} of {total} graded · {remaining} left
            </Text>
            <Text
              className="text-[12px] font-semibold text-text-secondary dark:text-text-secondary-dark"
              style={{ fontVariant: ['tabular-nums'] }}
            >
              {pct}%
            </Text>
          </View>
          <View className="h-1.5 overflow-hidden rounded-full bg-bg-surface-muted dark:bg-bg-surface-muted-dark">
            <View
              className="h-full rounded-full bg-accent-primary dark:bg-accent-primary-dark"
              style={{ width: `${pct}%` }}
            />
          </View>
        </View>
      ) : null}
    </View>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Body — deck OR empty/error/loading state
// ─────────────────────────────────────────────────────────────────────

function Body() {
  const { data, isLoading, isError, refetch } = useReviewQueue(WINDOW_DAYS);
  const grade = useGradeDecision(WINDOW_DAYS);

  if (isLoading) {
    return (
      <Card variant="default" className="flex-1">
        <SkeletonCardStack rows={4} />
      </Card>
    );
  }

  if (isError) {
    return (
      <Card variant="default">
        <ErrorState
          title="Couldn't load the review queue"
          description="The agent server isn't reachable. Try again in a moment."
          onRetry={() => refetch()}
        />
      </Card>
    );
  }

  const items = data?.items ?? [];

  if (items.length === 0) {
    const graded = data?.gradedInWindow ?? 0;
    if ((data?.totalInWindow ?? 0) === 0) {
      return (
        <Card variant="default" className="flex-1 items-center justify-center">
          <EmptyState
            title="Nothing to review yet"
            description={`Closed trades from the last ${WINDOW_DAYS} days show up here. Run the council + wait for fills.`}
          />
        </Card>
      );
    }
    return (
      <Card variant="default" className="flex-1 items-center justify-center">
        <EmptyState
          title="Inbox zero ✓"
          description={`You've graded all ${graded} closed trades in the last ${WINDOW_DAYS} days. Come back tomorrow.`}
        />
      </Card>
    );
  }

  const onAction = (direction: 'left' | 'right' | 'up', item: ReviewQueueItem) => {
    const g: Grade =
      direction === 'right' ? 'good' : direction === 'left' ? 'bad' : 'skip';
    grade.mutate({ decisionId: item.decisionId, grade: g });
  };

  return (
    <SwipeDeck
      items={items}
      keyFor={(i) => i.decisionId}
      renderItem={(i) => <ReviewCardBody item={i} />}
      onAction={onAction}
      actions={[
        { direction: 'left', label: 'Bad call', tone: 'loss', icon: '✗', accessibilityLabel: 'Mark as bad call' },
        { direction: 'up', label: 'Skip', tone: 'neutral', icon: '↻', accessibilityLabel: 'Skip this trade' },
        { direction: 'right', label: 'Good call', tone: 'gain', icon: '✓', accessibilityLabel: 'Mark as good call' },
      ]}
    />
  );
}

// ─────────────────────────────────────────────────────────────────────
// Card body — what each swipeable card shows
// ─────────────────────────────────────────────────────────────────────

function ReviewCardBody({ item }: { item: ReviewQueueItem }) {
  const pnl = item.realizedPnl ?? 0;
  const sideTone =
    item.side === 'BUY' ? 'gain' : item.side === 'SELL' ? 'loss' : 'neutral';

  return (
    <View className="flex-1 gap-3.5">
      {/* Header: symbol + side + relative time */}
      <View className="flex-row items-center justify-between">
        <View className="flex-row items-baseline gap-2">
          <Text
            className="text-[22px] font-bold text-text-primary dark:text-text-primary-dark"
            style={{ fontVariant: ['tabular-nums'] }}
          >
            {item.symbol}
          </Text>
          <Text
            className={cn(
              'text-[14px] font-semibold',
              sideTone === 'gain' && 'text-gain dark:text-gain-dark',
              sideTone === 'loss' && 'text-loss dark:text-loss-dark',
              sideTone === 'neutral' &&
                'text-text-tertiary dark:text-text-tertiary-dark',
            )}
          >
            {item.side}
          </Text>
        </View>
        <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
          {formatRelative(item.triggeredAt)}
        </Text>
      </View>

      {/* Realized P&L pill */}
      <View className="flex-row items-center justify-between">
        <PnLPill value={pnl} size="md" />
        {item.fillQty != null && item.fillAvgPrice != null ? (
          <Text
            className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark"
            style={{ fontVariant: ['tabular-nums'] }}
          >
            {item.fillQty} @ ${item.fillAvgPrice.toFixed(2)}
          </Text>
        ) : null}
      </View>

      {/* Strategy + confidence */}
      {item.selectedStrategy ? (
        <View className="gap-1.5 pt-1">
          <View className="flex-row items-baseline justify-between">
            <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
              Strategy · {item.selectedStrategy}
            </Text>
            {item.regime ? (
              <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
                regime: {item.regime}
              </Text>
            ) : null}
          </View>
          <ConfidenceBar value={item.selectorConfidence} />
        </View>
      ) : null}

      {/* Bull / bear narrative — the operator's main signal for grading */}
      <View className="flex-1 gap-3 pt-1">
        {item.bullCase ? (
          <CaseBlock label="Bull case" tone="gain" text={item.bullCase} />
        ) : null}
        {item.bearCase ? (
          <CaseBlock label="Bear case" tone="loss" text={item.bearCase} />
        ) : null}
      </View>
    </View>
  );
}

function CaseBlock({
  label,
  tone,
  text,
}: {
  label: string;
  tone: 'gain' | 'loss';
  text: string;
}) {
  return (
    <View className="gap-1">
      <Text
        className={cn(
          'text-[10px] font-bold uppercase tracking-[1.2px]',
          tone === 'gain' && 'text-gain dark:text-gain-dark',
          tone === 'loss' && 'text-loss dark:text-loss-dark',
        )}
      >
        {label}
      </Text>
      <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
        {text}
      </Text>
    </View>
  );
}
