/**
 * Biometric gate — sits between the navigator and authenticated screens.
 *
 * Behavior:
 *   - On mount + when ``unlocked=false``, attempts ``LocalAuthentication.
 *     authenticateAsync()``. Success → flip ``unlocked=true`` AND unlock
 *     trading; failure / cancel → stay locked + show a "Try again" button.
 *   - When the app backgrounds + foregrounds, relock so the next
 *     foregrounding reprompts. Matches PLAN.md §3's "explicit
 *     acknowledgement on resume".
 *   - **No hardware / no enrolment → read-only, never a free pass.** This
 *     used to call ``unlock()`` unconditionally, so an attacker holding an
 *     unlocked phone could simply remove the enrolled face/finger and walk
 *     into a fully authenticated trading session. Now the app contents
 *     render (a demo device with flaky enrolment stays usable) but the
 *     trading lock in ``lib/api`` refuses every order-placing call, and a
 *     persistent banner says so.
 *   - **Web is out of scope.** Browsers expose no biometric API, so the
 *     gate passes through on ``Platform.OS === 'web'`` rather than pinning
 *     the desktop build to read-only forever.
 *   - Repeated failures are counted. After ``MAX_ATTEMPTS`` the prompt
 *     stops re-arming itself; the user has to press "Try again"
 *     deliberately, which kills silent brute-force retry loops.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { AppStateStatus } from 'react-native';
import { AppState, Platform, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as LocalAuthentication from 'expo-local-authentication';

import { Button } from '@app/ui';

import { setTradingUnlocked } from '@/lib/api';
import { useBiometricStore } from '@/stores/biometricStore';

interface Props {
  children: React.ReactNode;
  /** When false, the gate is a passthrough. Wire to "user is authenticated". */
  enabled: boolean;
}

/** Consecutive failures before we stop auto-prompting. */
const MAX_ATTEMPTS = 5;

const NO_BIOMETRICS_REASON =
  'This device has no enrolled biometrics, so trading actions are disabled. ' +
  'Enrol Face ID / Touch ID (or a device passcode) to approve trades.';

const LOCKED_REASON = 'Unlock with Face ID / Touch ID to approve trades.';

export function BiometricGate({ children, enabled }: Props) {
  const unlocked = useBiometricStore((s) => s.unlocked);
  const unlock = useBiometricStore((s) => s.unlock);
  const lock = useBiometricStore((s) => s.lock);

  const appState = useRef(AppState.currentState);
  // "Biometrics are unavailable on this device" — read-only mode, not a
  // lock screen. Distinct from "available but not yet passed".
  const [readOnly, setReadOnly] = useState(false);
  const [attempts, setAttempts] = useState(0);

  const prompt = useCallback(async () => {
    // Web has no biometric API — expo-local-authentication reports "no
    // hardware" on every browser. Treating that as a failed enrolment
    // would put the desktop build permanently in read-only mode, which is
    // security theatre rather than security: on the web the equivalent
    // control is the OS/browser session, not Face ID. So the gate is
    // NOT APPLICABLE here and we pass through. Native keeps failing
    // closed — that is where a stolen unlocked phone is the real threat.
    if (Platform.OS === 'web') {
      setReadOnly(false);
      setTradingUnlocked(true);
      unlock();
      return;
    }

    // Hardware probe. expo-local-authentication returns false on simulators
    // and on devices whose enrolment was removed.
    const hasHardware = await LocalAuthentication.hasHardwareAsync();
    const enrolled = hasHardware ? await LocalAuthentication.isEnrolledAsync() : false;

    if (!hasHardware || !enrolled) {
      // Fail CLOSED for anything that moves money; let the rest render so a
      // device with broken enrolment can still be used to watch positions.
      setTradingUnlocked(false, NO_BIOMETRICS_REASON);
      setReadOnly(true);
      unlock();
      return;
    }

    setReadOnly(false);
    const res = await LocalAuthentication.authenticateAsync({
      promptMessage: 'Unlock Autonomous Trader',
      cancelLabel: 'Cancel',
      disableDeviceFallback: false,
      requireConfirmation: false,
    });
    if (res.success) {
      setAttempts(0);
      setTradingUnlocked(true);
      unlock();
      return;
    }
    // Count every non-success (cancel included — a cancel loop is exactly
    // how a shoulder-surfer probes for a moment of inattention).
    setAttempts((n) => n + 1);
  }, [unlock]);

  const retry = useCallback(() => {
    setAttempts(0);
    void prompt();
  }, [prompt]);

  // Initial mount: prompt once if locked. Stops re-arming after
  // MAX_ATTEMPTS — the user must press "Try again" explicitly.
  useEffect(() => {
    if (enabled && !unlocked && attempts < MAX_ATTEMPTS) {
      void prompt();
    }
  }, [enabled, unlocked, attempts, prompt]);

  // Background → foreground → re-lock + re-prompt.
  useEffect(() => {
    function handle(next: AppStateStatus) {
      const prev = appState.current;
      appState.current = next;
      if (!enabled) return;
      // Active → background/inactive: lock now so the contents flash isn't
      // visible if the user re-foregrounds before the next prompt resolves.
      if (prev === 'active' && (next === 'background' || next === 'inactive')) {
        setTradingUnlocked(false, LOCKED_REASON);
        lock();
      }
      // background/inactive → active: re-prompt.
      if (prev !== 'active' && next === 'active') {
        setAttempts(0);
        void prompt();
      }
    }
    const sub = AppState.addEventListener('change', handle);
    return () => sub.remove();
  }, [enabled, prompt, lock]);

  // The gate being disabled (signed out) must not leave trading unlocked.
  useEffect(() => {
    if (!enabled) setTradingUnlocked(false, LOCKED_REASON);
  }, [enabled]);

  if (!enabled) {
    return <>{children}</>;
  }

  if (unlocked) {
    return (
      <>
        {readOnly ? <ReadOnlyBanner onRetry={retry} /> : null}
        {children}
      </>
    );
  }

  const exhausted = attempts >= MAX_ATTEMPTS;

  return (
    <SafeAreaView className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <View className="flex-1 items-center justify-center px-6 gap-4">
        <Text className="text-[22px] font-bold text-text-primary dark:text-text-primary-dark">
          Locked
        </Text>
        <Text className="text-center text-[14px] leading-[20px] text-text-secondary dark:text-text-secondary-dark">
          {exhausted
            ? `Too many failed attempts (${attempts}). Tap below to try again.`
            : 'Use Face ID / Touch ID to unlock the app.'}
        </Text>
        <Button
          label="Try again"
          variant="primary"
          onPress={retry}
          accessibilityLabel="Retry biometric unlock"
          testID="biometric-retry"
        />
      </View>
    </SafeAreaView>
  );
}

/**
 * Persistent, non-dismissable notice that trading is disabled. Uses the
 * ``danger`` token per DESIGN.md — this is a capability the user expects to
 * have and doesn't, so it must not be mistakable for a passing toast.
 */
function ReadOnlyBanner({ onRetry }: { onRetry: () => void }) {
  return (
    <View
      className="flex-row items-center gap-3 bg-danger dark:bg-danger-dark px-4 py-3"
      accessibilityRole="alert"
      accessibilityLabel="Trading disabled: this device has no enrolled biometrics"
    >
      {/* text-white on `danger` matches the Button's destructive label —
          the design system's established on-danger foreground. */}
      <Text className="flex-1 text-[13px] leading-[18px] font-semibold text-white">
        Read-only — no biometrics enrolled. Approvals and orders are disabled.
      </Text>
      <Button
        label="Retry"
        variant="secondary"
        onPress={onRetry}
        accessibilityLabel="Re-check biometric enrolment"
        testID="biometric-readonly-retry"
      />
    </View>
  );
}
