// Drawdown circuit-breaker banner (DESIGN.md §: danger token, persistent,
// requires explicit acknowledgement).
//
// Renders nothing until the reconciler has halted the account on a daily
// drawdown breach. While halted, new BUYs are blocked server-side by the
// deterministic `drawdown_halt` rule; this banner is the only way the user
// clears it — "Acknowledge & resume" flips the breaker to manual_override.
// Kept prominent + non-dismissable-without-acknowledging on purpose.

import { Alert, Pressable, Text, View } from 'react-native';
import * as Haptics from 'expo-haptics';

import { useAcknowledgeBreaker, useCircuitBreaker } from '@/hooks/useCircuitBreaker';

export function CircuitBreakerBanner() {
  const { data } = useCircuitBreaker();
  const ack = useAcknowledgeBreaker();

  if (!data?.halted) return null;

  const dd =
    data.observedDrawdownPct != null ? ` (${data.observedDrawdownPct.toFixed(1)}%)` : '';

  const confirmAck = () => {
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => {});
    Alert.alert(
      'Resume trading?',
      'The agent halted on a daily drawdown breach and has blocked new buys. Acknowledging resumes trading for today. Sells to flatten are always allowed.',
      [
        { text: 'Keep halted', style: 'cancel' },
        {
          text: 'Acknowledge & resume',
          style: 'destructive',
          onPress: () => ack.mutate(),
        },
      ],
    );
  };

  return (
    <View
      accessibilityRole="alert"
      accessibilityLiveRegion="assertive"
      className="gap-2 rounded-xl bg-danger p-4 dark:bg-danger-dark"
    >
      <Text className="text-[13px] font-semibold uppercase tracking-[1px] text-white">
        Trading halted — drawdown circuit breaker
      </Text>
      <Text className="text-[12px] leading-[17px] text-white/90">
        {data.reason ?? `Daily drawdown limit hit${dd}.`} New buys are blocked;
        sells to flatten still go through.
      </Text>
      <Pressable
        onPress={confirmAck}
        disabled={ack.isPending}
        accessibilityRole="button"
        accessibilityLabel="Acknowledge the drawdown halt and resume trading"
        className="mt-1 min-h-[44px] items-center justify-center rounded-lg bg-white/15 active:bg-white/25"
      >
        <Text className="text-[13px] font-semibold text-white">
          {ack.isPending ? 'Acknowledging…' : 'Acknowledge & resume'}
        </Text>
      </Pressable>
    </View>
  );
}
