# @app/ui

Design tokens + React Native component primitives. Owned by `DESIGN.md`.

## Phase 0
Tokens only (`src/tokens.ts`). Component primitives land in Phase 3.

## The rules (also in DESIGN.md)
- **Approve button uses `accentPrimary`, never `gain`/green.** Green is reserved for fills + positive P&L.
- All colors via `palette[mode].<token>` — never raw hex.
- All numerals use `font.mono` with `font-variant-numeric: tabular-nums`.
- Every interactive element: 44pt minimum tap target, `accessibilityLabel` required.

## Build order (per DESIGN.md §13)
- Week 1: Button, Card, PriceDisplay, PnLBadge
- Week 2: ApprovalCard, BottomSheet, Input
- Week 3: ListItem, EmptyState, ErrorState, SkeletonLoader
- Week 4: Chart, HapticPressable, polish
