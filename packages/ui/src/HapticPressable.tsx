/**
 * HapticPressable — Pressable that fires a haptic on press.
 *
 * Haptics policy (DESIGN.md §9):
 *   - Primary CTA → light
 *   - Approve trade → medium
 *   - Decline trade → light
 *   - Fills → success
 *   - Risk breach → warning
 *   - Drawdown breaker → error
 *
 * Do NOT haptic on scroll, navigation, or routine taps. It becomes noise.
 */

import * as Haptics from 'expo-haptics';
import { Pressable } from 'react-native';
import type { PressableProps } from 'react-native';

type HapticKind = 'light' | 'medium' | 'heavy' | 'success' | 'warning' | 'error' | 'selection';

interface HapticPressableProps extends PressableProps {
  haptic?: HapticKind;
}

async function fire(kind: HapticKind): Promise<void> {
  switch (kind) {
    case 'light':
      return Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    case 'medium':
      return Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    case 'heavy':
      return Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy);
    case 'success':
      return Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    case 'warning':
      return Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
    case 'error':
      return Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
    case 'selection':
      return Haptics.selectionAsync();
  }
}

export function HapticPressable({ haptic = 'light', onPress, ...rest }: HapticPressableProps) {
  return (
    <Pressable
      onPress={(e) => {
        void fire(haptic);
        onPress?.(e);
      }}
      {...rest}
    />
  );
}
