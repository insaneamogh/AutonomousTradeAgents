// Veto ledger — Design D bento.
//
// Every risk rule that fired in the window, as a public scorecard: how
// often, how much notional it blocked, and (where the ghost evaluator
// has finalized) how much loss it prevented. Null ghost renders "—",
// never $0 — absence of evidence is not evidence of savings.

import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import type { VetoRuleDto } from '@app/shared-types';
import { EmptyState, Skeleton } from '@app/ui';

import { HeroHeadline, HeroSub, Tile, TileLabel, TileValue } from '@/components/bento';
import { useVetoLedger } from '@/hooks/useInsights';

// Named rule identifiers from engine.risk → human copy. Unknown rules
// fall back to the raw identifier (audit beats prettiness).
const RULE_LABEL: Record<string, string> = {
  drawdown_halt: 'Daily drawdown circuit breaker',
  forbid_short: 'Short selling blocked (Phase 0)',
  low_council_confidence: 'Council confidence too low',
  low_specialist_avg_score: 'Analyst scores too weak',
  pdt_block: 'Pattern day trader rule',
  max_open_positions: 'Too many open positions',
  position_size_cap: 'Position size cap',
  correlation_cap: 'Correlation cluster cap',
  sector_concentration: 'Sector concentration cap',
  single_name_concentration: 'Single name concentration cap',
  live_trading_disabled: 'Live trading disabled',
  unnamed_rule: 'Unnamed rule',
};

export default function VetoLedgerScreen() {
  const router = useRouter();
  const { data, isLoading } = useVetoLedger(30);

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
            ← Home
          </Text>
        </Pressable>

        <View>
          <HeroHeadline>Veto ledger</HeroHeadline>
          <HeroSub>What the risk engine blocked in the last {data?.windowDays ?? 30} days</HeroSub>
        </View>

        {isLoading ? (
          <>
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </>
        ) : !data || data.totalVetoes === 0 ? (
          <Tile>
            <EmptyState
              title="No vetoes in this window"
              description="When a named risk rule blocks a proposal, it shows up here with what it prevented."
            />
          </Tile>
        ) : (
          <>
            <View className="flex-row gap-3">
              <Tile className="flex-1 gap-1">
                <TileLabel>Blocked</TileLabel>
                <TileValue>{data.totalVetoes}</TileValue>
              </Tile>
              <Tile className="flex-1 gap-1">
                <TileLabel>Notional stopped</TileLabel>
                <TileValue>
                  ${Math.round(data.totalBlockedNotional).toLocaleString('en-US')}
                </TileValue>
              </Tile>
            </View>
            {data.rules.map((r) => (
              <RuleTile key={r.rule} r={r} />
            ))}
            <Text className="mt-1 text-center text-[10px] leading-[14px] text-text-tertiary dark:text-text-tertiary-dark">
              "Prevented" uses finalized ghost outcomes — what the blocked trade actually did
              afterward. "—" means the evaluation window hasn't closed yet.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function RuleTile({ r }: { r: VetoRuleDto }) {
  const prevented = r.preventedLossUsd;
  return (
    <Tile className="gap-2">
      <View className="flex-row items-center justify-between gap-2">
        <Text
          className="flex-1 text-[14px] font-medium text-text-primary dark:text-text-primary-dark"
          numberOfLines={1}
        >
          {RULE_LABEL[r.rule] ?? r.rule}
        </Text>
        <Text
          className="text-[14px] font-medium text-text-primary dark:text-text-primary-dark"
          style={{ fontVariant: ['tabular-nums'] }}
        >
          ×{r.count}
        </Text>
      </View>
      <Text className="text-[10px] uppercase tracking-[0.8px] text-text-tertiary dark:text-text-tertiary-dark">
        {r.rule}
      </Text>
      <View className="flex-row gap-2">
        <Tile inset className="flex-1 gap-0.5 p-2.5">
          <TileLabel>Blocked</TileLabel>
          <TileValue>${Math.round(r.blockedNotional).toLocaleString('en-US')}</TileValue>
        </Tile>
        <Tile inset className="flex-1 gap-0.5 p-2.5">
          <TileLabel>Prevented</TileLabel>
          <TileValue tone={prevented != null && prevented > 0 ? 'mint' : 'default'}>
            {prevented == null ? '—' : `$${Math.round(prevented).toLocaleString('en-US')}`}
          </TileValue>
        </Tile>
      </View>
    </Tile>
  );
}
