/**
 * Biometric gate — sits between the navigator and authenticated screens.
 *
 * Behavior:
 *   - On mount + when ``unlocked=false``, attempts ``LocalAuthentication.
 *     authenticateAsync()``. Success → flip ``unlocked=true``; failure /
 *     cancel → stay locked + show a "Try again" button.
 *   - When the app backgrounds + foregrounds, relock so the next
 *     foregrounding reprompts. Matches PLAN.md §3's "explicit
 *     acknowledgement on resume".
 *   - If the device has no biometric hardware enrolled (simulator, older
 *     device), we let the user pass with a single tap so the dev loop
 *     doesn't get blocked — we'd never ship this fallback to production
 *     without a fallback PIN, but for Phase 3.1 it's acceptable.
 */

import { useCallback, useEffect, useRef } from 'react';
import { AppState, AppStateStatus, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as LocalAuthentication from 'expo-local-authentication';

import { Button } from '@app/ui';

import { useBiometricStore } from '@/stores/biometricStore';

interface Props {
  children: React.ReactNode;
  /** When false, the gate is a passthrough. Wire to "user is authenticated". */
  enabled: boolean;
}

export function BiometricGate({ children, enabled }: Props) {
  const unlocked = useBiometricStore((s) => s.unlocked);
  const unlock = useBiometricStore((s) => s.unlock);
  const lock = useBiometricStore((s) => s.lock);

  const appState = useRef(AppState.currentState);

  const prompt = useCallback(async () => {
    // Hardware probe. expo-local-authentication returns false on simulators
    // without enrolled biometrics. We fall through with a single-tap unlock
    // in that case so dev doesn't get stuck.
    const hasHardware = await LocalAuthentication.hasHardwareAsync();
    const enrolled = hasHardware ? await LocalAuthentication.isEnrolledAsync() : false;

    if (!hasHardware || !enrolled) {
      unlock();
      return;
    }

    const res = await LocalAuthentication.authenticateAsync({
      promptMessage: 'Unlock Autonomous Trader',
      cancelLabel: 'Cancel',
      disableDeviceFallback: false,
      requireConfirmation: false,
    });
    if (res.success) {
      unlock();
    }
  }, [unlock]);

  // Initial mount: prompt once if locked.
  useEffect(() => {
    if (enabled && !unlocked) {
      void prompt();
    }
  }, [enabled, unlocked, prompt]);

  // Background → foreground → re-lock + re-prompt.
  useEffect(() => {
    function handle(next: AppStateStatus) {
      const prev = appState.current;
      appState.current = next;
      if (!enabled) return;
      // Active → background/inactive: lock now so the contents flash isn't
      // visible if the user re-foregrounds before the next prompt resolves.
      if (prev === 'active' && (next === 'background' || next === 'inactive')) {
        lock();
      }
      // background/inactive → active: re-prompt.
      if (prev !== 'active' && next === 'active') {
        void prompt();
      }
    }
    const sub = AppState.addEventListener('change', handle);
    return () => sub.remove();
  }, [enabled, prompt, lock]);

  if (!enabled || unlocked) {
    return <>{children}</>;
  }

  return (
    <SafeAreaView className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <View className="flex-1 items-center justify-center px-6 gap-4">
        <Text className="text-[22px] font-bold text-text-primary dark:text-text-primary-dark">
          Locked
        </Text>
        <Text className="text-center text-[14px] leading-[20px] text-text-secondary dark:text-text-secondary-dark">
          Use Face ID / Touch ID to unlock the app.
        </Text>
        <Button
          label="Try again"
          variant="primary"
          onPress={() => void prompt()}
          accessibilityLabel="Retry biometric unlock"
          testID="biometric-retry"
        />
      </View>
    </SafeAreaView>
  );
}
