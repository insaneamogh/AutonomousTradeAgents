/**
 * Notifications store.
 *
 * Owns the local view of:
 *   - The OS permission status (granted / denied / undetermined).
 *   - The Expo push token (memory only; we don't persist it because
 *     ``Notifications.getExpoPushTokenAsync`` is fast + the token can
 *     rotate behind our back).
 *   - The user's *intent* — `enabled` is the toggle the user controls;
 *     `permission` reflects what the OS actually says. They can drift
 *     (user toggles "enabled" but OS still says "denied" — Settings UI
 *     surfaces that gap).
 *
 * The hook ``usePushRegistration`` writes here on its lifecycle ticks.
 */

import { create } from 'zustand';

export type PermissionStatus = 'undetermined' | 'granted' | 'denied' | 'unsupported';

interface NotificationsState {
  /** What the OS most recently told us. */
  permission: PermissionStatus;
  /** Expo push token from getExpoPushTokenAsync. Null when we don't have one yet. */
  expoPushToken: string | null;
  /** The user's stated preference. True = ask for permission + register; false = skip. */
  enabled: boolean;
  /** Last device id we registered with the API. Lets the Settings card surface a state. */
  registeredDeviceId: string | null;
  /** Last error from a registration attempt. UI surfaces it in the Settings card. */
  lastError: string | null;

  setPermission: (status: PermissionStatus) => void;
  setExpoPushToken: (token: string | null) => void;
  setEnabled: (enabled: boolean) => void;
  setRegisteredDeviceId: (id: string | null) => void;
  setLastError: (msg: string | null) => void;
}

export const useNotificationsStore = create<NotificationsState>((set) => ({
  permission: 'undetermined',
  expoPushToken: null,
  // Default to enabled — the OS prompt is the real gate. User can flip off
  // in Settings to deregister.
  enabled: true,
  registeredDeviceId: null,
  lastError: null,

  setPermission: (status) => set({ permission: status }),
  setExpoPushToken: (token) => set({ expoPushToken: token }),
  setEnabled: (enabled) => set({ enabled }),
  setRegisteredDeviceId: (id) => set({ registeredDeviceId: id }),
  setLastError: (msg) => set({ lastError: msg }),
}));
