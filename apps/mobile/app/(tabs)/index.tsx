// Home / Dashboard — wired to the API via TanStack Query.
//
// Three sections per DESIGN.md §11:
//   1. Hero — account equity + today's P&L
//   2. Account stats — cash / buying power / open positions
//   3. Recent agent activity feed

import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import type { ActivityEntryDto } from '@app/shared-types';
import {
  Card,
  ErrorState,
  PnLBadge,
  PriceDisplay,
  Skeleton,
  SkeletonCardStack,
  StatusPill,
  cn,
  formatRelative,
} from '@app/ui';

import { useAccount } from '@/hooks/useAccount';
import { useActivity } from '@/hooks/useActivity';
import {
  ComponentHealth,
  ComponentStatus,
  useHealthFull,
} from '@/hooks/useHealthFull';
import { useReviewAgreement } from '@/hooks/useReview';

export default function HomeScreen() {
  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-4">
        <HealthStrip />
        <AgreementStrip />
        <Hero />
        <AccountStatsRow />
        <ActivityFeed />
      </ScrollView>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Health strip
//
// Compact system-status row at the top of Home. Reads /api/v1/health/full.
// Renders four pills (Council · Approvals · Broker · Reconciler) — each a
// dot + label + one-line hint. The strip self-hides if the health endpoint
// is unreachable; we don't want a "couldn't load health" banner cluttering
// the dashboard.
// ─────────────────────────────────────────────────────────────────────

function HealthStrip() {
  const { data, isLoading, isError } = useHealthFull();

  if (isLoading) {
    return (
      <Card variant="inset" className="flex-row gap-3 py-2.5">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-5 flex-1" />
        ))}
      </Card>
    );
  }
  if (isError || !data) {
    return null;
  }

  return (
    <Card variant="inset" className="gap-3 py-2.5">
      <View className="flex-row flex-wrap gap-y-3">
        <View className="w-1/2 pr-2">
          <HealthCell label="COUNCIL" h={data.council} />
        </View>
        <View className="w-1/2 pl-2">
          <HealthCell label="APPROVALS" h={data.approvals} />
        </View>
        <View className="w-1/2 pr-2">
          <HealthCell label="BROKER" h={data.broker} />
        </View>
        <View className="w-1/2 pl-2">
          <HealthCell label="RECONCILER" h={data.reconciler} />
        </View>
        <View className="w-full pt-1">
          <HealthCell label="LLM COST" h={data.llmCost} />
        </View>
      </View>
    </Card>
  );
}

function HealthCell({ label, h }: { label: string; h: ComponentHealth }) {
  return (
    <StatusPill
      tone={_toneFromStatus(h.status)}
      label={label}
      hint={h.label}
      layout="dot"
    />
  );
}

function _toneFromStatus(s: ComponentStatus): 'ok' | 'warning' | 'danger' | 'muted' {
  switch (s) {
    case 'ok':
      return 'ok';
    case 'warning':
      return 'warning';
    case 'danger':
      return 'danger';
    default:
      return 'muted';
  }
}

// ─────────────────────────────────────────────────────────────────────
// Agreement strip — Phase 4 calibration signal
//
// Shows: "Agreement X% over N reviews (30d)". The number tracks how
// often the operator's grade direction matches the Reflection Agent's
// nudge direction. Hides itself if there's nothing reviewed yet — no
// reason to show "Agreement 0% over 0 reviews" on day 1.
// ─────────────────────────────────────────────────────────────────────

function AgreementStrip() {
  const { data, isLoading } = useReviewAgreement(30);

  if (isLoading) return null;
  if (!data || data.totalReviewed === 0) return null;

  const tone: 'ok' | 'warning' | 'danger' =
    data.agreementPct >= 65 ? 'ok' : data.agreementPct >= 45 ? 'warning' : 'danger';
  const toneClasses: Record<typeof tone, string> = {
    ok: 'bg-gain dark:bg-gain-dark',
    warning: 'bg-warning dark:bg-warning-dark',
    danger: 'bg-loss dark:bg-loss-dark',
  };

  return (
    <Card variant="inset" className="flex-row items-center justify-between py-3">
      <View className="flex-1 gap-1 pr-3">
        <Text className="text-[10px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
          Calibration · 30d
        </Text>
        <Text className="text-[13px] leading-[17px] text-text-secondary dark:text-text-secondary-dark">
          Your grades match Reflection {Math.round(data.agreementPct)}% of the time over{' '}
          {data.totalReviewed} review{data.totalReviewed === 1 ? '' : 's'}.
        </Text>
      </View>
      <View className="items-center">
        <Text
          className={cn(
            'text-[24px] font-bold',
            tone === 'ok' && 'text-gain dark:text-gain-dark',
            tone === 'warning' && 'text-warning dark:text-warning-dark',
            tone === 'danger' && 'text-loss dark:text-loss-dark',
          )}
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {Math.round(data.agreementPct)}%
        </Text>
        <View className={cn('mt-1 h-1 w-8 rounded-full', toneClasses[tone])} />
      </View>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Hero — equity + today's P&L
// ─────────────────────────────────────────────────────────────────────

function Hero() {
  const { data, isLoading, isError, refetch } = useAccount();

  if (isLoading) {
    return (
      <Card variant="default">
        <SkeletonCardStack rows={2} />
      </Card>
    );
  }
  if (isError || !data) {
    return (
      <Card variant="default">
        <ErrorState
          title="Account unavailable"
          description="The agent server isn't reachable. Make sure the API is running."
          onRetry={() => refetch()}
        />
      </Card>
    );
  }

  const positive = data.todayPnl >= 0;
  return (
    <Card variant="default" className="gap-3">
      <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
        Equity · {data.isPaper ? 'paper' : 'live'}
      </Text>
      <PriceDisplay value={data.equity} size="lg" />
      <View className="flex-row items-center gap-3">
        <PriceDisplay
          value={data.todayPnl}
          size="md"
          signed
          tone={positive ? 'gain' : 'loss'}
        />
        <PnLBadge pct={data.todayPnlPct} />
        <Text className="text-[13px] text-text-tertiary dark:text-text-tertiary-dark">
          today
        </Text>
      </View>
      <View className="mt-1 flex-row items-center gap-2">
        <View
          className={cn(
            'h-2 w-2 rounded-full',
            data.status === 'connected'
              ? 'bg-gain dark:bg-gain-dark'
              : data.status === 'expiring'
                ? 'bg-warning dark:bg-warning-dark'
                : 'bg-loss dark:bg-loss-dark',
          )}
        />
        <Text className="text-[11px] font-medium uppercase tracking-[1px] text-text-secondary dark:text-text-secondary-dark">
          {data.brokerName} {data.isPaper ? 'paper' : 'live'} · {data.status}
        </Text>
      </View>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Stats row
// ─────────────────────────────────────────────────────────────────────

function AccountStatsRow() {
  const { data, isLoading } = useAccount();

  if (isLoading || !data) {
    return (
      <View className="flex-row gap-3">
        {[0, 1, 2].map((i) => (
          <Card key={i} variant="inset" className="flex-1 gap-2">
            <Skeleton className="h-3 w-16" />
            <Skeleton className="h-5 w-20" />
          </Card>
        ))}
      </View>
    );
  }

  return (
    <View className="flex-row gap-3">
      <Stat label="Cash" value={data.cash} />
      <Stat label="Buying power" value={data.buyingPower} />
      <Stat label="Open" value={data.openPositions} isCount />
    </View>
  );
}

function Stat({ label, value, isCount = false }: { label: string; value: number; isCount?: boolean }) {
  return (
    <Card variant="inset" className="flex-1 gap-1">
      <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
        {label}
      </Text>
      {isCount ? (
        <Text
          className="text-[20px] font-semibold text-text-primary dark:text-text-primary-dark"
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {value}
        </Text>
      ) : (
        <PriceDisplay value={value} size="md" fractionDigits={0} />
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Activity feed
// ─────────────────────────────────────────────────────────────────────

function ActivityFeed() {
  const { data, isLoading, isError, refetch } = useActivity();

  return (
    <View className="gap-2">
      <Text className="mt-2 text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
        Agent activity
      </Text>
      {isLoading ? (
        Array.from({ length: 3 }).map((_, i) => (
          <Card key={i} variant="default">
            <SkeletonCardStack rows={2} />
          </Card>
        ))
      ) : isError || !data ? (
        <Card variant="default">
          <ErrorState
            title="Couldn't load activity"
            description="The agent feed isn't reachable. Try again in a moment."
            onRetry={() => refetch()}
          />
        </Card>
      ) : (
        data.map((entry) => <ActivityRow key={entry.id} entry={entry} />)
      )}
    </View>
  );
}

const KIND_LABEL: Record<ActivityEntryDto['kind'], string> = {
  proposal: 'PROPOSED',
  approved: 'APPROVED',
  declined: 'DECLINED',
  filled: 'FILLED',
  vetoed: 'VETOED',
};

const KIND_CLASSES: Record<ActivityEntryDto['kind'], string> = {
  proposal: 'text-info dark:text-info-dark',
  approved: 'text-accent-primary dark:text-accent-primary-dark',
  declined: 'text-text-tertiary dark:text-text-tertiary-dark',
  filled: 'text-gain dark:text-gain-dark',
  vetoed: 'text-warning dark:text-warning-dark',
};

function ActivityRow({ entry }: { entry: ActivityEntryDto }) {
  return (
    <Card variant="default" className="gap-2">
      <View className="flex-row items-center justify-between">
        <View className="flex-row items-center gap-2">
          <Text
            className={cn(
              'text-[11px] font-bold uppercase tracking-[1.2px]',
              KIND_CLASSES[entry.kind],
            )}
          >
            {KIND_LABEL[entry.kind]}
          </Text>
          <Text
            className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark"
            style={{ fontVariant: ['tabular-nums'] }}
          >
            {entry.symbol}
          </Text>
          <Text
            className={cn(
              'text-[13px] font-semibold',
              entry.side === 'BUY'
                ? 'text-gain dark:text-gain-dark'
                : 'text-loss dark:text-loss-dark',
            )}
          >
            {entry.side}
          </Text>
        </View>
        <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
          {formatRelative(entry.timestamp)}
        </Text>
      </View>
      <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
        {entry.headline}
      </Text>
    </Card>
  );
}
