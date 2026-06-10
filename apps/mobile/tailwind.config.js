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
  darkMode: 'media',
  theme: {
    extend: {
      colors: {
        // ── Surfaces ──────────────────────────────────────────────
        'bg-base':                 '#FAFAF9',
        'bg-base-dark':            '#0A0A0B',
        'bg-surface':              '#FFFFFF',
        'bg-surface-dark':         '#141416',
        'bg-surface-elevated':     '#FFFFFF',
        'bg-surface-elevated-dark':'#1C1C1F',
        'bg-surface-muted':        '#F5F5F4',
        'bg-surface-muted-dark':   '#1C1C1F',

        // ── Borders ───────────────────────────────────────────────
        'border-subtle':         '#E7E5E4',
        'border-subtle-dark':    '#27272A',
        'border-strong':         '#D6D3D1',
        'border-strong-dark':    '#3F3F46',

        // ── Text ──────────────────────────────────────────────────
        'text-primary':          '#0C0A09',
        'text-primary-dark':     '#FAFAF9',
        'text-secondary':        '#57534E',
        'text-secondary-dark':   '#A1A1AA',
        'text-tertiary':         '#A8A29E',
        'text-tertiary-dark':    '#71717A',

        // ── Accent ────────────────────────────────────────────────
        'accent-primary':              '#1E40AF',
        'accent-primary-dark':         '#3B82F6',
        'accent-primary-hover':        '#1E3A8A',
        'accent-primary-hover-dark':   '#2563EB',

        // ── Bento (Design D) ──────────────────────────────────────
        // Editorial-bento skin: canvas behind everything, tiles for
        // every content block, one platinum/ink CTA per screen.
        'bg-canvas':             '#FAFAF9',
        'bg-canvas-dark':        '#131314',
        'bg-tile':               '#FFFFFF',
        'bg-tile-dark':          '#1C1B1C',
        'bg-tile-inset':         '#F4F4F3',
        'bg-tile-inset-dark':    '#201F20',
        'cta':                   '#1B1B1F',
        'cta-dark':              '#F1F0F4',
        'cta-label':             '#FAFAF9',
        'cta-label-dark':        '#131314',
        'mint':                  '#0E8A5F',
        'mint-dark':             '#00E383',
        'mint-subtle':           '#DFF5EB',
        'mint-subtle-dark':      '#0E2A1D',
        'rose':                  '#B3424A',
        'rose-dark':             '#FFB2B8',
        'rose-subtle':           '#FBE7E9',
        'rose-subtle-dark':      '#2A1416',
        'hairline':              '#E7E5E4',
        'hairline-dark':         '#353436',

        // ── Trading semantics ─────────────────────────────────────
        'gain':                  '#15803D',
        'gain-dark':             '#22C55E',
        'gain-subtle':           '#DCFCE7',
        'gain-subtle-dark':      '#14532D',
        'loss':                  '#B91C1C',
        'loss-dark':             '#EF4444',
        'loss-subtle':           '#FEE2E2',
        'loss-subtle-dark':      '#7F1D1D',
        'neutral':               '#57534E',
        'neutral-dark':          '#A1A1AA',
        'warning':               '#B45309',
        'warning-dark':          '#F59E0B',
        'warning-subtle':        '#FEF3C7',
        'warning-subtle-dark':   '#78350F',
        'danger':                '#991B1B',
        'danger-dark':           '#DC2626',
        'info':                  '#1E40AF',
        'info-dark':             '#60A5FA',
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
