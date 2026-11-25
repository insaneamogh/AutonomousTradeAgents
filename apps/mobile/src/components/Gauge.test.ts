import { gaugeGeometry, showsGaugeLabel } from './gaugeMath';

describe('gaugeGeometry', () => {
  it('sweeps exactly 270 degrees of the circle', () => {
    const g = gaugeGeometry(100, 96);
    expect(g.sweep).toBeCloseTo(g.circumference * 0.75, 6);
    expect(g.fill).toBeCloseTo(g.sweep, 6);
  });

  it('fills in proportion to the value', () => {
    const half = gaugeGeometry(50, 96);
    const full = gaugeGeometry(100, 96);
    expect(half.fill).toBeCloseTo(full.fill / 2, 6);
  });

  it('clamps values outside 0-100', () => {
    expect(gaugeGeometry(180, 96).fill).toBeCloseTo(gaugeGeometry(100, 96).fill, 6);
    expect(gaugeGeometry(-20, 96).fill).toBe(0);
  });

  it('keeps the arc inside the box, whatever the stroke', () => {
    for (const size of [56, 72, 96]) {
      const g = gaugeGeometry(50, size);
      expect(g.r + g.stroke / 2).toBeLessThanOrEqual(size / 2);
    }
  });
});

describe('showsGaugeLabel', () => {
  it('hides the caption on a gauge too small to hold it', () => {
    expect(showsGaugeLabel('confidence', 72)).toBe(false);
    expect(showsGaugeLabel('confidence', 84)).toBe(true);
  });

  it('never shows an absent caption', () => {
    expect(showsGaugeLabel(undefined, 120)).toBe(false);
  });
});
