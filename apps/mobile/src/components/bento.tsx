/**
 * Bento primitives — the Design D ("editorial bento") building blocks.
 *
 * Layout language:
 *   - Screen = canvas (`bg-canvas`) with a large editorial headline.
 *   - Every content block = a Tile on the canvas. No borders; separation
 *     comes from the tile/canvas contrast.
 *   - Exactly ONE platinum/ink CTA per screen (`BentoCTA`). The CTA is
 *     `cta` (ink in light, platinum in dark) — NEVER green, per DESIGN.md:
 *     green is reserved for fills + positive P&L.
 *   - Numerals: Inter + tabular-nums, weight 500, tight tracking.
 */

import { Pressable, Text, View } from 'react-native';
import type { ViewProps } from 'react-native';

import { cn } from '@app/ui';

/** RiskLevel / conviction (1–5) → caps label for pills. */
export function levelLabel(level: 1 | 2 | 3 | 4 | 5): string {
  return level <= 2 ? 'LOW' : level === 3 ? 'MED' : 'HIGH';
}

/** A bento tile. `inset` is the slightly-darker nested variant. */
export function Tile({
  inset = false,
  className,
  children,
  ...rest
}: ViewProps & { inset?: boolean }) {
  return (
    <View
      className={cn(
        'rounded-lg p-3.5',
        inset
          ? 'bg-bg-tile-inset dark:bg-bg-tile-inset-dark'
          : 'bg-bg-tile dark:bg-bg-tile-dark',
        className,
      )}
      {...rest}
    >
      {children}
    </View>
  );
}

/** 10px caps label inside tiles. */
export function TileLabel({ children }: { children: React.ReactNode }) {
  return (
    <Text className="text-[10px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
      {children}
    </Text>
  );
}

/** Tile stat numeral. */
export function TileValue({
  children,
  tone = 'default',
  size = 'md',
}: {
  children: React.ReactNode;
  tone?: 'default' | 'mint' | 'rose';
  size?: 'md' | 'lg';
}) {
  return (
    <Text
      className={cn(
        'font-medium',
        size === 'lg' ? 'text-[22px] leading-[26px]' : 'text-[19px] leading-[24px]',
        tone === 'default' && 'text-text-primary dark:text-text-primary-dark',
        tone === 'mint' && 'text-mint dark:text-mint-dark',
        tone === 'rose' && 'text-rose dark:text-rose-dark',
      )}
      style={{ fontVariant: ['tabular-nums'], letterSpacing: -0.3 }}
    >
      {children}
    </Text>
  );
}

/** The big editorial headline at the top of a bento screen. */
export function HeroHeadline({ children }: { children: React.ReactNode }) {
  return (
    <Text
      className="text-[30px] font-medium leading-[34px] text-text-primary dark:text-text-primary-dark"
      style={{ letterSpacing: -0.6, fontVariant: ['tabular-nums'] }}
    >
      {children}
    </Text>
  );
}

/** Muted line under the headline. */
export function HeroSub({ children }: { children: React.ReactNode }) {
  return (
    <Text className="mt-0.5 text-[13px] leading-[18px] text-text-secondary dark:text-text-secondary-dark">
      {children}
    </Text>
  );
}

/**
 * The single platinum/ink CTA tile. One per screen.
 * 44pt min height per a11y rules.
 */
export function BentoCTA({
  label,
  onPress,
  disabled = false,
  accessibilityLabel,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  accessibilityLabel?: string;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ disabled }}
      className={cn(
        'min-h-[48px] items-center justify-center rounded-lg bg-cta px-4 py-3 active:opacity-85 dark:bg-cta-dark',
        disabled && 'opacity-40',
      )}
    >
      <Text className="text-[14px] font-semibold text-cta-label dark:text-cta-label-dark">
        {label}
      </Text>
    </Pressable>
  );
}

/** Secondary (quiet, hairline-outlined) action. */
export function BentoQuiet({
  label,
  onPress,
  disabled = false,
  accessibilityLabel,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  accessibilityLabel?: string;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ disabled }}
      className={cn(
        'min-h-[48px] items-center justify-center rounded-lg border border-hairline px-4 py-3 active:opacity-70 dark:border-hairline-dark',
        disabled && 'opacity-40',
      )}
    >
      <Text className="text-[14px] font-medium text-text-secondary dark:text-text-secondary-dark">
        {label}
      </Text>
    </Pressable>
  );
}

/** Direction/percent pill — mint for long/gain, rose for trim/short. */
export function DirectionPill({
  label,
  tone,
}: {
  label: string;
  tone: 'mint' | 'rose' | 'muted';
}) {
  return (
    <View
      className={cn(
        'rounded-full px-2.5 py-1',
        tone === 'mint' && 'bg-mint-subtle dark:bg-mint-subtle-dark',
        tone === 'rose' && 'bg-rose-subtle dark:bg-rose-subtle-dark',
        tone === 'muted' && 'bg-bg-tile-inset dark:bg-bg-tile-inset-dark',
      )}
    >
      <Text
        className={cn(
          'text-[11px] font-semibold',
          tone === 'mint' && 'text-mint dark:text-mint-dark',
          tone === 'rose' && 'text-rose dark:text-rose-dark',
          tone === 'muted' && 'text-text-secondary dark:text-text-secondary-dark',
        )}
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {label}
      </Text>
    </View>
  );
}
