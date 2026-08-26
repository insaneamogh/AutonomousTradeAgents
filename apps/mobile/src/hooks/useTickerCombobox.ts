/**
 * useTickerCombobox — headless state machine for a ticker typeahead.
 *
 * A direct lift of the state logic already proven in the desktop
 * `CouncilLauncher` (see `src/desktop/screens/Picks.tsx`): a query string
 * drives `useSymbolSearch`, an open/closed flag gates the suggestion list,
 * and an active index tracks arrow-key/highlight navigation. This hook
 * owns none of the DOM/RN-specific glue (blur timers, `<ul>` markup,
 * `Pressable` rows) — only the state — so both the desktop combobox (DOM)
 * and the mobile typeahead (React Native) can drive it with their own
 * input handling.
 */

import { useState } from 'react';

import { useSymbolSearch } from './useSymbolSearch';
import type { SymbolHit } from './useSymbolSearch';

export function useTickerCombobox(limit = 8) {
  const [query, setQueryState] = useState('');
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const results = useSymbolSearch(query, { limit });
  const hits = results.data ?? [];

  /** Typing opens the list and re-anchors the highlight to the top hit. */
  const setQuery = (next: string) => {
    setQueryState(next);
    setActiveIndex(0);
    setOpen(true);
  };

  const close = () => setOpen(false);

  /** Arrow-key style navigation; wraps around the current hit count. */
  const moveActive = (delta: number) => {
    setActiveIndex((i) => {
      if (hits.length === 0) return 0;
      return (i + delta + hits.length) % hits.length;
    });
  };

  /** Pick hit `i`. Closes the list and hands back what was picked. */
  const selectIndex = (i: number): SymbolHit | undefined => {
    const hit = hits[i];
    close();
    return hit;
  };

  const selectActive = (): SymbolHit | undefined => selectIndex(activeIndex);

  /** Full clear — call after a successful add, not before. */
  const reset = () => {
    setQueryState('');
    setOpen(false);
    setActiveIndex(0);
  };

  return {
    query,
    setQuery,
    open,
    close,
    setOpen,
    activeIndex,
    moveActive,
    hits,
    isLoading: results.isLoading,
    selectIndex,
    selectActive,
    reset,
  };
}

export type TickerCombobox = ReturnType<typeof useTickerCombobox>;
