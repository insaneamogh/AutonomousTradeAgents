# DESIGN.md — Design System & UI Specification

**Status:** v0.1
**Scope:** Mobile app (React Native + NativeWind)
**Principle:** Calm, premium, trustworthy. Trading apps are anxiety machines — yours shouldn't be.

---

## 1. Design philosophy

Three rules:

1. **Calm over loud.** Robinhood-style red/green flashing makes people overtrade. Use muted colors. Numbers can be precise without being aggressive.
2. **Information density without clutter.** Power users want data. Hide depth behind progressive disclosure (tap a row → expand details).
3. **Trust through transparency.** Every agent decision shows reasoning. Every order shows fees. No hidden behavior.

Anti-patterns to avoid:

- Gamification (no streaks, no confetti on wins — this is not Duolingo)
- Aggressive color saturation
- Dense graphs as the default home screen
- Surprise auto-actions without notification trails

---

## 2. Color system

### Semantic tokens (use these in code, never raw hex)

| Token | Light | Dark | Purpose |
|---|---|---|---|
| `bg-base` | `#FAFAF9` | `#0A0A0B` | App background |
| `bg-surface` | `#FFFFFF` | `#141416` | Cards, sheets |
| `bg-surface-elevated` | `#FFFFFF` | `#1C1C1F` | Modals, popovers |
| `bg-surface-muted` | `#F5F5F4` | `#1C1C1F` | Disabled, subtle |
| `border-subtle` | `#E7E5E4` | `#27272A` | Card borders |
| `border-strong` | `#D6D3D1` | `#3F3F46` | Inputs, dividers |
| `text-primary` | `#0C0A09` | `#FAFAF9` | Body text, headings |
| `text-secondary` | `#57534E` | `#A1A1AA` | Captions, metadata |
| `text-tertiary` | `#A8A29E` | `#71717A` | Disabled, hints |
| `accent-primary` | `#1E40AF` | `#3B82F6` | Primary CTA, links |
| `accent-primary-hover` | `#1E3A8A` | `#2563EB` | Pressed state |

### Trading semantic colors (the critical ones)

| Token | Light | Dark | Purpose |
|---|---|---|---|
| `gain` | `#15803D` | `#22C55E` | Positive P&L, buy fills |
| `gain-subtle` | `#DCFCE7` | `#14532D` | Backgrounds, badges |
| `loss` | `#B91C1C` | `#EF4444` | Negative P&L, sell fills |
| `loss-subtle` | `#FEE2E2` | `#7F1D1D` | Backgrounds, badges |
| `neutral` | `#57534E` | `#A1A1AA` | Unchanged, pending |
| `warning` | `#B45309` | `#F59E0B` | Risk warnings, expiring auth |
| `warning-subtle` | `#FEF3C7` | `#78350F` | Warning backgrounds |
| `danger` | `#991B1B` | `#DC2626` | Drawdown breaker, auth failure |
| `info` | `#1E40AF` | `#60A5FA` | Agent notices, info badges |

**Important:** Don't use bright green/red for everything positive/negative. Reserve them for P&L on the dashboard. For pills, badges, and small indicators, use the subtle variants. This is the single biggest visual difference between a calm trading app and an anxiety-inducing one.

### Accessibility

- All text/background combinations must meet WCAG AA (4.5:1 for body, 3:1 for large)
- Don't rely on color alone — pair gain/loss with arrows (▲ ▼) or `+` / `−`

---

## 3. Typography

### Font stack

- **Primary (UI):** Inter — geometric, neutral, excellent number rendering
- **Numeric (tabular):** Inter with `font-variant-numeric: tabular-nums` — critical for prices, P&L lining up in columns
- **Optional accent (headings only):** None initially. Add Söhne or similar when budget allows.

### Type scale

| Token | Size / Line | Weight | Use |
|---|---|---|---|
| `display` | 32 / 38 | 600 | Onboarding screens, big numbers |
| `h1` | 24 / 30 | 600 | Screen titles |
| `h2` | 20 / 26 | 600 | Section headers |
| `h3` | 17 / 24 | 600 | Card titles |
| `body` | 15 / 22 | 400 | Body text |
| `body-emphasis` | 15 / 22 | 500 | Emphasized inline text |
| `caption` | 13 / 18 | 400 | Metadata, timestamps |
| `caption-emphasis` | 13 / 18 | 500 | Labels |
| `micro` | 11 / 14 | 500 | Badges, footnotes |
| `mono-price` | 17 / 22 | 500 tabular | Price displays |
| `mono-price-lg` | 28 / 34 | 600 tabular | Hero P&L numbers |

**Rule:** Always use tabular numerals for prices and P&L. Otherwise columns won't align and the app will feel sloppy.

---

## 4. Spacing & layout

### Spacing scale (4pt base)

`0, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64`

Use `12` and `16` as your most common values. Avoid arbitrary spacings like `13` or `18`.

### Layout rules

- Screen edge padding: `16pt`
- Card internal padding: `16pt`
- List item vertical padding: `12pt`
- Section spacing: `24–32pt`
- Safe area: always respect top/bottom safe areas, never put critical content under notch or home bar

### Border radius

| Token | Value | Use |
|---|---|---|
| `radius-sm` | 6 | Badges, pills |
| `radius-md` | 10 | Buttons, inputs |
| `radius-lg` | 14 | Cards |
| `radius-xl` | 20 | Bottom sheets, modals |
| `radius-full` | 9999 | Avatars, FABs |

---

## 5. Components

### Buttons

| Variant | Use |
|---|---|
| **Primary** | One per screen — main CTA (Approve Trade, Connect Broker) |
| **Secondary** | Less critical actions (View Details) |
| **Tertiary / Ghost** | Inline links, low-emphasis |
| **Destructive** | Sell-everything, disconnect broker, cancel auto-approval |

Sizes: `sm` (32pt), `md` (44pt — default), `lg` (52pt — used for hero CTAs)

State requirements: every button must have: `default`, `pressed`, `disabled`, `loading`. Loading must show a spinner inline, not replace the label.

### Cards

Default card: white surface (light) / `bg-surface` (dark), `radius-lg`, 1px `border-subtle`, no shadow in light mode, subtle shadow in dark.

Don't stack shadows on shadows. **One shadow level per surface.**

### Approval card (the most important component)

Layout:

```
┌────────────────────────────────────────┐
│ AGENT PROPOSAL              2 min ago  │
├────────────────────────────────────────┤
│ BUY  NVDA  10 shares                   │
│ Limit @ $XXX.XX · Est. $XXXX.XX        │
│                                        │
│ ▶ Why this trade  (tap to expand)      │
│                                        │
│ Risk: ●●○○○ Low-Medium                 │
│ Conviction: ●●●●○ High                 │
│                                        │
│ ┌──────────────┐  ┌────────────────┐   │
│ │  Decline     │  │  Approve   →   │   │
│ └──────────────┘  └────────────────┘   │
│ Auto-decline in 14:23                  │
└────────────────────────────────────────┘
```

- Use `mono-price-lg` for the share count and price
- Expandable rationale section
- Visible countdown timer for auto-decline
- **"Approve" button uses `accent-primary`, NOT green** — green is for fills, not for approve actions

### Number display

**Hero P&L (top of dashboard):**

- Use `mono-price-lg`
- Show ▲ or ▼ before the absolute change
- Color from `gain` / `loss` / `neutral`
- Show percentage in `caption-emphasis` underneath

**Inline P&L (lists):**

- Use `mono-price`
- Right-aligned
- Use subtle background pills for emphasis: `bg-gain-subtle` / `bg-loss-subtle`

### Charts

- Library: Victory Native XL or `react-native-svg-charts`
- Default range: 1D, 1W, 1M, 3M, 1Y, ALL
- Colors: single line color (`accent-primary`), avoid filled area charts as default (they look heavy)
- Annotations: show entry/exit points on candle charts as small ○ markers with hover labels
- Grid: very subtle, `border-subtle`

### Tables / Lists

- Alternating row backgrounds: **don't**. Use just `border-subtle` between rows.
- Sticky column headers when scrolling
- Pull-to-refresh on all list screens
- Empty states must explain what would normally appear here

### Input fields

- 44pt minimum height
- Label above, helper text below
- Error state uses `loss` for both border and helper text
- Numeric inputs use `mono-price` font

---

## 6. Iconography

- Library: **Lucide** (consistent with web ecosystem, matches your stack)
- Sizes: 16, 20, 24
- Stroke width: 1.75 (slightly heavier than default 1.5 for better mobile legibility)
- Color: inherit from text color by default

### Custom icons needed

- Agent / council (something abstract, not a robot face)
- Strategy (deck of cards or flowchart node)
- Approval pending (clock variant)
- Auto-mode active (toggle/lightning)

---

## 7. Motion & animation

### Principles

- **Fast:** 150–250ms for most transitions
- **Purposeful:** animation should communicate state change, never decorate
- **Reduced motion:** respect `useReducedMotion()` system setting — fallback to instant or opacity-only

### Standard durations

| Type | Duration | Easing |
|---|---|---|
| Micro (icon, color) | 150ms | ease-out |
| State change (button) | 200ms | ease-out |
| Transition (sheet, modal) | 280ms | spring (damping 22, stiffness 200) |
| Page navigation | 320ms | spring (default Expo Router) |

### Critical animations to design

- **Approval swipe:** swipe right to approve, left to decline, with haptic feedback at threshold
- **Number tick:** when P&L updates, brief flash (200ms) of `gain-subtle` / `loss-subtle` background, then settle
- **Agent thinking:** subtle pulsing dot animation when agent is processing
- **Drawdown breaker triggered:** strong animation — red banner slides down, persistent until acknowledged

---

## 8. Dark / Light mode rules

- **Default:** follow system
- **Override:** in Settings, three options — System, Light, Dark
- **Persist:** MMKV stored, applied before first render to avoid flash

### Mode-specific rules

**Light mode:**

- True white surfaces (`#FFFFFF`)
- Heavy reliance on borders, not shadows
- Slightly higher contrast in text

**Dark mode:**

- Never pure black (`#000`) — use `#0A0A0B` for base, `#141416` for surface
- Subtle shadows allowed
- Lower-contrast text for body (avoid pure white on dark)
- Saturated accent colors slightly desaturated to reduce eye strain

---

## 9. Haptics

iOS and Android both. Use sparingly.

| Event | Haptic |
|---|---|
| Button press (primary CTA only) | Light impact |
| Approve trade | Medium impact |
| Decline trade | Light impact |
| Order filled | Success notification |
| Order rejected / risk breach | Warning notification |
| Drawdown breaker | Error notification |
| Pull-to-refresh threshold | Selection |

Don't haptic on scrolling, navigation, or routine taps. It becomes noise.

---

## 10. Accessibility checklist

- All interactive elements: minimum 44×44pt tap target
- `accessibilityLabel` and `accessibilityHint` on every Touchable
- VoiceOver / TalkBack tested on every screen before ship
- Dynamic Type / Font Scale support up to 200%
- Color contrast meets WCAG AA minimum, AAA preferred for body text
- Don't convey state through color alone
- Loading states announce to screen readers
- Forms have inline error messages with `accessibilityLiveRegion`

---

## 11. Empty states & error states

Every list, screen, and component needs:

- **Loading state:** skeleton, not spinner (spinners feel slower)
- **Empty state:** illustration + one-line explanation + CTA if applicable
- **Error state:** clear cause + suggested action + retry button

Specific empty states to design:

- "No agent activity today" — Home dashboard when agent inactive
- "No pending approvals" — Approval inbox
- "No strategies yet" — Strategies tab
- "No trade history" — History tab
- "Broker not connected" — Most critical empty state, must drive to OAuth flow

---

## 12. Component library setup

Build these as reusable primitives early:

```
packages/ui/src/
├── tokens.ts              All design tokens (colors, type, spacing, radii)
├── Button.tsx
├── Card.tsx
├── ApprovalCard.tsx
├── PriceDisplay.tsx
├── PnLBadge.tsx
├── ListItem.tsx
├── BottomSheet.tsx
├── Input.tsx
├── NumberInput.tsx
├── Chart.tsx
├── EmptyState.tsx
├── ErrorState.tsx
├── SkeletonLoader.tsx
├── Avatar.tsx
├── Badge.tsx
├── HapticPressable.tsx    Wraps Pressable + haptic
└── utils.ts               cn() helper, color resolution helpers
```

**Don't reach for a UI library** (like Tamagui or React Native Paper). Build these yourself — for an opinionated, brand-distinct fintech app, you'll end up overriding 80% of any library and the dependency cost isn't worth it.

---

## 13. Implementation order for the design system

- **Week 1:** `tokens.ts` + Button + Card + PriceDisplay + PnLBadge
- **Week 2:** ApprovalCard + BottomSheet + Input
- **Week 3:** ListItem + EmptyState + ErrorState + SkeletonLoader
- **Week 4:** Chart + HapticPressable + polish pass

Build in Storybook (works with React Native via `@storybook/react-native`). Every component gets a story with all states (default, pressed, disabled, loading, error). Saves you from re-finding edge cases later.

---

## 14. Closing notes

The design system is more opinionated than you might expect for a v0.1. That's deliberate — fintech apps live or die on whether they feel trustworthy, and trustworthiness comes from consistency. Setting strict tokens now prevents the "Robinhood-Frankenstein" drift that happens when you make ad-hoc color/spacing decisions across 9 screens.

**On the calm-color point:** push back hard if anyone is ever tempted to use bright green/red for everything. Look at how Wealthfront, Public.com, and Frec handle it versus how Robinhood does. The muted-color apps have measurably lower overtrading rates among their users. That matters for liability surface too.
