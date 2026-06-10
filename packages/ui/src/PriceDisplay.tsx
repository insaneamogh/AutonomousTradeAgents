/**
 * PriceDisplay — tabular-numeric price rendering.
 *
 * Two sizes: `md` (mono-price, lists) and `lg` (mono-price-lg, hero P&L).
 * Both apply `font-variant-numeric: tabular-nums` so columns of prices line
 * up — without this the app looks sloppy.
 *
 * Use this primitive for ANY user-facing number that represents money or a
 * share count. For percentages, use ``PnLBadge``.
 */

import { Text } from 'react-native';
import type { StyleProp, TextStyle } from 'react-native';

import { cn } from './utils';

interface PriceDisplayProps {
  /** Numeric value to render (assumed USD; no currency symbol prepended). */
  value: number;
  /** Default 2; use 0 for share counts, 4 for sub-dollar fractional prices. */
  fractionDigits?: number;
  /** Show $ prefix. Default true for money, set false for share counts. */
  withCurrencySymbol?: boolean;
  /** Show explicit +/− sign. */
  signed?: boolean;
  size?: 'md' | 'lg';
  /** Text color override — pass a `text-*` Tailwind class. Default `text-text-primary`. */
  tone?: 'primary' | 'gain' | 'loss' | 'neutral';
  className?: string;
  style?: StyleProp<TextStyle>;
}

const SIZE_CLASSES: Record<NonNullable<PriceDisplayProps['size']>, string> = {
  md: 'text-[17px] font-medium leading-[22px]',
  lg: 'text-[28px] font-semibold leading-[34px]',
};

const TONE_CLASSES: Record<NonNullable<PriceDisplayProps['tone']>, string> = {
  primary: 'text-text-primary dark:text-text-primary-dark',
  gain: 'text-gain dark:text-gain-dark',
  loss: 'text-loss dark:text-loss-dark',
  neutral: 'text-text-secondary dark:text-text-secondary-dark',
};

export function PriceDisplay({
  value,
  fractionDigits = 2,
  withCurrencySymbol = true,
  signed = false,
  size = 'md',
  tone = 'primary',
  className,
  style,
}: PriceDisplayProps) {
  const sign = signed ? (value > 0 ? '+' : value < 0 ? '−' : '') : '';
  const formatted = Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
  const symbol = withCurrencySymbol ? '$' : '';
  return (
    <Text
      // Tabular nums via inline style; NativeWind/Tailwind doesn't expose this utility yet.
      style={[{ fontVariant: ['tabular-nums'] }, style]}
      className={cn(SIZE_CLASSES[size], TONE_CLASSES[tone], className)}
    >
      {sign}
      {symbol}
      {formatted}
    </Text>
  );
}
