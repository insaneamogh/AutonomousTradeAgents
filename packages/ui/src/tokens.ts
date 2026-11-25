/**
 * Design tokens — the single source of truth.
 *
 * Mirror of `DESIGN.md` §2–§4. The Tailwind config in `apps/mobile/tailwind.config.js`
 * mirrors a subset of these so NativeWind classes resolve. When a token is added
 * here, mirror it there.
 *
 * Hard rules:
 *  - Never use raw hex in components. Always `palette[mode].<token>`.
 *  - Approve-button color = `accentPrimary` (clay), NEVER `gain`. Green is reserved for fills + P&L.
 *  - Accent TEXT on a page uses `accentText`; a label ON an accent fill uses `onAccent`.
 *  - All numerals use `font.mono` with tabular-nums.
 */

export type ThemeMode = 'light' | 'dark';

// ── Color ────────────────────────────────────────────────────────────
export const palette = {
  // Aurora Calm: warm ivory / warm slate, ONE accent (clay). Colour carries
  // meaning only (gain / loss / caution); everything else is a tint of ink.
  // Mirrors `apps/mobile/src/desktop/theme.ts` so web and native agree.
  light: {
    // Surfaces
    bgBase:             '#FAF9F5',
    bgSurface:          '#FFFFFF',
    bgSurfaceElevated:  '#FFFFFF',
    bgSurfaceMuted:     '#F5F3EC',
    // Borders
    borderSubtle:       '#E8E5DC',
    borderStrong:       '#CFCBBF',
    // Text
    textPrimary:        '#171614',
    textSecondary:      '#45433E',
    textTertiary:       '#6D6B65',
    // Accent. `accentPrimary` is a FILL (buttons, toggles, bars); text that
    // sits on the page uses `accentText`, which clears 4.5:1; a label that
    // sits ON a clay fill uses `onAccent`.
    accentPrimary:      '#D97757',
    accentPrimaryHover: '#C8643F',
    accentText:         '#A9502F',
    onAccent:           '#1A1613',
    // Trading
    gain:               '#2B7A4E',
    gainSubtle:         '#E3F1E8',
    loss:               '#B4453B',
    lossSubtle:         '#F9E6E3',
    neutral:            '#45433E',
    warning:            '#96620F',
    warningSubtle:      '#F6ECD6',
    danger:             '#9A2F26',
    info:               '#3C6FA8',
    // Bento (Design D)
    bgCanvas:           '#FAF9F5',
    bgTile:             '#FFFFFF',
    bgTileInset:        '#F3F1EA',
    cta:                '#D97757',
    ctaLabel:           '#1A1613',
    mint:               '#2B7A4E',
    mintSubtle:         '#E3F1E8',
    rose:               '#B4453B',
    roseSubtle:         '#F9E6E3',
    hairline:           '#E8E5DC',
  },
  dark: {
    bgBase:             '#121211',
    bgSurface:          '#1A1A18',
    bgSurfaceElevated:  '#201F1D',
    bgSurfaceMuted:     '#201F1D',
    borderSubtle:       '#2E2D2A',
    borderStrong:       '#45433F',
    textPrimary:        '#F4F1EA',
    textSecondary:      '#C5C2BA',
    textTertiary:       '#8F8C85',
    accentPrimary:      '#D97757',
    accentPrimaryHover: '#E38A6D',
    accentText:         '#E8947A',
    onAccent:           '#1A1613',
    gain:               '#7BC99A',
    gainSubtle:         '#1B2A21',
    loss:               '#E4857C',
    lossSubtle:         '#2D1B19',
    neutral:            '#C5C2BA',
    warning:            '#D9A85B',
    warningSubtle:      '#332914',
    danger:             '#E4857C',
    info:               '#8FB1DB',
    // Bento (Design D)
    bgCanvas:           '#121211',
    bgTile:             '#1C1B19',
    bgTileInset:        '#242320',
    cta:                '#D97757',
    ctaLabel:           '#1A1613',
    mint:               '#7BC99A',
    mintSubtle:         '#1B2A21',
    rose:               '#E4857C',
    roseSubtle:         '#2D1B19',
    hairline:           '#2E2D2A',
  },
} as const;

export type ColorToken = keyof typeof palette.light;

// ── Typography ────────────────────────────────────────────────────────
export const font = {
  body: 'Inter',
  mono: 'Inter',  // same family, used with tabular-nums for prices
} as const;

export const type = {
  display:         { size: 32, line: 38, weight: '600' as const },
  h1:              { size: 24, line: 30, weight: '600' as const },
  h2:              { size: 20, line: 26, weight: '600' as const },
  h3:              { size: 17, line: 24, weight: '600' as const },
  body:            { size: 15, line: 22, weight: '400' as const },
  bodyEmphasis:    { size: 15, line: 22, weight: '500' as const },
  caption:         { size: 13, line: 18, weight: '400' as const },
  captionEmphasis: { size: 13, line: 18, weight: '500' as const },
  micro:           { size: 11, line: 14, weight: '500' as const },
  monoPrice:       { size: 17, line: 22, weight: '500' as const },
  monoPriceLg:     { size: 28, line: 34, weight: '600' as const },
} as const;

// ── Spacing (4pt base) ────────────────────────────────────────────────
export const spacing = {
  0: 0, 1: 2, 2: 4, 3: 8, 4: 12, 5: 16, 6: 20, 7: 24, 8: 32, 9: 40, 10: 48, 11: 64,
} as const;

// ── Radii ─────────────────────────────────────────────────────────────
export const radii = {
  sm: 6,
  md: 10,
  lg: 14,
  xl: 20,
  full: 9999,
} as const;

// ── Motion ────────────────────────────────────────────────────────────
export const motion = {
  micro:     { duration: 150, easing: 'ease-out' as const },
  state:     { duration: 200, easing: 'ease-out' as const },
  sheet:     { duration: 280, easing: 'spring' as const, damping: 22, stiffness: 200 },
  navigate:  { duration: 320, easing: 'spring' as const },
} as const;
