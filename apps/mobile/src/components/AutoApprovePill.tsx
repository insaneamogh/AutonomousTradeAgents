// Auto-approve mode pill (docs/PLAN_AUTO_APPROVE.md) — top-of-Home control.
//
// Shows the account's current entry-approval mode:
//   ASK  — every proposal waits for a human tap. The shipped default.
//   AUTO — the reconciler's sweeper may execute a pending proposal by
//          itself, up to the daily cap, on the paper account only.
//
// Arming a real autonomous-trading feature is not a cosmetic switch, so
// turning it ON requires an explicit confirmation naming the actual
// consequence — mirroring CircuitBreakerBanner's "Acknowledge & resume"
// flow. Turning it back OFF (the safe direction) is immediate, the same
// asymmetry the breaker uses (halting needs no confirmation; resuming
// does). Reflects the server's `autoApproveConsent` only — see
// `useSetAutoApproveConsent`'s docstring for why there is no optimistic
// flip here.

import { useMemo } from 'react';
import { Alert, Pressable, Text } from 'react-native';
import * as Haptics from 'expo-haptics';
import { Zap } from 'lucide-react-native';
import { useColorScheme } from 'nativewind';

import { cn, palette } from '@app/ui';

import {
  useBrokerConnections,
  useSetAutoApproveConsent,
} from '@/hooks/useBrokerConnections';

export function AutoApprovePill() {
  const { data: connections } = useBrokerConnections();
  const setConsent = useSetAutoApproveConsent();
  const { colorScheme } = useColorScheme();

  // Auto-approve is paper-only, hard-coded server-side (plan §2, gate 2 —
  // not configurable) — the active paper Alpaca connection is the one
  // real thing this control can ever act on.
  const connection = useMemo(
    () =>
      (connections ?? []).find(
        (c) => c.broker === 'alpaca' && c.status === 'active' && c.isPaper,
      ),
    [connections],
  );

  const armed = connection?.autoApproveConsent === true;
  const busy = setConsent.isPending;
  const unavailable = !connection;

  const confirmTurnOn = () => {
    if (!connection) return;
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => {});
    Alert.alert(
      'Turn on autonomous entries?',
      'The agent will open new paper-account positions on its own, with ' +
        'no approval tap from you, up to the daily cap. Every auto-opened ' +
        'trade still passes the full risk gate and is logged as ' +
        'machine-approved, not yours. Live trading stays fully blocked no ' +
        'matter what — this can only ever act on the paper account. This ' +
        'is the same paper account already connected in Settings, with ' +
        'whatever positions and history it already has — arming this ' +
        'does not reset it or create a new one. You can turn it back ' +
        'off instantly, any time.',
      [
        { text: 'Not yet', style: 'cancel' },
        {
          text: 'Turn on',
          style: 'destructive',
          onPress: () =>
            setConsent.mutate({ connectionId: connection.id, enabled: true }),
        },
      ],
    );
  };

  const turnOff = () => {
    if (!connection) return;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
    setConsent.mutate({ connectionId: connection.id, enabled: false });
  };

  const onPress = () => {
    if (unavailable) {
      Alert.alert(
        'Connect a broker first',
        'Auto-approve needs an active Alpaca paper connection before it ' +
          'can be armed. Add one from Settings.',
      );
      return;
    }
    if (armed) {
      turnOff();
    } else {
      confirmTurnOn();
    }
  };

  const accessibilityLabel = unavailable
    ? 'Auto-approve unavailable. Connect a broker in Settings first.'
    : armed
      ? 'Auto-approve is on. The agent can open paper trades without your ' +
        'approval, up to the daily cap. Double tap to turn off.'
      : 'Auto-approve is off. Every trade waits for your approval. Double ' +
        'tap to turn on autonomous entries.';

  const iconColor = palette[colorScheme === 'light' ? 'light' : 'dark'].warning;

  return (
    <Pressable
      onPress={onPress}
      disabled={busy}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityState={{ disabled: busy, selected: armed }}
      testID="auto-approve-pill"
      className={cn(
        'min-h-[44px] flex-row items-center justify-center gap-2 self-start',
        'rounded-full border px-4',
        armed
          ? 'border-warning/40 bg-warning-subtle dark:border-warning-dark/40 dark:bg-warning-subtle-dark'
          : 'border-hairline bg-bg-tile dark:border-hairline-dark dark:bg-bg-tile-dark',
        unavailable && 'opacity-50',
        busy && 'opacity-70',
      )}
    >
      {armed && !busy ? <Zap size={12} color={iconColor} fill={iconColor} /> : null}
      <Text
        className={cn(
          'text-[11px] font-bold uppercase tracking-[1.2px]',
          armed
            ? 'text-warning dark:text-warning-dark'
            : 'text-text-secondary dark:text-text-secondary-dark',
        )}
      >
        {busy ? (armed ? 'Turning off…' : 'Arming…') : armed ? 'AUTO' : 'ASK'}
      </Text>
      <Text
        className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark"
        numberOfLines={1}
      >
        {armed ? 'agent can enter trades' : 'entries need your tap'}
      </Text>
      {setConsent.isError ? (
        <Text className="text-[10px] font-medium text-danger dark:text-danger-dark">
          Failed — try again
        </Text>
      ) : null}
    </Pressable>
  );
}
