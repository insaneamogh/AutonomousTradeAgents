/**
 * Platinum Glass — the DESKTOP design system.
 *
 * This is a DIFFERENT system from the mobile one in `DESIGN.md` (calm,
 * muted, Inter, accent-primary blue). Do not blend them: mobile keeps its
 * tokens in `tailwind.config.js`, desktop keeps its tokens here, and the
 * two trees never share a component.
 *
 * Source of truth: `STITCH_DESIGN_SYSTEM.md` §2 (colour), §3 (type),
 * §4 (spacing / radii / motion), §5 (glass + halos).
 *
 * Every value below is emitted once as a CSS custom property on the
 * desktop root. Components reference `var(--pg-*)` and never a raw hex —
 * that's the "tokens only" rule for this tree.
 */

/** Ordered score bands. The colour is a semantic TOKEN (not a hex), so a
 * band reads correctly in both themes. Used for aria text and for the few
 * places that still colour by band; the score ring itself is accent-only. */
export const SCORE_BANDS = [
  { min: 85, name: 'Very Bullish', hex: 'var(--pg-bull-text)' },
  { min: 55, name: 'Bullish', hex: 'var(--pg-bull-text)' },
  { min: 30, name: 'Neutral', hex: 'var(--pg-outline)' },
  { min: 15, name: 'Cautious', hex: 'var(--pg-warn)' },
  { min: 0, name: 'Bearish', hex: 'var(--pg-bear-text)' },
] as const;

/** Score (0–100) → its band hex. The single source for every bar/ring/pill. */
export function scoreHex(score: number): string {
  const clamped = Math.max(0, Math.min(100, score));
  for (const band of SCORE_BANDS) {
    if (clamped >= band.min) return band.hex;
  }
  return SCORE_BANDS[SCORE_BANDS.length - 1].hex;
}

/** Score (0–100) → its band name, for the pill label + aria text. */
export function scoreBand(score: number): string {
  const clamped = Math.max(0, Math.min(100, score));
  for (const band of SCORE_BANDS) {
    if (clamped >= band.min) return band.name;
  }
  return 'Bearish';
}

export type Regime = 'bull' | 'bear' | 'neutral';

/**
 * The whole Platinum Glass stylesheet.
 *
 * Written as raw CSS (rather than NativeWind classes) on purpose: the
 * Stitch spec mandates `backdrop-filter`, CSS grid, `:hover`,
 * `:focus-visible` rings and `@keyframes` shimmer — none of which
 * react-native-web's style system can express. Desktop is web-only, so
 * real CSS is available and costs the native bundle nothing.
 */
export const PLATINUM_CSS = `
/* ── Tokens: Aurora Calm (light) ──────────────────────────────────
   Warm ivory ground, ink text, ONE accent (clay). Colour carries meaning
   only: gain / loss / caution. Everything else is a tint of the ink. */
[data-pg-theme] {
  --pg-surface: #faf9f5;
  --pg-surface-lowest: #ffffff;
  --pg-surface-low: #f5f3ec;
  --pg-surface-container: #efede5;
  --pg-surface-high: #e8e5dc;
  --pg-surface-highest: #dfdbd0;

  --pg-on-surface: #171614;
  --pg-on-surface-variant: #45433e;
  --pg-outline: #6d6b65;
  --pg-outline-variant: #cfcbbf;

  --pg-primary: #171614;
  --pg-primary-container: #efede5;
  --pg-primary-fixed-dim: #45433e;

  /* The single accent. Fill = brand clay; text = a darker clay that passes
     4.5:1 on ivory; ink = what sits ON a clay fill. */
  --pg-accent: #d97757;
  --pg-accent-text: #a9502f;
  --pg-accent-ink: #1a1613;
  --pg-accent-wash: rgba(217, 119, 87, 0.13);

  --pg-bull: #2b7a4e;
  --pg-bull-text: #2b7a4e;
  --pg-bull-wash: rgba(43, 122, 78, 0.10);
  --pg-bear: #b4453b;
  --pg-bear-text: #b4453b;
  --pg-bear-wash: rgba(180, 69, 59, 0.09);

  /* Warning: elevated-risk / "requires attention". Same semantic slot as
     'warning' in the mobile DESIGN.md token set. */
  --pg-warn: #96620f;
  --pg-warn-wash: rgba(150, 98, 15, 0.11);

  --pg-error: #b4453b;
  --pg-error-container: #f6d9d4;

  /* Glass */
  --pg-card-bg: rgba(255, 255, 255, 0.66);
  --pg-card-border: rgba(23, 22, 20, 0.09);
  --pg-card-shadow: 0 1px 2px rgba(23, 22, 20, 0.04), 0 14px 30px -18px rgba(23, 22, 20, 0.14), inset 0 1px 0 rgba(255, 255, 255, 0.9);
  --pg-card-blur: blur(30px) saturate(130%);
  --pg-card-sheen: none;
  --pg-inset-bg: rgba(23, 22, 20, 0.035);

  --pg-header-bg: rgba(250, 249, 245, 0.6);
  --pg-sidebar-bg: rgba(255, 255, 255, 0.66);

  --pg-cta-bg: #d97757;
  --pg-cta-fg: #1a1613;

  --pg-halo-a: rgba(217, 119, 87, 0.22);
  --pg-halo-b: rgba(111, 134, 184, 0.18);
  --pg-halo-opacity: 1;

  --pg-track: rgba(23, 22, 20, 0.09);
  --pg-hover: rgba(23, 22, 20, 0.045);

  /* Spacing (§4.1) */
  --pg-xs: 4px;
  --pg-sm: 8px;
  --pg-md: 16px;
  --pg-gutter: 20px;
  --pg-lg: 24px;
  --pg-xl: 40px;

  /* Radii (§4.2). Cards 20, controls 12, chips full. */
  --pg-r-sm: 6px;
  --pg-r-lg: 10px;
  --pg-r-xl: 14px;
  --pg-r-2xl: 20px;
  --pg-r-3xl: 24px;

  /* Type: Inter for everything, Newsreader for display numerals + titles. */
  --pg-font-sans: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --pg-font-num: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --pg-font-serif: 'Newsreader', Georgia, 'Times New Roman', serif;

  --pg-header-h: 64px;
  --pg-sidebar-w: 80px;
}

/* ── Tokens: Aurora Calm (dark) ───────────────────────────────────── */
[data-pg-theme='dark'] {
  --pg-surface: #121211;
  --pg-surface-lowest: #0d0d0c;
  --pg-surface-low: #1a1a18;
  --pg-surface-container: #1e1d1b;
  --pg-surface-high: #2a2927;
  --pg-surface-highest: #34332f;

  --pg-on-surface: #f4f1ea;
  --pg-on-surface-variant: #c5c2ba;
  --pg-outline: #8f8c85;
  --pg-outline-variant: #45433f;

  --pg-primary: #f4f1ea;
  --pg-primary-container: #3a3935;
  --pg-primary-fixed-dim: #c5c2ba;

  --pg-accent: #d97757;
  --pg-accent-text: #e8947a;
  --pg-accent-ink: #1a1613;
  --pg-accent-wash: rgba(217, 119, 87, 0.16);

  --pg-bull: #7bc99a;
  --pg-bull-text: #7bc99a;
  --pg-bull-wash: rgba(123, 201, 154, 0.12);
  --pg-bear: #e4857c;
  --pg-bear-text: #e4857c;
  --pg-bear-wash: rgba(228, 133, 124, 0.12);

  --pg-warn: #d9a85b;
  --pg-warn-wash: rgba(217, 168, 91, 0.13);

  --pg-error: #e4857c;
  --pg-error-container: #5a2a26;

  --pg-card-bg: rgba(255, 255, 255, 0.045);
  --pg-card-border: rgba(255, 255, 255, 0.08);
  --pg-card-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
  --pg-card-blur: blur(30px) saturate(130%);
  --pg-card-sheen: none;
  --pg-inset-bg: rgba(255, 255, 255, 0.04);

  --pg-header-bg: rgba(18, 18, 17, 0.55);
  --pg-sidebar-bg: rgba(255, 255, 255, 0.045);

  --pg-cta-bg: #d97757;
  --pg-cta-fg: #1a1613;

  --pg-halo-a: rgba(217, 119, 87, 0.14);
  --pg-halo-b: rgba(111, 134, 184, 0.12);
  --pg-halo-opacity: 1;

  --pg-track: rgba(255, 255, 255, 0.12);
  --pg-hover: rgba(255, 255, 255, 0.06);
}

/* ── Root canvas + ambient mesh (§5.2) ────────────────────────────── */
.pg-root {
  position: fixed;
  inset: 0;
  overflow: hidden;
  isolation: isolate;
  background-color: var(--pg-surface);
  color: var(--pg-on-surface);
  font-family: var(--pg-font-sans);
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.pg-root::before,
.pg-root::after {
  content: '';
  position: absolute;
  border-radius: 9999px;
  filter: blur(120px);
  opacity: var(--pg-halo-opacity);
  pointer-events: none;
  z-index: 0;
  transition: background-color 900ms ease-in-out;
}
.pg-root::before {
  width: 70%;
  height: 70%;
  top: -20%;
  left: -10%;
  background-color: var(--pg-halo-a);
}
.pg-root::after {
  width: 60%;
  height: 60%;
  bottom: -20%;
  right: -10%;
  background-color: var(--pg-halo-b);
}
/* Regime-aware tint (§5.3) — ambient only, never tints the UI. */
.pg-root[data-regime='bull']::before { background-color: rgba(43, 122, 78, 0.16); }
.pg-root[data-regime='bear']::before { background-color: rgba(180, 69, 59, 0.14); }
[data-pg-theme='dark'] .pg-root[data-regime='bull']::before { background-color: rgba(123, 201, 154, 0.08); }
[data-pg-theme='dark'] .pg-root[data-regime='bear']::before { background-color: rgba(228, 133, 124, 0.09); }

/* ── App shell (§7.1) ─────────────────────────────────────────────── */
.pg-header {
  position: sticky;
  top: 0;
  z-index: 30;
  height: var(--pg-header-h);
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--pg-md);
  padding: 0 var(--pg-lg);
  background-color: var(--pg-header-bg);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  border-bottom: 1px solid var(--pg-card-border);
}
.pg-body { display: flex; flex: 1; min-height: 0; position: relative; z-index: 1; }
.pg-sidebar {
  width: var(--pg-sidebar-w);
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--pg-xs);
  margin: var(--pg-md) 0 var(--pg-md) var(--pg-md);
  padding: 14px 0;
  overflow-y: auto;
  background-color: var(--pg-sidebar-bg);
  backdrop-filter: var(--pg-card-blur);
  -webkit-backdrop-filter: var(--pg-card-blur);
  border: 1px solid var(--pg-card-border);
  border-radius: var(--pg-r-2xl);
  box-shadow: var(--pg-card-shadow);
}
.pg-main { flex: 1; min-width: 0; overflow-y: auto; }
.pg-main-inner {
  max-width: 1600px;
  margin: 0 auto;
  padding: var(--pg-lg) var(--pg-lg) 80px;
  display: flex;
  flex-direction: column;
  gap: var(--pg-gutter);
}

/* ── Bento grid (§7.2) ────────────────────────────────────────────── */
.pg-grid {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: var(--pg-gutter);
  align-items: stretch;
}
.pg-cell { display: flex; min-width: 0; }
/* The cell itself already opts out of min-width:auto so a grid track can
   shrink. Its CHILD does not: .pg-cell is a flex container, and a flex item
   defaults to min-width:auto, meaning it refuses to shrink below its own
   content width. A wide child (the broker table on Settings) therefore grew
   past the cell and painted over the card in the next grid column. Both
   levels have to opt out for the track to actually clamp.
   NOTE: this whole stylesheet is a TS template literal — no backticks. */
.pg-cell > * { min-width: 0; }
.pg-cell[data-span='3'] { grid-column: span 3 / span 3; }
.pg-cell[data-span='4'] { grid-column: span 4 / span 4; }
.pg-cell[data-span='5'] { grid-column: span 5 / span 5; }
.pg-cell[data-span='6'] { grid-column: span 6 / span 6; }
.pg-cell[data-span='7'] { grid-column: span 7 / span 7; }
.pg-cell[data-span='8'] { grid-column: span 8 / span 8; }
.pg-cell[data-span='12'] { grid-column: span 12 / span 12; }

/* Laptop reflow (§11): the bento halves before it ever squeezes. */
@media (max-width: 1279px) {
  .pg-cell[data-span='3'],
  .pg-cell[data-span='4'] { grid-column: span 6 / span 6; }
  .pg-cell[data-span='5'],
  .pg-cell[data-span='7'],
  .pg-cell[data-span='8'] { grid-column: span 12 / span 12; }
}

/* ── Glass card (§5.1 / §6.1) ─────────────────────────────────────── */
.pg-card {
  position: relative;
  border-radius: var(--pg-r-2xl);
  background-color: var(--pg-card-bg);
  background-image: var(--pg-card-sheen);
  border: 1px solid var(--pg-card-border);
  box-shadow: var(--pg-card-shadow);
  backdrop-filter: var(--pg-card-blur);
  -webkit-backdrop-filter: var(--pg-card-blur);
  padding: var(--pg-gutter);
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}
.pg-card--dense { padding: 14px var(--pg-md); gap: 10px; }
.pg-card--flush { padding: 0; }
.pg-card--hero { padding: var(--pg-lg); }
/* A card that needs you: slightly brighter surface and a firmer edge. */
.pg-card--lift { background-color: var(--pg-hover); border-color: var(--pg-outline-variant); padding: var(--pg-lg); }
.pg-inset {
  border-radius: var(--pg-r-xl);
  background-color: var(--pg-inset-bg);
  border: 1px solid var(--pg-card-border);
  padding: 12px 14px;
  min-width: 0;
}

/* ── Typography (§3) ──────────────────────────────────────────────── */
.pg-root h1, .pg-root h2, .pg-root h3, .pg-root p { margin: 0; }
/* Section label: sentence case, quiet. (Was uppercase mono-ish caps.) */
.label-caps {
  font-size: 12px;
  line-height: 16px;
  font-weight: 500;
  letter-spacing: 0;
  font-family: var(--pg-font-sans);
  color: var(--pg-outline);
}
.pg-h1, .pg-h2, .pg-h3 { font-family: var(--pg-font-serif); font-weight: 400; letter-spacing: -0.025em; }
.pg-h1 { font-size: 48px; line-height: 1.1; }
.pg-h2 { font-size: 32px; line-height: 36px; }
.pg-h3 { font-size: 24px; line-height: 28px; }
.pg-body-lg { font-size: 18px; line-height: 1.6; }
.pg-body-md { font-size: 16px; line-height: 1.6; }
.pg-body-sm { font-size: 14px; line-height: 1.55; color: var(--pg-on-surface-variant); }
.pg-caption { font-size: 12.5px; line-height: 1.5; color: var(--pg-outline); }
.pg-num { font-family: var(--pg-font-num); font-variant-numeric: tabular-nums lining-nums; letter-spacing: -0.01em; }
.pg-serif { font-family: var(--pg-font-serif); font-weight: 400; letter-spacing: -0.025em; font-variant-numeric: tabular-nums lining-nums; }
.pg-num-hero { font-family: var(--pg-font-serif); font-variant-numeric: tabular-nums lining-nums; font-size: 64px; line-height: 64px; font-weight: 400; letter-spacing: -0.03em; }
.pg-money { display: inline-flex; align-items: baseline; }
.pg-money > s { text-decoration: none; font-size: 0.5em; color: var(--pg-outline); margin-left: 1px; }
.pg-muted { color: var(--pg-on-surface-variant); }
.pg-dim { color: var(--pg-outline); }
.pg-bull { color: var(--pg-bull-text); }
.pg-bear { color: var(--pg-bear-text); }
.pg-truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── Pills (§6.4) ─────────────────────────────────────────────────── */
.pg-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 9999px;
  padding: 4px 10px;
  font-size: 12px;
  line-height: 16px;
  font-weight: 500;
  font-family: var(--pg-font-num);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  background-color: var(--pg-inset-bg);
  color: var(--pg-on-surface-variant);
  border: 1px solid transparent;
}
.pg-pill--bull { background-color: var(--pg-bull-wash); color: var(--pg-bull-text); }
.pg-pill--bear { background-color: var(--pg-bear-wash); color: var(--pg-bear-text); }
.pg-pill--warn { background-color: var(--pg-warn-wash); color: var(--pg-warn); }
.pg-pill--glow { box-shadow: none; }

/* A '.pg-pill' that is also a real <button> — same shape/type as a
   read-only pill, but interactive (used by the auto-approve mode
   control). Kept separate from '.pg-pill' itself so every existing
   non-interactive pill stays exactly as it was. */
.pg-pill-btn {
  min-height: 44px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: opacity 150ms ease, transform 120ms ease;
}
.pg-pill-btn:hover:not(:disabled) { opacity: 0.85; }
.pg-pill-btn:active:not(:disabled) { transform: scale(0.97); }
.pg-pill-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* ── Buttons (§9) ─────────────────────────────────────────────────── */
.pg-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 20px;
  border-radius: var(--pg-r-xl);
  border: 1px solid transparent;
  font-family: var(--pg-font-sans);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 150ms ease, background-color 150ms ease, border-color 150ms ease, transform 120ms ease;
  background: transparent;
  color: var(--pg-on-surface);
}
.pg-btn:active { transform: scale(0.98); }
.pg-btn:disabled { opacity: 0.4; cursor: not-allowed; }
/* Clay. The one accent. NEVER green — green is reserved for positive P&L. */
.pg-btn-primary { background: var(--pg-cta-bg); color: var(--pg-cta-fg); }
.pg-btn-primary:hover:not(:disabled) { opacity: 0.92; }
.pg-btn-secondary { border-color: var(--pg-card-border); background-color: var(--pg-inset-bg); color: var(--pg-on-surface); }
.pg-btn-secondary:hover:not(:disabled) { background-color: var(--pg-hover); }
.pg-btn-ghost { color: var(--pg-on-surface-variant); padding: 0 12px; }
.pg-btn-ghost:hover:not(:disabled) { background-color: var(--pg-hover); }
.pg-btn-sm { min-height: 34px; font-size: 13px; padding: 0 14px; }
.pg-icon-btn {
  width: 36px; height: 36px; min-height: 36px; padding: 0;
  border-radius: 9999px; display: inline-flex; align-items: center; justify-content: center;
  background: var(--pg-inset-bg); border: 1px solid var(--pg-card-border); cursor: pointer;
  color: var(--pg-on-surface-variant); transition: background-color 150ms ease;
}
.pg-icon-btn:hover { background-color: var(--pg-hover); }

/* ── Sidebar nav: slim icon rail (labels live in aria-label + title) ── */
.pg-sidebar-link {
  display: flex; align-items: center; gap: 12px;
  min-height: 44px; padding: 0 16px;
  border-radius: var(--pg-r-xl); border: 1px solid transparent;
  background: transparent; cursor: pointer; width: 100%;
  font-family: var(--pg-font-sans); font-size: 14px; font-weight: 500;
  color: var(--pg-on-surface-variant); text-align: left;
  transition: background-color 200ms ease, color 200ms ease;
}
.pg-sidebar-link:hover { background-color: var(--pg-hover); color: var(--pg-on-surface); }
.pg-sidebar-link[aria-current='page'] {
  background-color: var(--pg-track);
  color: var(--pg-on-surface);
  font-weight: 600;
}
.pg-sidebar .pg-sidebar-link {
  position: relative; width: 48px; min-height: 48px; padding: 0; justify-content: center;
  color: var(--pg-outline);
}
.pg-sidebar .pg-sidebar-link:hover { color: var(--pg-on-surface); }
.pg-sidebar .pg-sidebar-link[aria-current='page'] { color: var(--pg-on-surface); }
.pg-nav-label { display: none; }
.pg-sheet .pg-nav-label { display: inline; }
.pg-rail-mark { width: 36px; height: 36px; border-radius: 11px; background: var(--pg-accent); margin-bottom: 14px; flex: none; }
.pg-sidebar-badge {
  margin-left: auto; font-family: var(--pg-font-num); font-size: 11px; font-weight: 700;
  min-width: 20px; height: 20px; padding: 0 6px; border-radius: 9999px;
  display: inline-flex; align-items: center; justify-content: center;
  background-color: var(--pg-accent); color: var(--pg-accent-ink);
}
.pg-sidebar .pg-sidebar-badge {
  position: absolute; top: 4px; right: 2px; min-width: 16px; height: 16px; padding: 0 4px; font-size: 10px;
}

/* ── Tables ───────────────────────────────────────────────────────── */
/* Empty state — a settled "nothing here", distinct from a shimmer.
   Shimmering at an empty result reads as a hung request. */
.pg-empty { padding: 28px 8px 24px; text-align: center; }
.pg-empty-title {
  font-size: 16px; font-weight: 500; letter-spacing: -0.01em;
  color: var(--pg-on-surface); margin: 0 0 6px;
}
.pg-empty-body {
  font-size: 13px; line-height: 1.6; color: var(--pg-outline);
  margin: 0 auto; max-width: 46ch;
}
/* Ticker typeahead — floats over the card, so it needs its own
   surface rather than inheriting the transparent glass fill. */
.pg-typeahead {
  position: absolute; top: calc(100% + 6px); left: 0; right: 0; z-index: 40;
  margin: 0; padding: 4px; list-style: none;
  max-height: 268px; overflow-y: auto;
  background: var(--pg-surface-low);
  border: 1px solid var(--pg-card-border);
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
}
.pg-typeahead-row {
  display: flex; align-items: baseline; gap: 10px;
  width: 100%; padding: 9px 10px;
  background: transparent; border: 0; border-radius: 8px;
  text-align: left; cursor: pointer; color: inherit;
}
.pg-typeahead-row.is-active { background: var(--pg-surface-high); }
.pg-typeahead-row:focus-visible { outline: 2px solid var(--pg-primary); outline-offset: -2px; }
.pg-typeahead-sym {
  font-family: var(--pg-font-num);
  font-size: 13px; font-weight: 600; letter-spacing: 0.02em;
  color: var(--pg-on-surface); flex: 0 0 auto; min-width: 52px;
}
.pg-typeahead-name {
  font-size: 12px; color: var(--pg-outline);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pg-table { width: 100%; border-collapse: collapse; }
.pg-table th {
  text-align: left; padding: 0 10px 10px;
  font-size: 12px; line-height: 16px; font-weight: 500;
  font-family: var(--pg-font-sans); color: var(--pg-outline);
  border-bottom: 1px solid var(--pg-card-border);
}
.pg-table td { padding: 12px 10px; border-bottom: 1px solid var(--pg-card-border); font-size: 14px; vertical-align: middle; }
.pg-table tr:last-child td { border-bottom: none; }
.pg-row-btn { cursor: pointer; transition: background-color 150ms ease; }
.pg-row-btn:hover td { background-color: var(--pg-hover); }
.pg-num-right { text-align: right; font-family: var(--pg-font-num); font-variant-numeric: tabular-nums; }

/* ── Stop → target range track ───────────────────────────────────── */
.pg-range { position: relative; height: 4px; border-radius: 2px; background-color: var(--pg-track); margin: 8px 0 6px; }
.pg-range > u { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 2px; background-color: var(--pg-on-surface); opacity: 0.7; text-decoration: none; }
.pg-range > i { position: absolute; top: -5px; width: 14px; height: 14px; margin-left: -7px; border-radius: 9999px; background-color: var(--pg-on-surface); box-shadow: 0 0 0 3px var(--pg-surface); }
.pg-range-labels { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; line-height: 16px; color: var(--pg-outline); }

/* ── Bars ─────────────────────────────────────────────────────────── */
.pg-bar { height: 6px; border-radius: 9999px; background-color: var(--pg-track); overflow: hidden; }
.pg-bar > i { display: block; height: 100%; border-radius: 9999px; transition: width 700ms ease; background-color: var(--pg-accent); }

/* ── Skeleton shimmer (§8.1) ──────────────────────────────────────── */
@keyframes pg-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
.pg-skel {
  border-radius: var(--pg-r-lg);
  background-image: linear-gradient(90deg, var(--pg-surface-high) 0%, var(--pg-surface-highest) 50%, var(--pg-surface-high) 100%);
  background-size: 200% 100%;
  animation: pg-shimmer 2s infinite linear;
}
@keyframes pg-ping { 75%, 100% { transform: scale(1.6); opacity: 0; } }
.pg-ping { animation: pg-ping 2s cubic-bezier(0, 0, 0.2, 1) infinite; }
@keyframes pg-fade-up { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.pg-fade-up { animation: pg-fade-up 350ms ease-out both; }
@keyframes pg-pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
.pg-live-dot { width: 8px; height: 8px; border-radius: 9999px; background-color: var(--pg-bull); animation: pg-pulse-dot 1.4s ease-in-out infinite; flex: none; }
@keyframes pg-spin { to { transform: rotate(360deg); } }
.pg-spin { animation: pg-spin 900ms linear infinite; }

@media (prefers-reduced-motion: reduce) {
  .pg-skel, .pg-ping, .pg-fade-up, .pg-live-dot, .pg-spin, .pg-dots i, .pg-sheet { animation: none !important; }
  .pg-tile--tap, .pg-seg-btn, .pg-bottom-tab { transition: none; }
  .pg-root::before, .pg-root::after { transition: none; }
}

/* ── Focus rings (§12) ────────────────────────────────────────────── */
.pg-root :focus-visible {
  outline: 2px solid var(--pg-accent);
  outline-offset: 2px;
  border-radius: var(--pg-r-lg);
}

/* ── Inputs (§9) ──────────────────────────────────────────────────── */
.pg-input {
  min-height: 40px; padding: 0 16px; border-radius: var(--pg-r-xl);
  background-color: var(--pg-inset-bg); border: 1px solid var(--pg-card-border);
  color: var(--pg-on-surface); font-family: var(--pg-font-sans); font-size: 14px;
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
}
.pg-input::placeholder { color: var(--pg-outline); }

/* ── Home tiles ───────────────────────────────────────────────────── */
.pg-tiles {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--pg-gutter);
  align-items: stretch;
}
.pg-tile { gap: 10px; justify-content: flex-start; padding: 20px; }
.pg-tile[data-span='1'] { grid-column: span 2 / span 2; }
.pg-tile[data-span='2'] { grid-column: span 3 / span 3; }
.pg-tile--tap { cursor: pointer; transition: transform 120ms ease, border-color 150ms ease; }
.pg-tile--tap:hover { border-color: var(--pg-outline-variant); }
.pg-tile--tap:active { transform: scale(0.985); }
.pg-tile-num {
  font-family: var(--pg-font-serif);
  font-size: 44px; line-height: 1; font-weight: 400; letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums lining-nums; overflow-wrap: anywhere;
}
.pg-tile-word {
  font-family: var(--pg-font-serif); font-size: 48px; line-height: 1;
  font-weight: 400; letter-spacing: -0.02em;
}
/* Dot grid: one dot per count. */
.pg-dots { display: flex; flex-wrap: wrap; gap: 6px; align-content: flex-start; min-height: 20px; }
.pg-dots i, .pg-legend i {
  display: inline-block; width: 10px; height: 10px; border-radius: 9999px;
  background-color: var(--pg-track); flex: none;
}
.pg-dots--sm i { width: 12px; height: 12px; }
.pg-dots i[data-kind='avoided'], .pg-legend i[data-kind='avoided'], .pg-dots i[data-kind='ok'] { background-color: var(--pg-accent); }
.pg-dots i[data-kind='blocked'], .pg-legend i[data-kind='blocked'], .pg-dots i[data-kind='warn'] { background-color: var(--pg-warn); }
.pg-dots i[data-kind='fail'] { background-color: var(--pg-bear-text); }
.pg-dots i[data-kind='pending'], .pg-legend i[data-kind='pending'] { background-color: var(--pg-outline-variant); }
@keyframes pg-dot-in { from { opacity: 0; transform: scale(0.4); } to { opacity: 1; transform: none; } }
.pg-dots i { animation: pg-dot-in 260ms ease-out both; animation-delay: calc(var(--i, 0) * 8ms); }
.pg-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--pg-outline); }
.pg-legend span { display: inline-flex; align-items: center; gap: 6px; }
.pg-ledger-totals { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.pg-ledger-totals > div { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.pg-ledger-totals .pg-tile-num { font-size: 28px; }
/* Segmented switch. */
.pg-seg {
  display: inline-flex; align-self: flex-start; padding: 3px; gap: 2px;
  border-radius: var(--pg-r-xl); background-color: var(--pg-inset-bg); border: 1px solid var(--pg-card-border);
}
.pg-seg-btn {
  min-height: 44px; min-width: 64px; padding: 0 16px; border-radius: var(--pg-r-lg); border: 0;
  background: transparent; color: var(--pg-on-surface-variant); cursor: pointer;
  font-family: var(--pg-font-sans); font-size: 13px; font-weight: 600;
  transition: background-color 180ms ease, color 180ms ease;
}
.pg-seg-btn[aria-pressed='true'] { background-color: var(--pg-track); color: var(--pg-on-surface); }
/* Autonomy checklist. */
.pg-checklist { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-direction: column; gap: 10px; cursor: default; }
.pg-checklist li { display: flex; flex-direction: column; gap: 2px; padding-top: 10px; border-top: 1px solid var(--pg-card-border); }
.pg-checklist li[data-ok='false'] .pg-checklist-label { color: var(--pg-bear-text); }
.pg-checklist-label { font-size: 14px; font-weight: 600; }
.pg-checklist-fix { font-size: 13px; color: var(--pg-on-surface); background-color: var(--pg-inset-bg); border-radius: var(--pg-r-lg); padding: 8px 10px; margin-top: 4px; }

/* ── Phone shell: bottom tab bar, compact header, sheet ────────────── */
.pg-compact-title { font-family: var(--pg-font-serif); font-size: 22px; font-weight: 400; letter-spacing: -0.02em; line-height: 1.2; min-width: 0; }
.pg-bottom-nav, .pg-sheet-wrap { display: none; }

@media (max-width: 767px) {
  .pg-root { --pg-header-h: 56px; --pg-nav-h: 66px; --pg-gutter: 14px; }
  .pg-header {
    height: auto; min-height: var(--pg-header-h);
    padding: env(safe-area-inset-top) max(16px, env(safe-area-inset-right)) 0 max(16px, env(safe-area-inset-left));
    gap: 10px;
  }
  .pg-main { overflow-x: hidden; -webkit-overflow-scrolling: touch; }
  .pg-main-inner {
    padding: 16px max(16px, env(safe-area-inset-right)) calc(var(--pg-nav-h) + env(safe-area-inset-bottom) + 40px) max(16px, env(safe-area-inset-left));
    max-width: 100%;
  }
  .pg-sidebar { display: none; }

  /* Floating glass tab bar. */
  .pg-bottom-nav {
    display: flex; position: fixed; left: 12px; right: 12px; z-index: 40;
    bottom: calc(env(safe-area-inset-bottom) + 14px);
    border-radius: 24px;
    background-color: var(--pg-header-bg);
    backdrop-filter: blur(30px) saturate(140%); -webkit-backdrop-filter: blur(30px) saturate(140%);
    border: 1px solid var(--pg-card-border);
    box-shadow: var(--pg-card-shadow);
  }
  .pg-bottom-tab {
    flex: 1; min-width: 0; min-height: var(--pg-nav-h); padding: 6px 2px;
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px;
    background: transparent; border: 0; cursor: pointer; color: var(--pg-outline);
    font-family: var(--pg-font-sans); font-size: 12px; font-weight: 500;
    transition: color 150ms ease;
  }
  .pg-bottom-tab[aria-current='page'] { color: var(--pg-on-surface); font-weight: 600; }
  .pg-bottom-tab:active { transform: scale(0.95); }
  .pg-bottom-tab-icon { position: relative; display: inline-flex; }
  .pg-bottom-tab-badge {
    position: absolute; top: -6px; right: -12px; min-width: 16px; height: 16px; padding: 0 4px;
    border-radius: 9999px; font-family: var(--pg-font-num); font-size: 10px; font-weight: 700;
    display: inline-flex; align-items: center; justify-content: center;
    background-color: var(--pg-accent); color: var(--pg-accent-ink);
  }

  .pg-sheet-wrap { display: block; position: fixed; inset: 0; z-index: 60; }
  .pg-sheet-backdrop { position: absolute; inset: 0; width: 100%; border: 0; background: rgba(0, 0, 0, 0.45); cursor: default; }
  .pg-sheet {
    position: absolute; left: 0; right: 0; bottom: 0;
    display: flex; flex-direction: column; gap: 4px;
    padding: 20px 16px calc(env(safe-area-inset-bottom) + 16px);
    background-color: var(--pg-surface-low);
    border-top: 1px solid var(--pg-card-border); border-radius: var(--pg-r-3xl) var(--pg-r-3xl) 0 0;
    animation: pg-sheet-up 220ms ease-out both;
  }
  @keyframes pg-sheet-up { from { transform: translateY(24px); opacity: 0; } to { transform: none; opacity: 1; } }

  /* One column. */
  .pg-grid { grid-template-columns: minmax(0, 1fr); }
  .pg-cell[data-span] { grid-column: 1 / -1; }
  .pg-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .pg-tile[data-span='1'] { grid-column: span 1 / span 1; }
  .pg-tile[data-span='2'] { grid-column: 1 / -1; }
  /* Two tiles per row at phone width: a six-figure equity must not wrap
     mid-number, so the numeral scales with the viewport and stays on one line. */
  .pg-tile-num { font-size: clamp(22px, 7vw, 36px); white-space: nowrap; }
  .pg-tile-word { font-size: 44px; }
  /* Screens lay out small inline grids (3/4/5 stat columns): reflow them. */
  .pg-root [style*='grid-template-columns'] { grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)) !important; }
  .pg-ledger-totals { grid-template-columns: 1fr 1fr !important; }
  .pg-num-hero { font-size: 44px; }
  .pg-h1 { font-size: 34px; }
  .pg-h2 { font-size: 26px; }

  /* Tables scroll inside their card rather than the page. */
  .pg-card:has(> .pg-table), .pg-card > .pg-table { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .pg-table { display: block; overflow-x: auto; max-width: 100%; -webkit-overflow-scrolling: touch; }
  .pg-table th, .pg-table td { white-space: nowrap; }

  /* 44px touch targets. */
  .pg-icon-btn { width: 44px; height: 44px; min-height: 44px; }
  .pg-btn-sm, .pg-input, .pg-sidebar-link, .pg-pill-btn { min-height: 44px; }
  .pg-row-btn td { padding-top: 14px; padding-bottom: 14px; }
  .pg-input { font-size: 16px; } /* iOS zooms the page on focus below 16px */
}

/* ── Scrollbars ───────────────────────────────────────────────────── */
/* '.pg-scroll' is the same thin/rounded treatment, opted into by any
   OTHER internally-scrolling panel (e.g. the council Stream log) that
   isn't '.pg-main'/'.pg-sidebar' — without it such a panel falls back to
   the browser's default scrollbar, which reads as off-system next to
   this glass design. */
.pg-main::-webkit-scrollbar, .pg-sidebar::-webkit-scrollbar, .pg-scroll::-webkit-scrollbar { width: 10px; }
.pg-main::-webkit-scrollbar-thumb, .pg-sidebar::-webkit-scrollbar-thumb, .pg-scroll::-webkit-scrollbar-thumb {
  background-color: var(--pg-outline-variant); border-radius: 9999px;
  border: 3px solid transparent; background-clip: content-box;
}
.pg-main::-webkit-scrollbar-track, .pg-sidebar::-webkit-scrollbar-track, .pg-scroll::-webkit-scrollbar-track { background: transparent; }
`;
