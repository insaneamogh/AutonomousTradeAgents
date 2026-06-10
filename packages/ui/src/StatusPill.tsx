/**
 * StatusPill — color-coded status indicator with label.
 *
 * Used in:
 *   - Home health-strip (per-component liveness).
 *   - Strategies tab card (decisions-in-window count).
 *   - Settings broker section (connection state).
 *
 * Tones:
 *   ok       — gain        ("everything's fine")
 *   warning  — warning     ("attention soon")
 *   danger   — danger      ("attention now")
 *   muted    — neutral     ("unknown / not-yet-set-up")
 *
 * Two layouts: dot (default — circle + label inline) and chip (filled
 * rounded background, used in dense rows where the dot would get lost).
 */

import { Text, View } from 'react-native';

import { cn } from './utils';

export type StatusTone = 'ok' | 'warning' | 'danger' | 'muted';
export type StatusLayout = 'dot' | 'chip';

interface StatusPillProps {
  tone: StatusTone;
  label: string;
  /** Optional one-line subtitle below the label (dot layout only). */
  hint?: string;
  layout?: StatusLayout;
  testID?: string;
}

const DOT_TONE: Record<StatusTone, string> = {
  ok: 'bg-gain dark:bg-gain-dark',
  warning: 'bg-warning dark:bg-warning-dark',
  danger: 'bg-danger dark:bg-loss-dark',
  muted: 'bg-text-tertiary dark:bg-text-tertiary-dark',
};

const CHIP_TONE: Record<StatusTone, { bg: string; text: string }> = {
  ok: {
    bg: 'bg-gain-subtle dark:bg-gain-subtle-dark',
    text: 'text-gain dark:text-gain-dark',
  },
  warning: {
    bg: 'bg-warning-subtle dark:bg-warning-subtle-dark',
    text: 'text-warning dark:text-warning-dark',
  },
  danger: {
    bg: 'bg-loss-subtle dark:bg-loss-subtle-dark',
    text: 'text-danger dark:text-loss-dark',
  },
  muted: {
    bg: 'bg-bg-surface-muted dark:bg-bg-surface-muted-dark',
    text: 'text-text-tertiary dark:text-text-tertiary-dark',
  },
};

export function StatusPill({
  tone,
  label,
  hint,
  layout = 'dot',
  testID,
}: StatusPillProps) {
  if (layout === 'chip') {
    const t = CHIP_TONE[tone];
    return (
      <View
        className={cn('flex-row items-center self-start rounded-full px-2 py-0.5', t.bg)}
        testID={testID}
      >
        <Text className={cn('text-[10px] font-semibold uppercase tracking-[1.1px]', t.text)}>
          {label}
        </Text>
      </View>
    );
  }

  return (
    <View className="flex-row items-start gap-2" testID={testID}>
      <View
        className={cn('mt-1 h-2 w-2 rounded-full', DOT_TONE[tone])}
        accessibilityLabel={`Status: ${tone}`}
      />
      <View className="flex-1 gap-0.5">
        <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
          {label}
        </Text>
        {hint ? (
          <Text
            className="text-[12px] leading-[16px] text-text-tertiary dark:text-text-tertiary-dark"
            numberOfLines={2}
          >
            {hint}
          </Text>
        ) : null}
      </View>
    </View>
  );
}
