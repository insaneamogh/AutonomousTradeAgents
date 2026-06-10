// Home — Design D "editorial bento".
//
// Hero portfolio numeral · stat tiles (open positions / pending picks)
// · agent-activity tile · ONE platinum CTA ("Review N picks").
// Health + calibration strips stay — they collapse to nothing when
// there's no signal.

import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import type { ActivityEntryDto } from '@app/shared-types';
import { ErrorState, Skeleton, StatusPill, cn, formatRelative } from '@app/ui';

import { BentoCTA, Tile, TileLabel, TileValue } from '@/components/bento';
import { useAccount } from '@/hooks/useAccount';
import { useActivity } from '@/hooks/useActivity';
import { usePendingApprovals } from '@/hooks/useApprovals';
import {
  ComponentHealth,
  ComponentStatus,
  useHealthFull,
} from '@/hooks/useHealthFull';
import { useReviewAgreement } from '@/hooks/useReview';

export default function HomeScreen() {
  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-3">
        <Hero />
        <StatTiles />
        <ActivityTile />
        <ReviewCTA />
        <AgreementTile />
        <HealthTile />
      </ScrollView>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Hero — portfolio numeral straight on the canvas, no card chrome.
// ─────────────────────────────────────────────────────────────────────

function Hero() {
  const { data, isLoading, isError, refetch } = useAccount();

  if (isLoading) {
    return (
      <View className="gap-2">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-9 w-44" />
        <Skeleton className="h-4 w-28" />
      </View>
    );
  }
  if (isError || !data) {
    return (
      <Tile>
        <ErrorState
          title="Account unavailable"
          description="The agent server isn't reachable. Make sure the API is running."
          onRetry={() => refetch()}
        />
      </Tile>
    );
  }

  const positive = data.todayPnl >= 0;
  const sign = positive ? '+' : '−';
  const pnlAbs = Math.abs(data.todayPnl);
  const pctAbs = Math.abs(data.todayPnlPct);

  return (
    <View>
      <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
        Portfolio · {data.brokerName} {data.isPaper ? 'paper' : 'live'}
      </Text>
      <Text
        className="mt-1 text-[34px] font-medium leading-[38px] text-text-primary dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'], letterSpacing: -0.8 }}
      >
        ${data.equity.toLocaleString('en-US', { maximumFractionDigits: 0 })}
      </Text>
      <Text
        className={cn(
          'mt-0.5 text-[14px] font-medium',
          positive ? 'text-mint dark:text-mint-dark' : 'text-rose dark:text-rose-dark',
        )}
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {sign}${pnlAbs.toLocaleString('en-US', { maximumFractionDigits: 0 })} · {sign}
        {pctAbs.toFixed(1)}% today
      </Text>
    </View>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Stat tiles — 2-up bento row.
// ─────────────────────────────────────────────────────────────────────

function StatTiles() {
  const { data: account } = useAccount();
  const { data: pending } = usePendingApprovals();

  return (
    <View className="flex-row gap-3">
      <Tile className="flex-1 gap-1">
        <TileLabel>Open positions</TileLabel>
        <TileValue>{account ? account.openPositions : '—'}</TileValue>
      </Tile>
      <Tile className="flex-1 gap-1">
        <TileLabel>Pending picks</TileLabel>
        <TileValue>{pending ? pending.length : '—'}</TileValue>
      </Tile>
    </View>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Activity tile — last few council events as compact lines.
// ─────────────────────────────────────────────────────────────────────

const KIND_LABEL: Record<ActivityEntryDto['kind'], string> = {
  proposal: 'proposed',
  approved: 'approved',
  declined: 'declined',
  filled: 'filled',
  vetoed: 'vetoed',
};

function ActivityTile() {
  const { data, isLoading, isError, refetch } = useActivity(6);

  return (
    <Tile className="gap-2">
      <TileLabel>Agent activity</TileLabel>
      {isLoading ? (
        <View className="gap-2">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
        </View>
      ) : isError || !data ? (
        <ErrorState
          title="Couldn't load activity"
          description="The agent feed isn't reachable. Try again in a moment."
          onRetry={() => refetch()}
        />
      ) : data.length === 0 ? (
        <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
          Quiet so far — run the council from the Picks tab.
        </Text>
      ) : (
        data.slice(0, 4).map((e) => <ActivityLine key={e.id} entry={e} />)
      )}
    </Tile>
  );
}

function ActivityLine({ entry }: { entry: ActivityEntryDto }) {
  const router = useRouter();
  const tone =
    entry.kind === 'filled'
      ? 'text-mint dark:text-mint-dark'
      : entry.kind === 'vetoed'
        ? 'text-rose dark:text-rose-dark'
        : 'text-text-secondary dark:text-text-secondary-dark';
  // Activity ids are `act-{decision_uuid}` — strip the prefix to deep-link
  // into the trade biography.
  const decisionId = entry.id.startsWith('act-') ? entry.id.slice(4) : null;
  return (
    <Pressable
      onPress={decisionId ? () => router.push(`/decision/${decisionId}`) : undefined}
      disabled={!decisionId}
      accessibilityRole="button"
      accessibilityLabel={`Open ${entry.symbol} trade biography`}
      className="min-h-[32px] flex-row items-baseline gap-2 active:opacity-70">
      <Text className="w-14 text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
        {formatRelative(entry.timestamp)}
      </Text>
      <Text
        className="text-[13px] font-medium text-text-primary dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {entry.symbol}
      </Text>
      <Text className={cn('flex-1 text-[12px]', tone)} numberOfLines={1}>
        {KIND_LABEL[entry.kind]} · {entry.headline}
      </Text>
    </Pressable>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Review CTA — the screen's single platinum tile.
// ─────────────────────────────────────────────────────────────────────

function ReviewCTA() {
  const router = useRouter();
  const { data: pending } = usePendingApprovals();
  const n = pending?.length ?? 0;

  if (n === 0) return null;
  return (
    <BentoCTA
      label={`Review ${n} pick${n === 1 ? '' : 's'} →`}
      onPress={() => router.push('/approvals')}
      accessibilityLabel={`Review ${n} pending picks`}
    />
  );
}

// ─────────────────────────────────────────────────────────────────────
// Calibration tile (30d agreement). Hides until there's data.
// ─────────────────────────────────────────────────────────────────────

function AgreementTile() {
  const { data, isLoading } = useReviewAgreement(30);
  if (isLoading || !data || data.totalReviewed === 0) return null;

  const pct = Math.round(data.agreementPct);
  const tone: 'mint' | 'rose' | 'default' = pct >= 65 ? 'mint' : pct >= 45 ? 'default' : 'rose';

  return (
    <Tile className="flex-row items-center justify-between">
      <View className="flex-1 gap-0.5 pr-3">
        <TileLabel>Calibration · 30d</TileLabel>
        <Text className="text-[12px] leading-[17px] text-text-secondary dark:text-text-secondary-dark">
          Your grades match Reflection over {data.totalReviewed} review
          {data.totalReviewed === 1 ? '' : 's'}.
        </Text>
      </View>
      <TileValue size="lg" tone={tone}>
        {pct}%
      </TileValue>
    </Tile>
  );
}

// ─────────────────────────────────────────────────────────────────────
// System health tile. Self-hides when unreachable.
// ─────────────────────────────────────────────────────────────────────

function HealthTile() {
  const { data, isLoading, isError } = useHealthFull();
  if (isLoading || isError || !data) return null;

  return (
    <Tile inset className="gap-3">
      <TileLabel>System</TileLabel>
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
    </Tile>
  );
}

function HealthCell({ label, h }: { label: string; h: ComponentHealth }) {
  return (
    <StatusPill tone={_toneFromStatus(h.status)} label={label} hint={h.label} layout="dot" />
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
