/**
 * Biometric lock state — separate from auth.
 *
 * The auth store says "is there a session?". This store says "even though
 * there's a session, is the user currently locked out behind Face ID /
 * Touch ID / biometric prompt?"
 *
 * Default behavior: locked-on-launch + locked-on-background (resume from
 * background → re-prompt). Settings screen lets the user disable the
 * launch-prompt; the resume-from-background prompt stays on regardless,
 * matching PLAN.md §3's "explicit acknowledgement" stance.
 */

import { create } from 'zustand';

interface BiometricState {
  /** True until the user passes biometric. False = app contents are hidden. */
  unlocked: boolean;
  /** Set to true by Settings → "Require biometric on launch". Persistence
   * is a follow-on; for now the default is "always require".
   */
  requireOnLaunch: boolean;

  unlock: () => void;
  lock: () => void;
  setRequireOnLaunch: (value: boolean) => void;
}

export const useBiometricStore = create<BiometricState>((set) => ({
  // We start LOCKED so a fresh-launch always sees the prompt before any
  // sensitive screen renders.
  unlocked: false,
  requireOnLaunch: true,

  unlock: () => set({ unlocked: true }),
  lock: () => set({ unlocked: false }),
  setRequireOnLaunch: (value) => set({ requireOnLaunch: value }),
}));
