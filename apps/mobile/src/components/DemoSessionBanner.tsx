// Read-only demo session banner (docs/IMPL_DEMO_SESSION.md §4).
//
// Structurally the same shape as CircuitBreakerBanner: reads one piece of
// state, renders nothing unless the condition holds. Mounted ONCE at the
// root (app/_layout.tsx), above both the native tab tree and the desktop
// Platinum Glass shell, rather than duplicated per-screen the way
// CircuitBreakerBanner is — a demo session is a standing identity/permission
// fact that must stay visible across every route for the whole session, not
// a per-screen "you have something actionable here" alert.
//
// Unlike CircuitBreakerBanner/BiometricGate's ReadOnlyBanner (both `danger`
// — something is WRONG), this is informational: a judge is looking at the
// real account on purpose. `info` tone, not `danger`/`warning`.

import { Text, View } from 'react-native';

import { useIsDemoSession } from '@/lib/demoSession';

export function DemoSessionBanner() {
  const isDemo = useIsDemoSession();
  if (!isDemo) return null;

  return (
    <View
      accessibilityRole="alert"
      accessibilityLabel="Read-only demo session banner"
      className="flex-row items-center gap-2 bg-info px-4 py-2.5 dark:bg-info-dark"
    >
      <Text className="flex-1 text-[12px] font-semibold leading-[17px] text-white">
        Read-only demo · viewing the live paper account · trading actions are disabled
      </Text>
    </View>
  );
}
