/** @type {import('tailwindcss').Config} */
//
// Tailwind / NativeWind mirror of `packages/ui/src/tokens.ts`. The mobile app
// uses the `dark:` variant prefix to pick the right token under
// `useColorScheme()`. Both LIGHT and DARK tokens are flat-named here:
//
//   bg-bg-surface          → light surface  (#FFFFFF)
//   dark:bg-bg-surface-dark → dark surface  (#141416)
//
// When a token is added to packages/ui/src/tokens.ts, mirror BOTH the
// light and the dark entry here.
module.exports = {
  content: [
    './app/**/*.{ts,tsx}',
    './src/**/*.{ts,tsx}',
    '../../packages/ui/src/**/*.{ts,tsx}',
  ],
  presets: [require('nativewind/preset')],
  // 'class' lets the in-app theme control (Settings › Appearance) override the
  // OS scheme via NativeWind's colorScheme.set(). 'system' preference falls
  // back to the device appearance. See src/stores/themeStore.ts.
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Aurora Calm: warm ivory / warm slate, ONE accent (clay).
        // Every value here mirrors `packages/ui/src/tokens.ts`.

        // ── Surfaces ──────────────────────────────────────────────
        'bg-base':                 '#FAF9F5',
        'bg-base-dark':            '#121211',
        'bg-surface':              '#FFFFFF',
        'bg-surface-dark':         '#1A1A18',
        'bg-surface-elevated':     '#FFFFFF',
        'bg-surface-elevated-dark':'#201F1D',
        'bg-surface-muted':        '#F5F3EC',
        'bg-surface-muted-dark':   '#201F1D',

        // ── Borders ───────────────────────────────────────────────
        'border-subtle':         '#E8E5DC',
        'border-subtle-dark':    '#2E2D2A',
        'border-strong':         '#CFCBBF',
        'border-strong-dark':    '#45433F',

        // ── Text ──────────────────────────────────────────────────
        'text-primary':          '#171614',
        'text-primary-dark':     '#F4F1EA',
        'text-secondary':        '#45433E',
        'text-secondary-dark':   '#C5C2BA',
        'text-tertiary':         '#6D6B65',
        'text-tertiary-dark':    '#8F8C85',

        // ── Accent ────────────────────────────────────────────────
        // `accent-primary` is a FILL; `accent-text` is accent copy on the
        // page (4.5:1); `on-accent` is a label sitting ON a clay fill.
        'accent-primary':              '#D97757',
        'accent-primary-dark':         '#D97757',
        'accent-primary-hover':        '#C8643F',
        'accent-primary-hover-dark':   '#E38A6D',
        'accent-text':                 '#A9502F',
        'accent-text-dark':            '#E8947A',
        'on-accent':                   '#1A1613',
        'on-accent-dark':              '#1A1613',

        // ── Bento (Design D) ──────────────────────────────────────
        // Canvas behind everything, tiles for every content block, one
        // clay CTA per screen.
        'bg-canvas':             '#FAF9F5',
        'bg-canvas-dark':        '#121211',
        'bg-tile':               '#FFFFFF',
        'bg-tile-dark':          '#1C1B19',
        'bg-tile-inset':         '#F3F1EA',
        'bg-tile-inset-dark':    '#242320',
        'cta':                   '#D97757',
        'cta-dark':              '#D97757',
        'cta-label':             '#1A1613',
        'cta-label-dark':        '#1A1613',
        'mint':                  '#2B7A4E',
        'mint-dark':             '#7BC99A',
        'mint-subtle':           '#E3F1E8',
        'mint-subtle-dark':      '#1B2A21',
        'rose':                  '#B4453B',
        'rose-dark':             '#E4857C',
        'rose-subtle':           '#F9E6E3',
        'rose-subtle-dark':      '#2D1B19',
        'hairline':              '#E8E5DC',
        'hairline-dark':         '#2E2D2A',

        // ── Trading semantics ─────────────────────────────────────
        'gain':                  '#2B7A4E',
        'gain-dark':             '#7BC99A',
        'gain-subtle':           '#E3F1E8',
        'gain-subtle-dark':      '#1B2A21',
        'loss':                  '#B4453B',
        'loss-dark':             '#E4857C',
        'loss-subtle':           '#F9E6E3',
        'loss-subtle-dark':      '#2D1B19',
        'neutral':               '#45433E',
        'neutral-dark':          '#C5C2BA',
        'warning':               '#96620F',
        'warning-dark':          '#D9A85B',
        'warning-subtle':        '#F6ECD6',
        'warning-subtle-dark':   '#332914',
        'danger':                '#9A2F26',
        'danger-dark':           '#E4857C',
        'info':                  '#3C6FA8',
        'info-dark':             '#8FB1DB',

        // ── Desktop tree (web) ────────────────────────────────────
        // Mirror of `src/desktop/theme.ts`, which consumes them as
        // `--pg-*` CSS custom properties (plain CSS is required for
        // backdrop-filter, grid and focus-visible). Registered here so the
        // config stays the one token registry: NOTHING in either tree may
        // hard-code one of these hexes.
        // `-pgd-` = dark, `-pgl-` = light.
        'pgd-surface':           '#121211',
        'pgd-surface-lowest':    '#0D0D0C',
        'pgd-surface-low':       '#1A1A18',
        'pgd-surface-high':      '#2A2927',
        'pgd-on-surface':        '#F4F1EA',
        'pgd-outline':           '#8F8C85',
        'pgd-primary':           '#F4F1EA',
        'pgd-bull':              '#7BC99A',
        'pgd-bear':              '#E4857C',
        'pgd-error':             '#E4857C',
        'pgl-surface':           '#FAF9F5',
        'pgl-surface-lowest':    '#FFFFFF',
        'pgl-on-surface':        '#171614',
        'pgl-outline':           '#6D6B65',
        'pgl-primary':           '#171614',
        'pgl-bull':              '#2B7A4E',
        'pgl-bear':              '#B4453B',
        'pgl-error':             '#B4453B',
        // 5-band score palette. Identical in light and dark by design, so
        // each is a mid-tone that reads on both grounds.
        'band-very-bullish':     '#4FA776',
        'band-bullish':          '#3E8F63',
        'band-neutral':          '#8F8C85',
        'band-cautious':         '#D9A85B',
        'band-bearish':          '#B4453B',
      },
      borderRadius: {
        sm: '6px',
        md: '10px',
        lg: '14px',
        xl: '20px',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['Inter', 'system-ui', 'monospace'],
      },
    },
  },
  plugins: [],
};
