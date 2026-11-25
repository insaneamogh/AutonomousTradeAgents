/**
 * The Aurora Calm primitives: rings, gauge, money, ticker tile.
 *
 * Rendered with react-test-renderer, so each assertion reads the real
 * SVG/DOM props the component emits rather than a mocked shape.
 */
import type { ReactElement } from 'react';
import { act, create } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';

import type { ApprovalProposalDto } from '@app/shared-types';

import { Gauge, Money, ScoreBar, ScorePill, ScoreRing, TickerTile } from './primitives';
import { pickTitle } from './screens/Picks';
import { rangePct } from './screens/Positions';

type Node = { type: string; props: Record<string, unknown>; children: (Node | string)[] | null };

function render(el: ReactElement): Node {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(el);
  });
  return renderer.toJSON() as Node;
}

function walk(node: Node | string, visit: (n: Node) => void): void {
  if (typeof node === 'string') return;
  visit(node);
  (node.children ?? []).forEach((c) => walk(c, visit));
}

function all(root: Node, type: string): Node[] {
  const out: Node[] = [];
  walk(root, (n) => {
    if (n.type === type) out.push(n);
  });
  return out;
}

function text(node: Node | string): string {
  if (typeof node === 'string') return node;
  return (node.children ?? []).map(text).join('');
}

/** Arc length (first number) of every `strokeDasharray` in the tree. */
function arcs(root: Node): number[] {
  return all(root, 'circle').map((c) => Number(String(c.props.strokeDasharray).split(' ')[0]));
}

describe('ScoreRing', () => {
  it('draws an arc proportional to the score', () => {
    const half = arcs(render(<ScoreRing score={50} size={36} />))[1];
    const full = arcs(render(<ScoreRing score={100} size={36} />))[1];
    // stroke = 4, r = (36 - 4) / 2 = 16, circumference = 2 * pi * 16
    expect(full).toBeCloseTo(100.53, 1);
    expect(half).toBeCloseTo(full / 2, 1);
  });

  it('clamps out-of-range scores instead of overdrawing', () => {
    const over = arcs(render(<ScoreRing score={140} />))[1];
    const exact = arcs(render(<ScoreRing score={100} />))[1];
    const under = arcs(render(<ScoreRing score={-5} />))[1];
    expect(over).toBe(exact);
    expect(under).toBe(0);
  });

  it('centres the numeral on the ring, not on a font-metric guess', () => {
    const root = render(<ScoreRing score={87} size={36} />);
    const label = all(root, 'text')[0];
    expect(label.props.dominantBaseline).toBe('central');
    expect(label.props.textAnchor).toBe('middle');
    expect(text(label)).toBe('87');
  });
});

describe('Gauge', () => {
  it('sweeps 270 degrees at 100 and a proportional share below that', () => {
    const [track, value100] = arcs(render(<Gauge value={100} size={72} />));
    const [, value50] = arcs(render(<Gauge value={50} size={72} />));
    expect(value100).toBeCloseTo(track, 1);
    expect(value50).toBeCloseTo(value100 / 2, 1);
    // 270 / 360 of the circumference (r = (72 - 6) / 2 = 33)
    expect(track).toBeCloseTo(2 * Math.PI * 33 * 0.75, 1);
  });

  it('only draws the caption when the gauge is big enough to hold it', () => {
    const small = render(<Gauge value={87} size={72} label="conviction" />);
    const big = render(<Gauge value={87} size={88} label="conviction" />);
    expect(all(small, 'text').map(text)).toEqual(['87']);
    expect(all(big, 'text').map(text)).toEqual(['87', 'conviction']);
    // The caption is never lost to assistive tech.
    expect(small.props['aria-label']).toBe('87 conviction');
  });

  it('exposes the value and caption to assistive tech', () => {
    const root = render(<Gauge value={52} size={96} label="hit rate" suffix="%" />);
    expect(root.props['aria-label']).toBe('52% hit rate');
  });
});

describe('Money', () => {
  it('splits whole dollars from quieter cents', () => {
    const root = render(<Money value={100412.3} />);
    expect(text(root)).toBe('$100,412.30');
    expect(text(all(root, 's')[0])).toBe('.30');
  });

  it('puts the minus sign before the dollar sign for losses', () => {
    expect(text(render(<Money value={-48} />))).toBe('−$48.00');
  });
});

describe('TickerTile', () => {
  it('shows short tickers whole and truncates long ones', () => {
    expect(text(render(<TickerTile symbol="AMD" />))).toBe('AMD');
    expect(text(render(<TickerTile symbol="NVDA" />))).toBe('NV');
  });

  it('reads an option symbol as its underlying', () => {
    expect(text(render(<TickerTile symbol="NVDA261218C00225000" />))).toBe('NV');
  });

  it('gives an unknown ticker a stable colour', () => {
    const a = render(<TickerTile symbol="ZZZZ" />);
    const b = render(<TickerTile symbol="ZZZZ" />);
    const bg = (a.props.style as { background: string }).background;
    expect(bg).toMatch(/^hsl\(/);
    expect(bg).toBe((b.props.style as { background: string }).background);
  });
});

describe('ScorePill / ScoreBar', () => {
  it('keeps the band name in the accessible label, so colour is never the only signal', () => {
    const root = render(<ScorePill score={87} />);
    expect(root.props['aria-label']).toBe('Very Bullish, 87 out of 100');
  });

  it('fills the bar from the accent token, with no inline colour', () => {
    const fill = all(render(<ScoreBar score={85} />), 'i')[0];
    expect(fill.props.style).toEqual({ width: '85%' });
  });
});

describe('pickTitle', () => {
  const base = {
    id: 'p1',
    symbol: 'NVDA',
    side: 'BUY',
    qty: 4,
    orderType: 'LIMIT',
    estimatedNotional: 868,
  } as unknown as ApprovalProposalDto;

  it('names an option by expiry, strike and type', () => {
    const t = pickTitle({ ...base, isOption: true, strike: 225, expiryDate: '2026-12-18', contractType: 'call' });
    expect(t).toBe('Buy 4 Dec 18 $225 calls');
  });

  it('uses the singular for a single contract', () => {
    const t = pickTitle({ ...base, qty: 1, isOption: true, strike: 112, expiryDate: '2026-12-18', contractType: 'put' });
    expect(t).toBe('Buy 1 Dec 18 $112 put');
  });

  it('describes a plain equity order as shares', () => {
    expect(pickTitle({ ...base, qty: 90 })).toBe('Buy 90 shares');
  });

  it('says Short, not Sell, when a sell opens a short', () => {
    expect(pickTitle({ ...base, side: 'SELL', opensShort: true, qty: 10 })).toBe('Short 10 shares');
  });
});

describe('rangePct (stop to target marker)', () => {
  it('places the last price between the stop (0) and the target (100)', () => {
    expect(rangePct(100, 200, 150)).toBe(50);
    expect(rangePct(421.5, 452, 430.2)).toBeCloseTo(28.5, 0);
  });

  it('clamps a price beyond the stop or target to the ends', () => {
    expect(rangePct(100, 200, 80)).toBe(0);
    expect(rangePct(100, 200, 250)).toBe(100);
  });

  it('returns null when the plan cannot be drawn', () => {
    expect(rangePct(null, 200, 150)).toBeNull();
    expect(rangePct(100, null, 150)).toBeNull();
    expect(rangePct(100, 200, null)).toBeNull();
    // Target at or below the stop (a short, or a bad plan) is not a track.
    expect(rangePct(200, 100, 150)).toBeNull();
    expect(rangePct(100, 100, 100)).toBeNull();
  });
});
