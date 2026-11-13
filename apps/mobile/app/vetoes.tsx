// Veto ledger — Design D bento.
//
// Every risk rule that fired in the window, as a public scorecard: how
// often, how much notional it blocked, and (where the ghost evaluator
// has finalized) how much loss it prevented. Null ghost renders "—",
// never $0 — absence of evidence is not evidence of savings.

import { useState } from 'react';
import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import { vetoRuleLabels, type VetoRuleDto } from '@app/shared-types';
import { EmptyState, Skeleton, cn } from '@app/ui';

import { HeroHeadline, HeroSub, Tile, TileLabel, TileValue } from '@/components/bento';
import { useVetoLedger } from '@/hooks/useInsights';
import type { InsightsCurrency, InsightsMarket } from '@/hooks/useInsights';

const MARKETS: { market: InsightsMarket; currency: InsightsCurrency; label: string }[] = [
  { market: 'US', currency: 'USD', label: 'USD' },
  { market: 'IN', currency: 'INR', label: 'INR' },
];

/** `$1,204` / `₹1,20,450` in the ledger's own currency. Never mix the two. */
function fmt(value: number, currency: InsightsCurrency): string {
  return Math.round(value).toLocaleString(currency === 'INR' ? 'en-IN' : 'en-US', {
    style: 'currency',
    currency,
    maximumFractionDigits: 0,
  });
}

export default function VetoLedgerScreen() {
  const router = useRouter();
  const [market, setMarket] = useState<InsightsMarket>('US');
  const { data, isLoading } = useVetoLedger(30, market);
  const currency: InsightsCurrency =
    data?.currency ?? MARKETS.find((m) => m.market === market)?.currency ?? 'USD';

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

        <View
          className="flex-row gap-2"
          accessibilityRole="tablist"
          accessibilityLabel="Ledger currency"
          testID="veto-market-control"
        >
          {MARKETS.map((m) => {
            const selected = m.market === market;
            return (
              <Pressable
                key={m.market}
                onPress={() => setMarket(m.market)}
                accessibilityRole="tab"
                accessibilityState={{ selected }}
                accessibilityLabel={`${m.label} ledger`}
                testID={`veto-market-${m.market}`}
                className={cn(
                  'min-h-[44px] flex-1 items-center justify-center rounded-full border',
                  selected
                    ? 'border-text-primary bg-text-primary dark:border-text-primary-dark dark:bg-text-primary-dark'
                    : 'border-hairline dark:border-hairline-dark',
                )}
              >
                <Text
                  className={cn(
                    'text-[13px] font-medium',
                    selected
                      ? 'text-bg-base dark:text-bg-base-dark'
                      : 'text-text-secondary dark:text-text-secondary-dark',
                  )}
                >
                  {m.label}
                </Text>
              </Pressable>
            );
          })}
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
                <TileValue>{fmt(data.totalBlockedNotional, currency)}</TileValue>
              </Tile>
            </View>
            {data.rules.map((r) => (
              <RuleTile key={r.rule} r={r} currency={currency} />
            ))}
            <Text className="mt-1 text-center text-[10px] leading-[14px] text-text-tertiary dark:text-text-tertiary-dark">
              &quot;Prevented&quot; uses finalized ghost outcomes - what the blocked trade actually
              did afterward. &quot;—&quot; means the evaluation window hasn&apos;t closed yet.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function RuleTile({ r, currency }: { r: VetoRuleDto; currency: InsightsCurrency }) {
  const prevented = r.preventedLossUsd;
  return (
    <Tile className="gap-2">
      <View className="flex-row items-center justify-between gap-2">
        <Text
          className="flex-1 text-[14px] font-medium text-text-primary dark:text-text-primary-dark"
          numberOfLines={1}
        >
          {vetoRuleLabels[r.rule] ?? r.rule}
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
          <TileValue>{fmt(r.blockedNotional, currency)}</TileValue>
        </Tile>
        <Tile inset className="flex-1 gap-0.5 p-2.5">
          <TileLabel>Prevented</TileLabel>
          <TileValue tone={prevented != null && prevented > 0 ? 'mint' : 'default'}>
            {prevented == null ? '—' : fmt(prevented, currency)}
          </TileValue>
        </Tile>
      </View>
    </Tile>
  );
}
