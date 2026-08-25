# Platinum Glass — Desktop Design System

**Scope:** Desktop web only (`Platform.OS === 'web'`, viewport ≥ 1024px, authenticated).
**Companion doc:** [`DESIGN.md`](DESIGN.md) — the mobile design system (calm, muted, Inter,
`accent-primary` blue). The two are deliberately **not blended**: mobile keeps its tokens in
[`apps/mobile/tailwind.config.js`](apps/mobile/tailwind.config.js), desktop keeps its tokens in
[`apps/mobile/src/desktop/theme.ts`](apps/mobile/src/desktop/theme.ts), and no component is
shared between the two trees.

This file exists because the desktop code's own docstrings cite "STITCH_DESIGN_SYSTEM.md §N"
as their source of truth (see `theme.ts`, `DesktopShell.tsx`) — this is that file, checked into
the repo so the citation resolves to something real. Every value below was verified against the
live implementation, not transcribed from a spec that predates the code.

---

## 1. Why a second design system

The product is one Expo app (`apps/mobile`) that also runs on web via `react-native-web`. Below
1024px — phones, and a browser at phone width — the app is the phone experience end to end.
At ≥1024px on web, with a live session, [`DesktopShell`](apps/mobile/src/components/DesktopShell.tsx)
**replaces** the router subtree with [`DesktopApp`](apps/mobile/src/desktop/DesktopApp.tsx) — an
institutional, information-dense "trading desk" surface. Native and narrow-web paths never
evaluate the desktop module (it's behind a guarded `require`), so the phone build is unaffected
by anything in this document.

Anchor principles (carried over from the original Stitch spec, verified against the code):

1. **Data first.** Numerals dominate (Space Grotesk, tabular). Chrome is quiet.
2. **Glass surfaces.** Cards feel layered — `backdrop-filter: blur(24px) saturate(180%)` in dark,
   a flat white card with soft shadow in light.
3. **Semantic color, not decorative.** Mint = bullish, rose-gold = bearish, platinum = neutral.
4. **Regime-aware ambience.** The canvas carries two blurred halos that tint mint/rose based on
   `body`-level regime state (`useRegime()`), never the foreground UI itself.
5. **No "No data available" text.** Loading and empty states render a shimmer skeleton
   (`Skel` / `SkelRows`) or a designed empty state with a CTA — never bare text.
6. **Approve is never green.** Green (`--pg-bull`) is reserved for fills and positive P&L, matching
   the same rule in the mobile `DESIGN.md`.

---

## 2. Color tokens

All tokens are CSS custom properties (`--pg-*`) emitted once by `PLATINUM_CSS` in `theme.ts` and
scoped under `[data-pg-theme]` (light) / `[data-pg-theme='dark']` (dark). Components reference
`var(--pg-*)` — raw hex in a component file means the token registry is out of date, not that a
new color was "needed."

### Surfaces

| Token | Light | Dark |
|---|---|---|
| `--pg-surface` | `#e8edf5` | `#131314` |
| `--pg-surface-lowest` | `#ffffff` | `#0e0e0f` |
| `--pg-surface-low` | `#f0f4fa` | `#1c1b1c` |
| `--pg-surface-container` | `#e8ecf4` | `#201f20` |
| `--pg-surface-high` | `#e0e5ee` | `#2a2a2b` |
| `--pg-surface-highest` | `#d8dee8` | `#353436` |

### Text & outline

| Token | Light | Dark |
|---|---|---|
| `--pg-on-surface` | `#191c1e` | `#e5e2e3` |
| `--pg-on-surface-variant` | `#43474b` | `#c6c6ca` |
| `--pg-outline` | `#73787b` | `#909094` |
| `--pg-outline-variant` | `#c3c7cb` | `#45474a` |

### Brand / primary

| Token | Light | Dark |
|---|---|---|
| `--pg-primary` | `#50616b` (slate) | `#f1f0f4` (platinum) |
| `--pg-primary-container` | `#e0f2fe` | `#d4d4d8` |
| `--pg-primary-fixed-dim` | `#b7c9d5` | `#c6c6ca` |

### Bull / bear (semantic, not decorative)

| Token | Light | Dark | Role |
|---|---|---|---|
| `--pg-bull` | `#62df7d` | `#00e383` | Fills, positive accents, live-dot |
| `--pg-bull-text` | `#006e2d` | `#00e383` | Bull text — forest in light, mint in dark |
| `--pg-bull-wash` | `rgba(0,110,45,.10)` | `rgba(0,227,131,.10)` | Pill/badge backgrounds |
| `--pg-bear` | `#ffb4ab` | `#ffb2b8` | Bear accents |
| `--pg-bear-text` | `#bf0715` | `#ffb2b8` | Bear text — ruby in light, rose-gold in dark |
| `--pg-bear-wash` | `rgba(191,7,21,.09)` | `rgba(255,178,184,.10)` | Pill/badge backgrounds |

### Score bands — mode-locked (§ the important exception)

`scoreHex(score)` / `scoreBand(score)` in `theme.ts` return the **same hex in light, dark, and
regardless of theme** — this is deliberate. Unlike the tokens above (which flip per theme), a
score of 22 must read as "Cautious" whether the trader is in light or dark mode, so these five
are raw, unthemed hex:

| Range | Name | Hex |
|---|---|---|
| 85–100 | Very Bullish | `#00e383` |
| 55–84 | Bullish | `#006e2d` |
| 30–54 | Neutral | `#a1a1aa` |
| 15–29 | Cautious | `#ffb2b8` |
| 0–14 | Bearish | `#bf0715` |

Never hand-pick a Tailwind/CSS color for score UI — always go through `scoreHex()` / `scoreBand()`
so a future rebalance of the bands lands in one place. Mirrored (for documentation/discoverability,
not consumed directly by the desktop tree) as `pgd-*` / `pgl-*` / `band-*` entries in
[`apps/mobile/tailwind.config.js`](apps/mobile/tailwind.config.js) — the desktop tree itself reads
`theme.ts`, never Tailwind classes, since it's plain CSS.

### Error (system errors, not negative performance)

| Token | Light | Dark |
|---|---|---|
| `--pg-error` | `#ba1a1a` | `#ffb4ab` |
| `--pg-error-container` | `#ffdad6` | `#93000a` |

---

## 3. Typography

| Family | CSS var | Use |
|---|---|---|
| Inter | `--pg-font-sans` | Prose, headings, labels, buttons |
| Space Grotesk | `--pg-font-num` | Numerals, tickers, `.label-caps` |

Loaded via Google Fonts `<link>` injected by `runtime.ts` (`installPlatinumGlass()`) — not a
native font asset, since this tree only ever runs on web.

| Class | Size / line | Weight | Use |
|---|---|---|---|
| `.pg-h1` | 48 / 1.1, `-0.02em` | 500 | Page-level display |
| `.pg-h2` | 32 / 1.2, `-0.01em` | 500 | Section headlines |
| `.pg-h3` | 24 / 1.3 | 500 | Card titles |
| `.pg-body-lg` | 18 / 1.6 | 400 | Lead paragraphs |
| `.pg-body-md` | 16 / 1.6 | 400 | Body text |
| `.pg-body-sm` | 14 / 1.55 | 400 (muted) | Secondary text |
| `.pg-caption` | 12.5 / 1.5 | 400 (dim) | Metadata |
| `.label-caps` | 12 / 1, `0.05em`, uppercase | 600 | Section labels — non-negotiable |
| `.pg-num` | inherits, tabular | 400–500 | Any numeral, price, ticker |
| `.pg-num-hero` | 60 / 1, `-0.03em` | 500 | Hero P&L / equity number |

`.pg-num` and `.pg-num-hero` always set `font-variant-numeric: tabular-nums` — required so a
column of prices lines up.

---

## 4. Spacing, radii, motion

### Spacing (`--pg-xs` … `--pg-xl`)

| Token | Value |
|---|---|
| `--pg-xs` | 4px |
| `--pg-sm` | 8px |
| `--pg-md` | 16px |
| `--pg-gutter` | 20px |
| `--pg-lg` | 24px |
| `--pg-xl` | 40px |

### Radii

| Token | Value | Use |
|---|---|---|
| `--pg-r-sm` | 4px | Tiny badges |
| `--pg-r-lg` | 8px | Inset panels, inputs |
| `--pg-r-xl` | 12px | `.pg-card` — the default card radius |
| `--pg-r-2xl` | 16px | `.pg-card--hero` |
| `--pg-r-3xl` | 24px | Reserved for large panels / error states |
| `9999px` | pill | Buttons, pills, avatars, sidebar links |

### Motion

| Animation | Duration | Use |
|---|---|---|
| `pg-shimmer` | 2s linear infinite | `Skel` / `SkelRows` loading |
| `pg-fade-up` | 350ms ease-out | Card entry |
| `pg-ping` | 2s cubic-bezier(0,0,.2,1) | Error-state ring |
| `pg-pulse-dot` | 1.4s ease-in-out infinite | Live-data dot (`.pg-live-dot`) |
| `pg-spin` | 900ms linear infinite | Inline spinners |

All five are disabled under `@media (prefers-reduced-motion: reduce)` in one block at the bottom
of `PLATINUM_CSS` — don't add a new `@keyframes` without adding it to that guard too.

---

## 5. Glass & elevation

### Card (`.pg-card`, §6 primitive: `Card` in `primitives.tsx`)

**Light:** `#ffffff` background, `1px solid rgb(203 213 225)` border, layered shadow
(`0 1px 2px rgba(15,23,42,.04), 0 8px 24px rgba(15,23,42,.06)`), no blur.

**Dark:** `rgba(10,10,11,.6)` background, `1px solid rgba(255,255,255,.10)` border,
`backdrop-filter: blur(24px) saturate(180%)`, plus an inset top highlight
(`inset 0 1px 0 rgba(255,255,255,.06)`) and a subtle gradient sheen
(`linear-gradient(180deg, rgba(255,255,255,.04) 0%, transparent 30%)`).

Variants: `.pg-card--dense` (tighter padding), `.pg-card--flush` (no padding),
`.pg-card--hero` (16px radius, larger padding). `.pg-inset` is the non-card "recessed" surface
used for small panels inside a card (e.g. the signed-in-as block in the sidebar).

### Ambient halos

Two blurred (`120px`), pill-shaped layers behind the whole app (`.pg-root::before` /
`.pg-root::after`), tinted by `data-regime` on the root:

| Regime | Light halo | Dark halo |
|---|---|---|
| `bull` | `rgba(98,223,125,.35)` | `rgba(0,227,131,.08)` |
| `bear` | `rgba(255,180,171,.28)` | `rgba(255,178,184,.07)` |
| neutral | theme default halo pair | theme default halo pair |

Ambient only — the halo tints the canvas, never the foreground card/text colors. `useRegime()`
(`regime.ts`) derives the regime and drives `data-regime` from `Shell.tsx`.

### Header / sidebar

| Surface | Blur | Light | Dark |
|---|---|---|---|
| `.pg-header` (sticky, 64px) | `blur(24px) saturate(180%)` | `rgba(255,255,255,.55)` | `rgba(0,0,0,.40)` |
| `.pg-sidebar` (280px) | `blur(24px)` | `rgba(248,250,252,.72)` | `#0a0a0b` (opaque) |

---

## 6. Layout

```
┌──────────────────────────── header (64px, sticky) ─────────────────────────┐
├───────────────┬──────────────────────────────────────────────────────────┤
│  sidebar       │  main (max-width 1600px, centred)                         │
│  280px         │                                                          │
│  (240px        │  .pg-grid → 12-col bento, gap 20px                       │
│   below 1280)  │                                                          │
└───────────────┴──────────────────────────────────────────────────────────┘
```

Bento cells declare `data-span="{3|4|5|6|7|8|12}"` out of 12 columns (`.pg-cell`). Below 1280px,
3/4-spans become 6 and 5/7/8-spans become 12 (full width) — the grid reflows to two columns
before it ever squeezes a tile (§ laptop reflow in `PLATINUM_CSS`).

---

## 7. File map (verified against `apps/mobile/src/desktop/` and `src/components/`)

```
apps/mobile/src/
├── components/
│   └── DesktopShell.tsx      The switch point. Native/narrow-web/no-session → children
│                              untouched. Wide web + session → mounts DesktopApp instead.
└── desktop/                   Web-only. Never imported by anything native evaluates.
    ├── theme.ts               PLATINUM_CSS + score-band tokens. The token registry.
    ├── runtime.ts             installPlatinumGlass() — injects the stylesheet + webfonts
    │                          into document.head, idempotently.
    ├── nav.tsx                DesktopRoute union + NavProvider/useNav — a tiny in-memory
    │                          router (desktop replaces expo-router, so it needs its own).
    ├── DesktopApp.tsx         Root component: installs the stylesheet, wraps NavProvider +
    │                          Shell, and switches on `route.name` to the right screen.
    ├── Shell.tsx               Header (brand mark, regime pill, equity/P&L, theme toggle)
    │                          + Sidebar (7 nav links, pending-approvals badge, sign out).
    ├── primitives.tsx          Card, Stack, Row, Cell, Numeral, Pill, ScorePill, ScoreBar,
    │                          DeltaPill, Button, IconButton, Skel, SkelRows,
    │                          DataStreamInterrupted, StatTile, PageHead, Label, CardHead.
    ├── icons.tsx               16 hand-rolled SVG icons (nav + status). No icon-library
    │                          dependency pulled into the desktop bundle.
    ├── format.ts               usd / signedUsd / signedPct / tone / ago / clock / ruleLabel /
    │                          humanize — desktop's own formatters (mobile has its own; the
    │                          two trees intentionally don't share this kind of helper either).
    ├── regime.ts               useRegime() — derives bull/bear/neutral for the ambient halo
    │                          and header pill from live account/portfolio data.
    └── screens/
        ├── Dashboard.tsx       Portfolio hero, market mode, open positions / pending picks
        │                      counters, today P&L, risk-saved/regret/vetoes, opportunity
        │                      radar, agent activity, ghost P&L, veto ledger.
        ├── Picks.tsx           Pending council proposals inbox + "run the council" CTA.
        ├── PickDetail.tsx      Single proposal: thesis, factors, approve/decline.
        ├── Council.tsx         Live view of a council run (the 7 nodes deliberating).
        ├── Positions.tsx       Open positions, equity/P&L/cash, per-position exit plan.
        ├── Strategies.tsx      Per-strategy performance over the reflection window.
        ├── Review.tsx          Grade-what-closed queue (agreement with the reflection loop).
        ├── Insights.tsx        Veto ledger / ghost P&L / calibration tabs.
        └── Settings.tsx        Broker connections, appearance (System/Daylight/Platinum
                               Glass), session, watchlist, system health.
```

Every screen above is wired to the **same hooks the mobile app uses**
(`useAccount`, `usePendingApprovals`, etc.) — the desktop tree is a second view of real data, not
a mockup with placeholder content.

---

## 8. States

- **Loading:** `Skel` (single shimmer block) / `SkelRows` (N shimmer rows) — never a bare spinner
  as the only feedback on first paint.
- **Empty:** a designed empty state with a one-line explanation and a CTA where one makes sense
  (e.g. Picks' "Inbox clear… Run the council on a watchlist name"). Never the literal string
  "No data available."
- **Error:** `DataStreamInterrupted` (`primitives.tsx`) — the desktop port of the mobile/Stitch
  "Data Stream Interrupted" pattern: rose halo + ring + retry.

---

## 9. Rules for new desktop work

1. **Never hard-code a hex.** Add a `--pg-*` token to `theme.ts` first, or use an existing one.
2. **Never import a mobile component into `desktop/`, or vice versa.** The two trees are
   deliberately separate; share logic only through hooks/services that already sit above both
   (e.g. `@/hooks/*`, `@/stores/*`), never through UI components.
3. **Approve/primary CTA is platinum/ink (`.pg-btn-primary`), never `--pg-bull` green.** Green is
   fills and positive P&L only — see the comment above `.pg-btn-primary` in `theme.ts`.
4. **Loading/empty → `Skel`/`SkelRows`/a designed empty state. Never bare "No data" text.**
5. **Numerals get `.pg-num` (or `.pg-num-hero`).** Tabular figures, Space Grotesk.
6. **Test at 1024px, 1280px (laptop reflow), and ≥1600px (main caps out), in both themes**, and
   confirm the change is invisible below 1024px / on native.
7. **New top-level section?** Add it to `DesktopRoute` and `SectionId` in `nav.tsx`, the switch in
   `DesktopApp.tsx`, and the `NAV` array in `Shell.tsx` — all three, or the sidebar/router drift
   apart.

---

## 10. Change log

| Date | Commit(s) | Change |
|---|---|---|
| 2026-08-25 | `f2156171`, `9e5604f9` | Desktop build unblocked (react-dom pin, BiometricGate web branch) then the full Platinum Glass tree landed: `theme.ts`, `runtime.ts`, `nav.tsx`, `Shell.tsx`, `primitives.tsx`, `icons.tsx`, `format.ts`, `regime.ts`, 9 screens, `tailwind.config.js` token mirror. Verified end-to-end in a browser (typecheck clean, all 7 sections + theme toggle functional, mobile confirmed unaffected at 375px). This file added retroactively — the code cited it before it existed in the repo. |

Append every design-token-affecting change here, same as the pattern in `DESIGN.md`'s own history
(tracked instead via `fable5findings.md`'s build log — see that file for the day-to-day narrative).
