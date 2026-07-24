/**
 * Theme store.
 *
 * Owns the user's appearance preference: 'system' (follow the OS), 'light', or
 * 'dark'. The preference persists synchronously in MMKV so the first render
 * already reflects it (no flash of the wrong theme on launch).
 *
 * The actual switch is driven through NativeWind's ``colorScheme.set`` — with
 * ``darkMode: 'class'`` in tailwind.config.js this is what flips every
 * ``dark:`` variant across the app. ``applyTheme`` is called once on boot from
 * the root layout and again whenever the user changes the preference.
 */

import { colorScheme } from 'nativewind';
import { create } from 'zustand';

import { kv } from '@/lib/kv';

export type ThemePreference = 'system' | 'light' | 'dark';

const STORAGE_KEY = 'autotrader.prefs.theme';

function loadInitial(): ThemePreference {
  const raw = kv.getString(STORAGE_KEY);
  return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'system';
}

/** Push the preference into NativeWind. 'system' hands control back to the OS. */
function applyToColorScheme(pref: ThemePreference): void {
  colorScheme.set(pref);
}

interface ThemeState {
  preference: ThemePreference;
  setPreference: (pref: ThemePreference) => void;
  /** Re-apply the stored preference to NativeWind. Called once on app boot. */
  applyTheme: () => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  preference: loadInitial(),
  setPreference: (pref) => {
    kv.set(STORAGE_KEY, pref);
    applyToColorScheme(pref);
    set({ preference: pref });
  },
  applyTheme: () => applyToColorScheme(get().preference),
}));
