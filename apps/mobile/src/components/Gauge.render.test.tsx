/**
 * The native Gauge renders under both colour schemes and takes its colours
 * from the palette tokens (no raw hex in the component).
 */
import type { ReactElement } from 'react';
import type * as Tokens from '@app/ui/tokens';
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';
import { Circle, Text as SvgText } from 'react-native-svg';

import { palette } from '@app/ui';

import { Gauge } from './Gauge';

// `@app/ui` re-exports Reanimated-backed components that cannot start under
// jest; the Gauge only needs the palette, so give it just the tokens.
jest.mock('@app/ui', () => jest.requireActual<typeof Tokens>('@app/ui/tokens'));

let mockScheme: 'light' | 'dark' = 'dark';
jest.mock('nativewind', () => ({
  useColorScheme: () => ({ colorScheme: mockScheme }),
}));

function mount(el: ReactElement): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  return r;
}

describe('native Gauge', () => {
  it('draws the track and arc in palette tokens, in both schemes', () => {
    for (const mode of ['light', 'dark'] as const) {
      mockScheme = mode;
      const [track, arc] = mount(<Gauge value={87} size={96} label="confidence" />).root.findAllByType(Circle);
      expect(track.props.stroke).toBe(palette[mode].hairline);
      expect(arc.props.stroke).toBe(palette[mode].accentPrimary);
    }
  });

  it('shows the rounded value, and the caption only once there is room for it', () => {
    mockScheme = 'light';
    const big = mount(<Gauge value={52.4} size={96} label="hit rate" suffix="%" />).root;
    expect(big.findAllByType(SvgText).map((t) => (t.props as { children: string }).children)).toEqual(['52%', 'hit rate']);
    const small = mount(<Gauge value={52.4} size={72} label="hit rate" suffix="%" />).root;
    expect(small.findAllByType(SvgText).map((t) => (t.props as { children: string }).children)).toEqual(['52%']);
  });

  it('keeps the full caption for screen readers even when it is not drawn', () => {
    mockScheme = 'dark';
    const small = mount(<Gauge value={87} size={72} label="conviction" />).root;
    expect(small.findAll((n) => n.props.accessibilityLabel === '87 conviction').length).toBeGreaterThan(0);
  });
});
