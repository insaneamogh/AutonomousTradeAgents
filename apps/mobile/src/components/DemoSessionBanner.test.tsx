/**
 * DemoSessionBanner tests (docs/IMPL_DEMO_SESSION.md §4).
 *
 * Renders via react-test-renderer directly against the real authStore
 * (mirrors SymbolResultsList.test.tsx's style for a presentational
 * component, and authStore.test.ts's style of driving the store with
 * `setState` rather than mocking it).
 */
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';

import { useAuthStore } from '@/stores/authStore';

import { DemoSessionBanner } from './DemoSessionBanner';

function renderBanner(): ReactTestRenderer {
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(<DemoSessionBanner />);
  });
  return tree;
}

describe('DemoSessionBanner', () => {
  beforeEach(() => {
    useAuthStore.setState({ status: 'idle', user: null, accessToken: null });
  });

  it('renders nothing with no signed-in user', () => {
    const tree = renderBanner();
    expect(tree.toJSON()).toBeNull();
  });

  it('renders nothing for a normal (non-demo) session', () => {
    useAuthStore.setState({
      status: 'authenticated',
      accessToken: 'a',
      user: { userId: 'u1', email: 'trader@example.com' },
    });

    const tree = renderBanner();

    expect(tree.toJSON()).toBeNull();
  });

  it('renders the read-only notice for a demo session', () => {
    useAuthStore.setState({
      status: 'authenticated',
      accessToken: 'a',
      user: { userId: 'u1', email: 'judge@example.com', authMethod: 'demo' },
    });

    const tree = renderBanner();

    expect(tree.toJSON()).not.toBeNull();
    const json = JSON.stringify(tree.toJSON());
    expect(json).toContain('Read-only demo');
    expect(json).toContain('trading actions are disabled');
    expect(() => tree.root.findByProps({ accessibilityRole: 'alert' })).not.toThrow();
  });
});
