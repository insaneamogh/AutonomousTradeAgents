/**
 * ClosePositionButton tests (docs/IMPL_DEMO_SESSION.md §4).
 *
 * Same react-test-renderer style as SymbolResultsList.test.tsx. The key
 * assertion: a demo session DISABLES the button — it must still be found
 * in the tree (findByProps throws if it isn't there), never removed.
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';

import { ClosePositionButton } from './ClosePositionButton';

function renderButton(
  props: Partial<React.ComponentProps<typeof ClosePositionButton>> = {},
): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(
      <ClosePositionButton
        symbol="NVDA"
        qty={10}
        pending={false}
        busy={false}
        disabledForDemo={false}
        onPress={jest.fn()}
        {...props}
      />,
    );
  });
  return tree;
}

describe('ClosePositionButton', () => {
  it('is enabled and reads "Close now" for a normal session', () => {
    const tree = renderButton();

    const button = tree.root.findByProps({ accessibilityLabel: 'Close 10 NVDA now' });
    expect(button.props.disabled).toBe(false);
    expect(JSON.stringify(tree.toJSON())).toContain('Close now');
  });

  it('is disabled — but still present, not removed — for a demo session, with a stated reason', () => {
    const tree = renderButton({ disabledForDemo: true });

    // Throws if the button isn't in the tree at all — pins "disabled, not removed".
    const button = tree.root.findByProps({
      accessibilityLabel: 'Close 10 NVDA now — Disabled in read-only demo mode',
    });
    expect(button.props.disabled).toBe(true);
  });

  it('reads "Cancel the working ... order" + is disabled for a pending order in a demo session', () => {
    const tree = renderButton({ pending: true, disabledForDemo: true });

    const button = tree.root.findByProps({
      accessibilityLabel: 'Cancel the working NVDA order — Disabled in read-only demo mode',
    });
    expect(button.props.disabled).toBe(true);
    expect(JSON.stringify(tree.toJSON())).toContain('Cancel order');
  });

  it('is disabled while its own mutation is in flight, independent of demo status', () => {
    const tree = renderButton({ busy: true });

    const button = tree.root.findByProps({ accessibilityLabel: 'Close 10 NVDA now' });
    expect(button.props.disabled).toBe(true);
    expect(JSON.stringify(tree.toJSON())).toContain('Closing…');
  });
});
