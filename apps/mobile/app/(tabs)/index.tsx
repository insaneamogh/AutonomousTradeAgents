// Home — calm tiles: autonomy verdict, equity, counts, refusal dots, sweep,
// mode, then the agent-activity list.
// Health + calibration strips stay — they collapse to nothing when
// there's no signal. Scanner is the one exception: it always renders,
// because the whole point is making an off-by-default feature visible.

import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import type { ActivityEntryDto } from '@app/shared-types';
import { ErrorState, Skeleton, StatusPill, cn, formatRelative } from '@app/ui';

import { AutoApprovePill } from '@/components/AutoApprovePill';
import { DirectionPill, Tile, TileLabel, TileValue } from '@/components/bento';
import { CircuitBreakerBanner } from '@/components/CircuitBreakerBanner';
import { AutonomyTile } from '@/components/home/AutonomyTile';
import { CountTile } from '@/components/home/CountTile';
import { EquityTile } from '@/components/home/EquityTile';
import { NextSweepTile } from '@/components/home/NextSweepTile';
import { RefusalDotsTile } from '@/components/home/RefusalDotsTile';
import { TradingModeTile } from '@/components/home/TradingModeTile';
import { useAccount } from '@/hooks/useAccount';
import { useActivity } from '@/hooks/useActivity';
import { usePendingApprovals } from '@/hooks/useApprovals';
import type { ComponentHealth, ComponentStatus } from '@/hooks/useHealthFull';
import { useHealthFull } from '@/hooks/useHealthFull';
import { useReviewAgreement } from '@/hooks/useReview';
import type { ScanSignalDto } from '@/hooks/useScannerStatus';
import { useScannerStatus } from '@/hooks/useScannerStatus';

function HomeCounts() {
  const router = useRouter();
  const { data: account } = useAccount();
  const { data: pending } = usePendingApprovals();
  return (
    <View className="flex-row gap-3">
      <CountTile label="Open positions" value={account?.openPositions} />
      <CountTile
        label="Pending picks"
        value={pending?.length}
        hint="tap to review"
        onPress={() => router.push('/approvals')}
        testID="pending-picks"
      />
    </View>
  );
}

export default function HomeScreen() {
  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-3">
        <CircuitBreakerBanner />
        <AutoApprovePill />
        <AutonomyTile />
        <EquityTile />
        <HomeCounts />
        <RefusalDotsTile />
        <View className="flex-row gap-3">
          <NextSweepTile />
          <TradingModeTile />
        </View>
        <ActivityTile />
        <AgreementTile />
        <HealthTile />
        <ScannerTile />
      </ScrollView>
    </SafeAreaView>
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
  // A HOLD never reached the risk officer — "vetoed" would claim a
  // rule fired when none did.
  hold: 'held',
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
          Quiet so far - run the council from the Picks tab.
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
      className="min-h-[32px] flex-row items-baseline gap-2 active:opacity-70"
    >
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
// Calibration tile (30d agreement). Hides until there's data.
// ─────────────────────────────────────────────────────────────────────

function AgreementTile() {
  const router = useRouter();
  const { data, isLoading } = useReviewAgreement(30);
  if (isLoading || !data || data.totalReviewed === 0) return null;

  const pct = Math.round(data.agreementPct);
  const tone: 'mint' | 'rose' | 'default' = pct >= 65 ? 'mint' : pct >= 45 ? 'default' : 'rose';

  return (
    <Pressable
      onPress={() => router.push('/calibration')}
      accessibilityRole="button"
      accessibilityLabel="Open the calibration scorecard"
      className="active:opacity-80"
    >
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
    </Pressable>
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
  return <StatusPill tone={_toneFromStatus(h.status)} label={label} hint={h.label} layout="dot" />;
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
// Scanner tile — the deterministic trigger loop (engine.scanner).
//
// Unlike the tiles above, this one never self-hides: SCANNER_ENABLED
// defaults to off, and the point of this tile is making that visible,
// not hiding it further. All four states (scheduler off / not armed /
// armed-clean / armed-with-signals) render something.
// ─────────────────────────────────────────────────────────────────────

function ScannerTile() {
  const { data, isLoading } = useScannerStatus();

  return (
    <Tile className="gap-2">
      <TileLabel>Scanner</TileLabel>
      {isLoading || !data ? (
        <View className="gap-2">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </View>
      ) : !data.schedulerEnabled ? (
        <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
          Off · set COUNCIL_SCHEDULER_ENABLED=1 to arm the scheduler.
        </Text>
      ) : !data.triggerLoopArmed ? (
        <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
          {data.scannerEnabledFlag
            ? 'SCANNER_ENABLED=1 but Alpaca data keys are missing — not started.'
            : 'Baseline sweep only · set SCANNER_ENABLED=1 to scan between sweeps.'}
        </Text>
      ) : data.signals.length === 0 ? (
        <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">
          Watching {data.watchlistSize} symbol{data.watchlistSize === 1 ? '' : 's'} · no triggers right
          now.
        </Text>
      ) : (
        <View className="gap-1.5">
          {data.signals.slice(0, 4).map((s: ScanSignalDto, i: number) => (
            <View key={`${s.symbol}-${s.rule}-${i}`} className="flex-row items-center gap-2">
              <Text
                className="w-12 text-[13px] font-medium text-text-primary dark:text-text-primary-dark"
                style={{ fontVariant: ['tabular-nums'] }}
              >
                {s.symbol}
              </Text>
              <DirectionPill
                label={s.direction === 'bullish' ? 'BULL' : 'BEAR'}
                tone={s.direction === 'bullish' ? 'mint' : 'rose'}
              />
              <Text
                className="flex-1 text-[12px] text-text-secondary dark:text-text-secondary-dark"
                numberOfLines={1}
              >
                {s.rule.replace(/_/g, ' ')}
              </Text>
            </View>
          ))}
          {data.signals.length > 4 && (
            <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
              +{data.signals.length - 4} more
            </Text>
          )}
        </View>
      )}
    </Tile>
  );
}
