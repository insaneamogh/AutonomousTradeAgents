/**
 * SymbolResultsList tests — target the dumb list component directly, not
 * the SymbolTypeahead smart wrapper, so no React Query mocking is needed
 * here. Rendered via react-test-renderer, resolvable through this
 * workspace's hoisted linker (see src/lib/api.test.ts for this repo's
 * other Jest test style).
 *
 * Every `renderer.create()` call is wrapped in `act()`: React's
 * concurrent renderer defers some effect flushing to a scheduler tick, and
 * without `act()` that tick fires after the test (and Jest's module
 * registry for this file) has already torn down, crashing the worker
 * instead of failing the test cleanly.
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';

import type { SymbolHit } from '@/hooks/useSymbolSearch';

import { SymbolResultsList } from './SymbolResultsList';

const HITS: SymbolHit[] = [
  { symbol: 'AAPL', name: 'Apple Inc.', fractionable: true },
  { symbol: 'AAPLW', name: 'Apple Inc Warrants', fractionable: false },
];

function renderList(hits: SymbolHit[], onSelect: (hit: SymbolHit) => void): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(<SymbolResultsList hits={hits} onSelect={onSelect} />);
  });
  return tree;
}

describe('SymbolResultsList', () => {
  it('renders nothing for an empty hits array', () => {
    const tree = renderList([], jest.fn());
    expect(tree.toJSON()).toBeNull();
  });

  it('renders one row per hit with the correct symbol and name', () => {
    const tree = renderList(HITS, jest.fn());

    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('AAPL');
    expect(json).toContain('Apple Inc.');
    expect(json).toContain('AAPLW');
    expect(json).toContain('Apple Inc Warrants');

    // One row per hit: each hit's accessibilityLabel resolves to exactly
    // one element in the tree. (findAllByProps over-matches on a prop
    // like accessibilityRole, since Pressable forwards it through its own
    // internal wrapper layers — but findByProps throws unless the given
    // prop combination is unique, so this pins "exactly one" per hit.)
    for (const hit of HITS) {
      expect(() =>
        tree.root.findByProps({ accessibilityLabel: `${hit.symbol} — ${hit.name}` }),
      ).not.toThrow();
    }
  });

  it('calls onSelect with the tapped hit, not just the tapped index', () => {
    const onSelect = jest.fn();
    const tree = renderList(HITS, onSelect);

    const secondRow = tree.root.findByProps({
      accessibilityLabel: 'AAPLW — Apple Inc Warrants',
    });
    act(() => {
      secondRow.props.onPress();
    });

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(HITS[1]);
  });
});
