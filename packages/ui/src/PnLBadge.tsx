/**
 * PnLBadge — compact gain/loss pill for lists.
 *
 * Pairs an arrow (▲ ▼) with a percentage so the state is conveyed both by
 * shape AND color, satisfying WCAG and DESIGN.md §2 accessibility note.
 *
 * For hero P&L on the dashboard, use ``PriceDisplay`` with `size="lg"` and
 * `tone="gain" | "loss"` instead.
 */

import { Text, View } from 'react-native';

import { formatPct } from './utils';
import { cn } from './utils';

interface PnLBadgeProps {
  /** Signed percentage value, e.g. `2.34` for +2.34%, `-1.05` for −1.05%. */
  pct: number;
  size?: 'sm' | 'md';
  className?: string;
}

const SIZE_CLASSES: Record<NonNullable<PnLBadgeProps['size']>, string> = {
  sm: 'px-1.5 py-0.5 text-[11px]',
  md: 'px-2 py-1 text-[13px]',
};

export function PnLBadge({ pct, size = 'md', className }: PnLBadgeProps) {
  const positive = pct > 0;
  const negative = pct < 0;
  const arrow = positive ? '▲' : negative ? '▼' : '·';
  const bg = positive
    ? 'bg-gain-subtle dark:bg-gain-subtle-dark'
    : negative
      ? 'bg-loss-subtle dark:bg-loss-subtle-dark'
      : 'bg-bg-surface-muted dark:bg-bg-surface-muted-dark';
  const fg = positive
    ? 'text-gain dark:text-gain-dark'
    : negative
      ? 'text-loss dark:text-loss-dark'
      : 'text-text-secondary dark:text-text-secondary-dark';
  return (
    <View className={cn('flex-row items-center self-start rounded-sm', bg, SIZE_CLASSES[size], className)}>
      <Text className={cn('mr-1 font-medium', fg)}>{arrow}</Text>
      <Text
        className={cn('font-medium', fg)}
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {formatPct(pct)}
      </Text>
    </View>
  );
}
