/**
 * Push registration lifecycle hook.
 *
 * Lifecycle:
 *   1. Root layout mounts → this hook runs.
 *   2. If the user is signed in + biometric-unlocked + ``enabled`` is true:
 *        a. Read current OS permission.
 *        b. If undetermined, prompt.
 *        c. If granted, fetch Expo push token + POST to /register-device.
 *   3. If the user toggles ``enabled=false`` in Settings, revoke the
 *      registered device (best-effort) + clear local state.
 *
 * The hook is idempotent — re-mounts + re-renders don't re-register the
 * same token. The API side is also idempotent on (userId, expoPushToken)
 * so a duplicate POST is harmless.
 *
 * Web platform is a no-op (Expo Push doesn't target web — needs FCM /
 * web push, which is a follow-on).
 */

import { useEffect, useRef } from 'react';
import { Platform } from 'react-native';
import * as Notifications from 'expo-notifications';
import Constants from 'expo-constants';

import { ApiError, request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';
import { useBiometricStore } from '@/stores/biometricStore';
import { useNotificationsStore } from '@/stores/notificationsStore';

interface RegisterDeviceResponse {
  id: string;
}

export function usePushRegistration(): void {
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');
  const unlocked = useBiometricStore((s) => s.unlocked);
  const enabled = useNotificationsStore((s) => s.enabled);
  const registeredDeviceId = useNotificationsStore((s) => s.registeredDeviceId);

  const setPermission = useNotificationsStore((s) => s.setPermission);
  const setExpoPushToken = useNotificationsStore((s) => s.setExpoPushToken);
  const setRegisteredDeviceId = useNotificationsStore((s) => s.setRegisteredDeviceId);
  const setLastError = useNotificationsStore((s) => s.setLastError);

  // Guard against double-registration across renders.
  const inFlight = useRef(false);

  useEffect(() => {
    if (Platform.OS === 'web') {
      setPermission('unsupported');
      return;
    }
    if (!isAuthed || !unlocked || !enabled) return;
    if (registeredDeviceId) return; // already registered this device
    if (inFlight.current) return;

    inFlight.current = true;
    (async () => {
      try {
        // Probe current permission. iOS gives 'undetermined' the first
        // time; Android usually 'granted' until the user denies.
        let perm = await Notifications.getPermissionsAsync();
        if (perm.status === 'undetermined' || (!perm.granted && perm.canAskAgain)) {
          perm = await Notifications.requestPermissionsAsync();
        }
        if (!perm.granted) {
          setPermission(perm.status === 'denied' ? 'denied' : 'undetermined');
          return;
        }
        setPermission('granted');

        const projectId =
          Constants.expoConfig?.extra?.eas?.projectId ??
          (Constants.easConfig as { projectId?: string } | undefined)?.projectId;

        // getExpoPushTokenAsync requires a projectId in EAS builds; in
        // local dev (Expo Go), the call still works but emits a warning.
        // We pass projectId when we have it.
        const tokenInfo = projectId
          ? await Notifications.getExpoPushTokenAsync({ projectId })
          : await Notifications.getExpoPushTokenAsync();

        setExpoPushToken(tokenInfo.data);

        const platform: 'ios' | 'android' | 'web' =
          Platform.OS === 'ios' ? 'ios' : Platform.OS === 'android' ? 'android' : 'web';
        const label = Constants.deviceName ?? null;

        const resp = await request<RegisterDeviceResponse>(
          '/api/v1/notifications/register-device',
          {
            method: 'POST',
            body: {
              expoPushToken: tokenInfo.data,
              platform,
              label,
            },
          },
        );
        setRegisteredDeviceId(resp.id);
        setLastError(null);
      } catch (err) {
        if (err instanceof ApiError) {
          const detail =
            typeof err.body === 'object' && err.body && 'detail' in err.body
              ? String((err.body as { detail: unknown }).detail)
              : err.message;
          setLastError(detail);
        } else if (err instanceof Error) {
          setLastError(err.message);
        } else {
          setLastError('Registration failed.');
        }
      } finally {
        inFlight.current = false;
      }
    })();
  }, [
    isAuthed,
    unlocked,
    enabled,
    registeredDeviceId,
    setPermission,
    setExpoPushToken,
    setRegisteredDeviceId,
    setLastError,
  ]);
}

/** Best-effort revoke when the user toggles notifications off. */
export async function revokeRegisteredDevice(): Promise<void> {
  const id = useNotificationsStore.getState().registeredDeviceId;
  if (!id) return;
  try {
    await request(`/api/v1/notifications/devices/${id}`, { method: 'DELETE' });
  } catch {
    // Swallow — local state below is the source of truth for the toggle.
  } finally {
    useNotificationsStore.getState().setRegisteredDeviceId(null);
    useNotificationsStore.getState().setExpoPushToken(null);
  }
}
