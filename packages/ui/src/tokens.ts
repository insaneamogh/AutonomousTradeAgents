/**
 * Design tokens — the single source of truth.
 *
 * Mirror of `DESIGN.md` §2–§4. The Tailwind config in `apps/mobile/tailwind.config.js`
 * mirrors a subset of these so NativeWind classes resolve. When a token is added
 * here, mirror it there.
 *
 * Hard rules:
 *  - Never use raw hex in components. Always `palette[mode].<token>`.
 *  - Approve-button color = `accentPrimary`, NEVER `gain`. Green is reserved for fills + P&L.
 *  - All numerals use `font.mono` with tabular-nums.
 */

export type ThemeMode = 'light' | 'dark';

// ── Color ────────────────────────────────────────────────────────────
export const palette = {
  light: {
    // Surfaces
    bgBase:             '#FAFAF9',
    bgSurface:          '#FFFFFF',
    bgSurfaceElevated:  '#FFFFFF',
    bgSurfaceMuted:     '#F5F5F4',
    // Borders
    borderSubtle:       '#E7E5E4',
    borderStrong:       '#D6D3D1',
    // Text
    textPrimary:        '#0C0A09',
    textSecondary:      '#57534E',
    textTertiary:       '#A8A29E',
    // Accent
    accentPrimary:      '#1E40AF',
    accentPrimaryHover: '#1E3A8A',
    // Trading
    gain:               '#15803D',
    gainSubtle:         '#DCFCE7',
    loss:               '#B91C1C',
    lossSubtle:         '#FEE2E2',
    neutral:            '#57534E',
    warning:            '#B45309',
    warningSubtle:      '#FEF3C7',
    danger:             '#991B1B',
    info:               '#1E40AF',
    // Bento (Design D)
    bgCanvas:           '#FAFAF9',
    bgTile:             '#FFFFFF',
    bgTileInset:        '#F4F4F3',
    cta:                '#1B1B1F',
    ctaLabel:           '#FAFAF9',
    mint:               '#0E8A5F',
    mintSubtle:         '#DFF5EB',
    rose:               '#B3424A',
    roseSubtle:         '#FBE7E9',
    hairline:           '#E7E5E4',
  },
  dark: {
    bgBase:             '#0A0A0B',
    bgSurface:          '#141416',
    bgSurfaceElevated:  '#1C1C1F',
    bgSurfaceMuted:     '#1C1C1F',
    borderSubtle:       '#27272A',
    borderStrong:       '#3F3F46',
    textPrimary:        '#FAFAF9',
    textSecondary:      '#A1A1AA',
    textTertiary:       '#71717A',
    accentPrimary:      '#3B82F6',
    accentPrimaryHover: '#2563EB',
    gain:               '#22C55E',
    gainSubtle:         '#14532D',
    loss:               '#EF4444',
    lossSubtle:         '#7F1D1D',
    neutral:            '#A1A1AA',
    warning:            '#F59E0B',
    warningSubtle:      '#78350F',
    danger:             '#DC2626',
    info:               '#60A5FA',
    // Bento (Design D)
    bgCanvas:           '#131314',
    bgTile:             '#1C1B1C',
    bgTileInset:        '#201F20',
    cta:                '#F1F0F4',
    ctaLabel:           '#131314',
    mint:               '#00E383',
    mintSubtle:         '#0E2A1D',
    rose:               '#FFB2B8',
    roseSubtle:         '#2A1416',
    hairline:           '#353436',
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
