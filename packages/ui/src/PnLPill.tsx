/**
 * PnLPill — small filled pill that surfaces a realized P&L value.
 *
 * Differs from `PnLBadge` (which is a soft chip with a percent number)
 * in that it carries the FULL signed dollar value + a colored background
 * for at-a-glance scanability in dense lists (Review tab, Activity feed).
 *
 * Sizes: sm (chip-density rows) and md (review-card hero).
 */

import { Text, View } from 'react-native';

import { cn } from './utils';

interface PnLPillProps {
  value: number;
  size?: 'sm' | 'md';
  /** Override the tone; defaults to gain/loss based on value sign. */
  tone?: 'gain' | 'loss' | 'neutral';
}

const SIZE_CONTAINER: Record<'sm' | 'md', string> = {
  sm: 'h-7 px-2.5',
  md: 'h-9 px-3.5',
};

const SIZE_TEXT: Record<'sm' | 'md', string> = {
  sm: 'text-[12px] font-semibold',
  md: 'text-[15px] font-bold',
};

export function PnLPill({ value, size = 'md', tone }: PnLPillProps) {
  const resolvedTone: 'gain' | 'loss' | 'neutral' =
    tone ?? (value > 0 ? 'gain' : value < 0 ? 'loss' : 'neutral');

  return (
    <View
      className={cn(
        'flex-row items-center self-start rounded-full',
        SIZE_CONTAINER[size],
        resolvedTone === 'gain' &&
          'bg-gain-subtle dark:bg-gain-subtle-dark',
        resolvedTone === 'loss' &&
          'bg-loss-subtle dark:bg-loss-subtle-dark',
        resolvedTone === 'neutral' &&
          'bg-bg-surface-muted dark:bg-bg-surface-muted-dark',
      )}
      accessibilityLabel={`Realized P and L ${value >= 0 ? 'gain' : 'loss'} ${Math.abs(value).toFixed(2)} dollars`}
    >
      <Text
        className={cn(
          SIZE_TEXT[size],
          resolvedTone === 'gain' && 'text-gain dark:text-gain-dark',
          resolvedTone === 'loss' && 'text-loss dark:text-loss-dark',
          resolvedTone === 'neutral' &&
            'text-text-secondary dark:text-text-secondary-dark',
        )}
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {value > 0 ? '+' : ''}
        {value.toLocaleString(undefined, {
          style: 'currency',
          currency: 'USD',
          maximumFractionDigits: 2,
        })}
      </Text>
    </View>
  );
}
