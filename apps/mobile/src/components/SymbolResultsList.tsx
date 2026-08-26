/**
 * SymbolResultsList — dumb typeahead results list for React Native.
 *
 * Deliberately a plain `.map()` over `Pressable` rows, not a `FlatList`:
 * this renders inside `SymbolTypeahead`, which itself sits inside
 * `watchlist.tsx`'s plain `ScrollView`. Nesting a VirtualizedList
 * (FlatList) inside a plain ScrollView trips RN's own "VirtualizedLists
 * should never be nested inside plain ScrollViews" warning/perf hazard —
 * and a capped ~8-row list never needs virtualization anyway.
 */

import { Pressable, Text, View } from 'react-native';

import type { SymbolHit } from '@/hooks/useSymbolSearch';

/**
 * Deliberately not `cn` from `@app/ui`: that package's barrel
 * (`src/index.ts`) re-exports `SkeletonLoader`, which imports
 * `react-native-reanimated` at module-eval time. Under Jest that throws
 * ("Worklets native part not initialized") the moment anything imports
 * it — which would make this component un-renderable in the very unit
 * test this file exists for. The join logic is a one-liner, so inlining
 * it avoids the barrel entirely.
 */
function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

export function SymbolResultsList({
  hits,
  activeIndex,
  onSelect,
}: {
  hits: SymbolHit[];
  activeIndex?: number;
  onSelect: (hit: SymbolHit) => void;
}) {
  if (hits.length === 0) return null;

  return (
    <View className="overflow-hidden rounded-lg border border-hairline bg-bg-surface-elevated dark:border-hairline-dark dark:bg-bg-surface-elevated-dark">
      {hits.map((hit, i) => (
        <Pressable
          key={hit.symbol}
          onPress={() => onSelect(hit)}
          accessibilityRole="button"
          accessibilityLabel={`${hit.symbol} — ${hit.name}`}
          className={cx(
            'min-h-[44px] flex-row items-center gap-2 px-3',
            i === activeIndex
              ? 'bg-bg-tile-inset dark:bg-bg-tile-inset-dark'
              : 'bg-bg-surface-elevated dark:bg-bg-surface-elevated-dark',
            i > 0 && 'border-t border-hairline dark:border-hairline-dark',
          )}
        >
          <Text
            className="text-[13px] font-semibold text-text-primary dark:text-text-primary-dark"
            style={{ fontVariant: ['tabular-nums'] }}
          >
            {hit.symbol}
          </Text>
          <Text
            className="flex-1 text-[12px] text-text-secondary dark:text-text-secondary-dark"
            numberOfLines={1}
          >
            {hit.name}
          </Text>
        </Pressable>
      ))}
    </View>
  );
}
