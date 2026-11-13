// The refusal ledger as dots: one dot per refusal. Mint = it would have
// lost money (a good refusal), rose = it would have made money (cost us),
// grey = not marked yet. USD / INR segmented switch, one market per request.

import { useState } from 'react';
import { Text, View } from 'react-native';
import { useRouter } from 'expo-router';

import type { VetoRuleDto } from '@app/shared-types';
import { HapticPressable, Skeleton, cn } from '@app/ui';

import { Tile, TileLabel } from '@/components/bento';
import { useVetoLedger } from '@/hooks/useInsights';
import type { InsightsMarket } from '@/hooks/useInsights';

import { formatMoney } from './format';

const MARKETS: { market: InsightsMarket; currency: 'USD' | 'INR' }[] = [
  { market: 'US', currency: 'USD' },
  { market: 'IN', currency: 'INR' },
];
const MAX_DOTS = 60;

export type DotKind = 'saved' | 'missed' | 'pending';

/** Per rule: marked refusals split by loss-avoided vs upside-blocked dollars. */
export function buildDots(rules: VetoRuleDto[]): DotKind[] {
  const dots: DotKind[] = [];
  for (const r of rules) {
    const marked = Math.min(r.markedCount ?? 0, r.count);
    const loss = Math.max(r.lossAvoidedUsd ?? 0, 0);
    const up = Math.max(r.upsideBlockedUsd ?? 0, 0);
    const total = loss + up;
    const savedN = total > 0 ? Math.round((marked * loss) / total) : 0;
    for (let i = 0; i < marked; i++) dots.push(i < savedN ? 'saved' : 'missed');
    for (let i = marked; i < r.count; i++) dots.push('pending');
  }
  return dots;
}

const DOT_CLASS: Record<DotKind, string> = {
  saved: 'bg-mint dark:bg-mint-dark',
  missed: 'bg-rose dark:bg-rose-dark',
  pending: 'bg-hairline dark:bg-hairline-dark',
};

export function RefusalDotsTile() {
  const router = useRouter();
  const [market, setMarket] = useState<InsightsMarket>('US');
  const { data, isLoading, isError } = useVetoLedger(30, market);
  const currency = data?.currency ?? MARKETS.find((m) => m.market === market)?.currency ?? 'USD';

  const dots = data ? buildDots(data.rules ?? []) : [];
  const shown = dots.slice(0, MAX_DOTS);
  const saved = (data?.rules ?? []).reduce((s, r) => s + Math.max(r.lossAvoidedUsd ?? 0, 0), 0);
  const missed = (data?.rules ?? []).reduce((s, r) => s + Math.max(r.upsideBlockedUsd ?? 0, 0), 0);

  return (
    <Tile className="gap-3" testID="refusal-dots-tile">
      <View className="flex-row items-center justify-between">
        <TileLabel>Refusals · 30d</TileLabel>
        <View
          className="flex-row rounded-full bg-bg-tile-inset p-0.5 dark:bg-bg-tile-inset-dark"
          accessibilityRole="tablist"
          accessibilityLabel="Ledger currency"
        >
          {MARKETS.map((m) => (
            <HapticPressable
              key={m.market}
              haptic="selection"
              onPress={() => setMarket(m.market)}
              accessibilityRole="tab"
              accessibilityState={{ selected: market === m.market }}
              testID={`refusal-market-${m.currency}`}
              className={cn(
                'min-h-[32px] min-w-[44px] items-center justify-center rounded-full px-3',
                market === m.market && 'bg-bg-tile dark:bg-bg-tile-dark',
              )}
            >
              <Text className="text-[11px] font-semibold text-text-primary dark:text-text-primary-dark">
                {m.currency}
              </Text>
            </HapticPressable>
          ))}
        </View>
      </View>

      {isLoading ? (
        <Skeleton className="h-16 w-full" />
      ) : isError || !data ? (
        <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
          Ledger unavailable right now.
        </Text>
      ) : (
        <HapticPressable
          haptic="light"
          onPress={() => router.push('/vetoes')}
          accessibilityRole="button"
          accessibilityLabel="Open the veto ledger"
          className="gap-2"
        >
          <Text
            className="text-[44px] font-medium leading-[48px] text-text-primary dark:text-text-primary-dark"
            style={{ fontVariant: ['tabular-nums'], letterSpacing: -1 }}
          >
            {data.totalVetoes}
          </Text>
          {shown.length > 0 && (
            <View className="flex-row flex-wrap gap-1.5" testID="refusal-dots">
              {shown.map((k, i) => (
                <View key={i} className={cn('h-2.5 w-2.5 rounded-full', DOT_CLASS[k])} />
              ))}
              {dots.length > MAX_DOTS && (
                <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
                  +{dots.length - MAX_DOTS}
                </Text>
              )}
            </View>
          )}
          <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
            Avoided {formatMoney(saved, currency)} · blocked upside {formatMoney(missed, currency)} · grey
            = pending
          </Text>
        </HapticPressable>
      )}
    </Tile>
  );
}
