/**
 * Desktop runtime bootstrap — stylesheet + webfont injection.
 *
 * WHY injection instead of a CSS import or `@expo-google-fonts/*`:
 *
 *   - A `.css` import would have to be resolved by Metro for the NATIVE
 *     bundle too, and this tree must be provably inert on iOS/Android.
 *     A string injected into `document.head` only ever runs on web.
 *   - `@expo-google-fonts/space-grotesk` ships a native TTF and needs
 *     `useFonts()` to gate the first paint — that is a change to the
 *     shared boot path, i.e. a change to MOBILE. Desktop is web-only, so
 *     a `<link>` to Google Fonts gets the same family for zero new
 *     dependencies and zero risk to the mobile render.
 *
 * Both injections are idempotent and guarded by an id, so Fast Refresh
 * and remounts don't stack duplicates.
 */

import { PLATINUM_CSS } from './theme';

const STYLE_ID = 'platinum-glass-tokens';
const FONT_ID = 'platinum-glass-fonts';

const FONT_HREF =
  'https://fonts.googleapis.com/css2' +
  '?family=Inter:wght@400;500;600;700' +
  '&family=Space+Grotesk:wght@400;500;600;700' +
  '&display=swap';

/** Idempotently install the Platinum Glass stylesheet + webfonts. */
export function installPlatinumGlass(): void {
  if (typeof document === 'undefined') return;

  if (!document.getElementById(FONT_ID)) {
    const preconnect = document.createElement('link');
    preconnect.rel = 'preconnect';
    preconnect.href = 'https://fonts.gstatic.com';
    preconnect.crossOrigin = 'anonymous';
    document.head.appendChild(preconnect);

    const link = document.createElement('link');
    link.id = FONT_ID;
    link.rel = 'stylesheet';
    link.href = FONT_HREF;
    document.head.appendChild(link);
  }

  if (!document.getElementById(STYLE_ID)) {
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = PLATINUM_CSS;
    document.head.appendChild(style);
  }
}
