// Picks — Design D bento feed.
//
// Pending proposals + decided/vetoed history in one list with
// All / Pending / Vetoed filter chips. Vetoed rows stay visible
// (dimmed, named risk rule) — the audit trail is part of the UI.
// Tapping a pending pick opens /pick/[id] for the full bull/bear/risk
// breakdown + approve sheet.

import { useMemo, useState } from 'react';
import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import type { ActivityEntryDto, ApprovalProposalDto } from '@app/shared-types';
import { EmptyState, ErrorState, Skeleton, cn, formatRelative } from '@app/ui';

import {
  BentoCTA,
  DirectionPill,
  HeroHeadline,
  HeroSub,
  Tile,
  TileLabel,
  levelLabel,
} from '@/components/bento';
import { usePendingApprovals } from '@/hooks/useApprovals';
import { useActivity } from '@/hooks/useActivity';
import { useStartCouncilRun } from '@/hooks/useCouncilRun';

const TICKERS = ['NVDA', 'AAPL', 'MSFT', 'TSLA', 'AMD', 'AMZN', 'GOOGL'] as const;

type Filter = 'all' | 'pending' | 'vetoed';

export default function PicksScreen() {
  const router = useRouter();
  const { data: pending, isLoading, isError, refetch } = usePendingApprovals();
  const { data: activity } = useActivity(30);
  const startRun = useStartCouncilRun();

  const [filter, setFilter] = useState<Filter>('all');
  const [tickerIndex, setTickerIndex] = useState(0);

  const vetoed = useMemo(
    () => (activity ?? []).filter((e) => e.kind === 'vetoed'),
    [activity],
  );
  const decided = useMemo(
    () => (activity ?? []).filter((e) => e.kind === 'approved' || e.kind === 'declined' || e.kind === 'filled'),
    [activity],
  );

  const nextSymbol = TICKERS[tickerIndex % TICKERS.length];
  const handleRunCouncil = () => {
    setTickerIndex((i) => i + 1);
    startRun.mutate(
      { symbol: nextSymbol, horizon: 'short' },
      {
        // Theater: jump straight into the live council view.
        onSuccess: (res) =>
          router.push({ pathname: `/council/${res.runId}`, params: { symbol: res.symbol } }),
      },
    );
  };

  const pendingList = pending ?? [];
  const showPending = filter !== 'vetoed';
  const showVetoed = filter !== 'pending';
  const showDecided = filter === 'all';

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-3">
        <View>
          <HeroHeadline>
            {pendingList.length > 0
              ? `${pendingList.length} pick${pendingList.length === 1 ? '' : 's'}`
              : 'Picks'}
          </HeroHeadline>
          <HeroSub>
            {pendingList.length > 0
              ? 'waiting for your approval'
              : 'Every council run lands here — including vetoes.'}
          </HeroSub>
        </View>

        <View className="flex-row gap-2">
          <FilterChip label="All" active={filter === 'all'} onPress={() => setFilter('all')} />
          <FilterChip
            label="Pending"
            active={filter === 'pending'}
            onPress={() => setFilter('pending')}
          />
          <FilterChip
            label="Vetoed"
            active={filter === 'vetoed'}
            onPress={() => setFilter('vetoed')}
          />
        </View>

        {isLoading ? (
          <Tile className="gap-3">
            <Skeleton className="h-5 w-full" />
            <Skeleton className="h-5 w-2/3" />
          </Tile>
        ) : isError ? (
          <Tile>
            <ErrorState
              title="Couldn't load picks"
              description="The agent server isn't reachable. Try again in a moment."
              onRetry={() => refetch()}
            />
          </Tile>
        ) : (
          <>
            {showPending &&
              pendingList.map((p) => (
                <PendingTile key={p.id} p={p} onPress={() => router.push(`/pick/${p.id}`)} />
              ))}
            {showPending && pendingList.length === 0 && filter === 'pending' && (
              <Tile>
                <EmptyState
                  title="No pending picks"
                  description="Run the council below to generate one."
                />
              </Tile>
            )}
            {showVetoed && vetoed.map((e) => <VetoTile key={e.id} e={e} />)}
            {showVetoed && vetoed.length === 0 && filter === 'vetoed' && (
              <Tile>
                <EmptyState
                  title="No vetoes"
                  description="When a risk rule blocks a proposal it shows up here with the rule name."
                />
              </Tile>
            )}
            {showDecided && decided.slice(0, 10).map((e) => <DecidedTile key={e.id} e={e} />)}
          </>
        )}

        <Tile inset className="gap-2">
          <View className="flex-row items-center justify-between">
            <View>
              <TileLabel>Run council</TileLabel>
              <Text
                className="mt-0.5 text-[14px] font-medium text-text-primary dark:text-text-primary-dark"
                style={{ fontVariant: ['tabular-nums'] }}
              >
                Next: {nextSymbol}
              </Text>
            </View>
          </View>
          {startRun.isError && (
            <Text className="text-[12px] text-rose dark:text-rose-dark">
              Couldn't start the run — is the server reachable?
            </Text>
          )}
          <BentoCTA
            label={startRun.isPending ? 'Starting…' : `Run on ${nextSymbol}`}
            onPress={handleRunCouncil}
            disabled={startRun.isPending}
            accessibilityLabel={`Run the agent council on ${nextSymbol}`}
          />
        </Tile>
      </ScrollView>
    </SafeAreaView>
  );
}

function FilterChip({
  label,
  active,
  onPress,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Filter: ${label}`}
      accessibilityState={{ selected: active }}
      className={cn(
        'min-h-[32px] items-center justify-center rounded-full px-4 py-1.5',
        active
          ? 'bg-cta dark:bg-cta-dark'
          : 'border border-hairline dark:border-hairline-dark',
      )}
    >
      <Text
        className={cn(
          'text-[12px] font-medium',
          active
            ? 'text-cta-label dark:text-cta-label-dark'
            : 'text-text-secondary dark:text-text-secondary-dark',
        )}
      >
        {label}
      </Text>
    </Pressable>
  );
}

function PendingTile({ p, onPress }: { p: ApprovalProposalDto; onPress: () => void }) {
  const isBuy = p.side === 'BUY';
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Open ${p.symbol} pick detail`}
    >
      <Tile className="gap-2 active:opacity-80">
        <View className="flex-row items-center justify-between">
          <Text
            className="text-[16px] font-medium text-text-primary dark:text-text-primary-dark"
            style={{ fontVariant: ['tabular-nums'] }}
          >
            {p.symbol}
          </Text>
          <DirectionPill
            label={`${isBuy ? 'LONG' : 'SELL'} · ${levelLabel(p.convictionLevel)}`}
            tone={isBuy ? 'mint' : 'rose'}
          />
        </View>
        <Text
          className="text-[12px] leading-[17px] text-text-secondary dark:text-text-secondary-dark"
          numberOfLines={2}
        >
          {p.rationale}
        </Text>
        <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
          {p.qty} sh · ~${Math.round(p.estimatedNotional).toLocaleString('en-US')} ·{' '}
          {formatRelative(p.proposedAt)} · tap for detail
        </Text>
      </Tile>
    </Pressable>
  );
}

function VetoTile({ e }: { e: ActivityEntryDto }) {
  return (
    <Tile className="gap-1.5 opacity-60">
      <View className="flex-row items-center justify-between">
        <Text
          className="text-[15px] font-medium text-text-primary dark:text-text-primary-dark"
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {e.symbol}
        </Text>
        <View className="rounded-full border border-rose px-2.5 py-1 dark:border-rose-dark">
          <Text className="text-[10px] font-semibold text-rose dark:text-rose-dark">
            VETOED
          </Text>
        </View>
      </View>
      <Text className="text-[11px] text-text-secondary dark:text-text-secondary-dark">
        {e.headline} · {formatRelative(e.timestamp)}
      </Text>
    </Tile>
  );
}

function DecidedTile({ e }: { e: ActivityEntryDto }) {
  const tone =
    e.kind === 'filled' ? 'mint' : e.kind === 'approved' ? 'mint' : ('muted' as const);
  return (
    <Tile inset className="gap-1">
      <View className="flex-row items-center justify-between">
        <Text
          className="text-[14px] font-medium text-text-primary dark:text-text-primary-dark"
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {e.symbol}
        </Text>
        <DirectionPill label={e.kind.toUpperCase()} tone={tone} />
      </View>
      <Text
        className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark"
        numberOfLines={1}
      >
        {e.headline} · {formatRelative(e.timestamp)}
      </Text>
    </Tile>
  );
}
