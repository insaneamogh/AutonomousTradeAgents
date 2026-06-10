// Council theater — Design D bento.
//
// Watches a background council run live: Router → analysts → Selector →
// Drafter → Risk Officer, each row flipping pending → running → done
// (or skipped) as the polled progress feed advances. The risk verdict
// row is the finale: approved links to the pick detail (platinum CTA —
// never green, per DESIGN.md), vetoed shows the named rule.

import { useMemo } from 'react';
import { ActivityIndicator, Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';

import type { AgentRunResponse, CouncilNode, CouncilProgressEvent } from '@app/shared-types';
import { cn } from '@app/ui';

import { BentoCTA, HeroHeadline, HeroSub, Tile } from '@/components/bento';
import { useCouncilProgress } from '@/hooks/useCouncilRun';

const NODE_ORDER: CouncilNode[] = [
  'router',
  'technical',
  'fundamental',
  'macro',
  'selector',
  'drafter',
  'risk_officer',
];

const NODE_LABEL: Record<CouncilNode, string> = {
  router: 'Router',
  technical: 'Technical analyst',
  fundamental: 'Fundamental analyst',
  macro: 'Macro analyst',
  selector: 'Strategy selector',
  drafter: 'Drafter',
  risk_officer: 'Risk officer',
};

type RowState = 'pending' | 'running' | 'done' | 'skipped';

interface NodeRow {
  node: CouncilNode;
  state: RowState;
  summary: Record<string, unknown> | null;
}

function buildRows(events: CouncilProgressEvent[]): NodeRow[] {
  const byNode = new Map<CouncilNode, { status: string; summary: Record<string, unknown> | null }>();
  for (const e of events) {
    byNode.set(e.node, { status: e.status, summary: e.summary });
  }
  return NODE_ORDER.map((node) => {
    const seen = byNode.get(node);
    if (!seen) return { node, state: 'pending' as const, summary: null };
    if (seen.status === 'skipped') return { node, state: 'skipped' as const, summary: null };
    if (seen.status === 'completed') return { node, state: 'done' as const, summary: seen.summary };
    return { node, state: 'running' as const, summary: null };
  });
}

export default function CouncilTheaterScreen() {
  const { runId, symbol } = useLocalSearchParams<{ runId: string; symbol?: string }>();
  const router = useRouter();
  const { data, isError } = useCouncilProgress(runId ?? null);

  const rows = useMemo(() => buildRows(data?.events ?? []), [data?.events]);
  const running = data == null || data.status === 'running';
  const result = data?.result ?? null;

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView contentContainerClassName="px-4 pb-16 pt-4 gap-3">
        <Pressable
          onPress={() => router.back()}
          accessibilityRole="button"
          accessibilityLabel="Back to picks"
          className="min-h-[44px] justify-center"
        >
          <Text className="text-[13px] text-text-secondary dark:text-text-secondary-dark">
            ← Picks
          </Text>
        </Pressable>

        <View>
          <HeroHeadline>{symbol ?? 'Council'}</HeroHeadline>
          <HeroSub>
            {running
              ? 'The council is deliberating…'
              : data?.status === 'failed'
                ? 'Run failed'
                : 'Deliberation complete'}
          </HeroSub>
        </View>

        {isError || data?.status === 'failed' ? (
          <Tile className="gap-1.5">
            <Text className="text-[13px] font-medium text-rose dark:text-rose-dark">
              {data?.error ?? "Couldn't reach the agent server."}
            </Text>
            <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
              The run may have expired — head back and run again.
            </Text>
          </Tile>
        ) : (
          rows.map((row) => <NodeRowTile key={row.node} row={row} />)
        )}

        {result != null && <VerdictTile result={result} onOpenPick={(id) => router.replace(`/pick/${id}`)} />}
      </ScrollView>
    </SafeAreaView>
  );
}

function NodeRowTile({ row }: { row: NodeRow }) {
  const dimmed = row.state === 'skipped';
  return (
    <Tile
      className={cn('flex-row items-start gap-3', dimmed && 'opacity-40')}
      accessibilityLabel={`${NODE_LABEL[row.node]}: ${row.state}`}
    >
      <View className="w-6 items-center pt-0.5">
        {row.state === 'running' ? (
          <ActivityIndicator size="small" />
        ) : (
          <Text
            className={cn(
              'text-[15px] font-medium',
              row.state === 'done' && 'text-mint dark:text-mint-dark',
              row.state === 'pending' && 'text-text-tertiary dark:text-text-tertiary-dark',
              row.state === 'skipped' && 'text-text-tertiary dark:text-text-tertiary-dark',
            )}
          >
            {row.state === 'done' ? '✓' : row.state === 'skipped' ? '–' : '·'}
          </Text>
        )}
      </View>
      <View className="flex-1 gap-0.5">
        <View className="flex-row items-baseline justify-between">
          <Text className="text-[14px] font-medium text-text-primary dark:text-text-primary-dark">
            {NODE_LABEL[row.node]}
          </Text>
          <ScoreBadge summary={row.summary} node={row.node} />
        </View>
        {row.summary?.thesis ? (
          <Text
            className="text-[12px] leading-[17px] text-text-secondary dark:text-text-secondary-dark"
            numberOfLines={3}
          >
            {String(row.summary.thesis)}
          </Text>
        ) : null}
      </View>
    </Tile>
  );
}

function ScoreBadge({
  summary,
  node,
}: {
  summary: Record<string, unknown> | null;
  node: CouncilNode;
}) {
  if (!summary) return null;
  if (node === 'risk_officer') {
    const approved = summary.approved === true;
    return (
      <Text
        className={cn(
          'text-[12px] font-semibold',
          approved ? 'text-mint dark:text-mint-dark' : 'text-rose dark:text-rose-dark',
        )}
      >
        {approved ? 'CLEAR' : `VETO · ${String(summary.vetoRule ?? 'rule fired')}`}
      </Text>
    );
  }
  if (typeof summary.score === 'number') {
    return (
      <Text
        className="text-[14px] font-medium text-text-primary dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {Math.round(summary.score)}
      </Text>
    );
  }
  if (typeof summary.regime === 'string') {
    return (
      <Text className="text-[11px] font-semibold uppercase tracking-[1px] text-text-secondary dark:text-text-secondary-dark">
        {summary.regime}
      </Text>
    );
  }
  if (typeof summary.strategy === 'string') {
    return (
      <Text className="text-[11px] text-text-secondary dark:text-text-secondary-dark" numberOfLines={1}>
        {summary.strategy}
      </Text>
    );
  }
  return null;
}

function VerdictTile({
  result,
  onOpenPick,
}: {
  result: AgentRunResponse;
  onOpenPick: (id: string) => void;
}) {
  if (result.proposal != null) {
    const p = result.proposal;
    return (
      <Tile className="gap-2 bg-mint-subtle dark:bg-mint-subtle-dark">
        <Text className="text-[10px] font-semibold uppercase tracking-[1.1px] text-mint dark:text-mint-dark">
          Proposal ready
        </Text>
        <Text className="text-[14px] font-medium text-text-primary dark:text-text-primary-dark">
          {p.side} {p.qty} {p.symbol} · ~$
          {Math.round(p.estimatedNotional).toLocaleString('en-US')}
        </Text>
        <BentoCTA
          label="Review & approve"
          onPress={() => onOpenPick(p.id)}
          accessibilityLabel={`Review the ${p.symbol} proposal`}
        />
      </Tile>
    );
  }
  return (
    <Tile className="gap-1.5">
      <Text className="text-[10px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
        {result.finalAction === 'VETOED' ? 'Vetoed by the risk engine' : 'Council holds'}
      </Text>
      <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
        {result.riskReason || 'No trade this run.'}
        {result.riskVetoRule ? ` (${result.riskVetoRule})` : ''}
      </Text>
    </Tile>
  );
}
