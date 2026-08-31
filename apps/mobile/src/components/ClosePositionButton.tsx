// The "Close now" / "Cancel order" control from app/positions.tsx, pulled
// out into its own presentational component so it can be unit-tested
// directly. It deliberately does NOT import from `@app/ui` (whose barrel
// pulls in `SkeletonLoader` → `react-native-reanimated`, which throws at
// import time under this project's Jest setup with no native module
// registered) — a plain local class-join keeps this file cheap to render
// in a test.
//
// Disabling (not hiding) for a read-only demo session
// (docs/IMPL_DEMO_SESSION.md §4) is deliberate: a judge should still see
// the control and why it's inert, not have it silently vanish.

import { Pressable, Text } from 'react-native';

import { DEMO_DISABLED_REASON } from '@/lib/demoSession';

function joinClasses(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

export function ClosePositionButton({
  symbol,
  qty,
  pending,
  busy,
  disabledForDemo,
  onPress,
}: {
  symbol: string;
  qty: number;
  /** True for a working, not-yet-filled order — "Cancel order" instead of "Close now". */
  pending: boolean;
  /** The close/cancel mutation is in flight for THIS row. */
  busy: boolean;
  disabledForDemo: boolean;
  onPress: () => void;
}) {
  const disabled = busy || disabledForDemo;
  const baseLabel = pending ? `Cancel the working ${symbol} order` : `Close ${qty} ${symbol} now`;
  const accessibilityLabel = disabledForDemo ? `${baseLabel} — ${DEMO_DISABLED_REASON}` : baseLabel;

  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      className={joinClasses(
        'mt-1 min-h-[44px] items-center justify-center rounded-lg',
        'border border-hairline dark:border-hairline-dark',
        disabled && 'opacity-50',
      )}
    >
      <Text className="text-[13px] font-medium text-text-primary dark:text-text-primary-dark">
        {busy ? (pending ? 'Cancelling…' : 'Closing…') : pending ? 'Cancel order' : 'Close now'}
      </Text>
    </Pressable>
  );
}
