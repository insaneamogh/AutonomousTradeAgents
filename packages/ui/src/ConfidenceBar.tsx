/**
 * ConfidenceBar — horizontal 0..1 bar with optional delta indicator.
 *
 * Used in:
 *   - Strategies tab card (current per-strategy prior).
 *   - Reflection summary (before → after delta indicator).
 *
 * The bar fills from the LEFT proportionally to ``value``. Tone is
 * driven by where the value sits:
 *
 *   value >= 0.65  → ``gain``
 *   value >= 0.45  → ``accentPrimary``
 *   value <  0.45  → ``warning``
 *
 * A subtle marker at 0.5 indicates the cold-start prior so the user
 * can see how far Reflection has nudged each strategy from neutral.
 */

import { Text, View } from 'react-native';

import { cn } from './utils';

interface ConfidenceBarProps {
  value: number;
  /** 0..1; values outside the range are clamped. */
  size?: 'sm' | 'md';
  /** When provided, renders a small triangle indicator at this position
   * to show "before" while ``value`` shows "after". Useful in Reflection
   * summaries.
   */
  previousValue?: number;
  showLabel?: boolean;
  testID?: string;
}

function _toneClasses(value: number): { fill: string; label: string } {
  if (value >= 0.65) {
    return {
      fill: 'bg-gain dark:bg-gain-dark',
      label: 'text-gain dark:text-gain-dark',
    };
  }
  if (value >= 0.45) {
    return {
      fill: 'bg-accent-primary dark:bg-accent-primary-dark',
      label: 'text-accent-primary dark:text-accent-primary-dark',
    };
  }
  return {
    fill: 'bg-warning dark:bg-warning-dark',
    label: 'text-warning dark:text-warning-dark',
  };
}

export function ConfidenceBar({
  value,
  size = 'md',
  previousValue,
  showLabel = true,
  testID,
}: ConfidenceBarProps) {
  const v = Math.max(0, Math.min(1, value));
  const prev = previousValue !== undefined ? Math.max(0, Math.min(1, previousValue)) : undefined;
  const tone = _toneClasses(v);

  const barHeight = size === 'sm' ? 4 : 6;

  return (
    <View className="gap-1" testID={testID}>
      {showLabel ? (
        <View className="flex-row items-center justify-between">
          <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
            Confidence
          </Text>
          <Text
            className={cn('text-[13px] font-semibold', tone.label)}
            style={{ fontVariant: ['tabular-nums'] }}
          >
            {(v * 100).toFixed(0)}
            <Text className="text-text-tertiary dark:text-text-tertiary-dark">%</Text>
          </Text>
        </View>
      ) : null}
      <View
        className="overflow-hidden rounded-full bg-bg-surface-muted dark:bg-bg-surface-muted-dark"
        style={{ height: barHeight }}
      >
        {/* Cold-start prior marker at 0.5. Hairline; visible behind the fill. */}
        <View
          className="absolute h-full w-px bg-border-strong dark:bg-border-strong-dark"
          style={{ left: '50%' }}
        />
        <View
          className={cn('h-full rounded-full', tone.fill)}
          style={{ width: `${v * 100}%` }}
        />
        {/* Previous-value indicator: small dot at the prior position. */}
        {prev !== undefined && Math.abs(prev - v) > 0.005 ? (
          <View
            className="absolute h-full w-0.5 bg-text-primary/40 dark:bg-text-primary-dark/40"
            style={{ left: `${prev * 100}%` }}
          />
        ) : null}
      </View>
    </View>
  );
}
