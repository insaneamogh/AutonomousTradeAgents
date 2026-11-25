/**
 * Geometry for the 270-degree gauge, kept free of react-native so it can be
 * unit-tested without an SVG runtime. Same maths as the web `Gauge`
 * (`src/desktop/primitives.tsx`): open at the bottom, arc starts at 135deg.
 */

export interface GaugeGeometry {
  stroke: number;
  r: number;
  circumference: number;
  /** Length of the full 270-degree track. */
  sweep: number;
  /** Length of the filled arc for `value`. */
  fill: number;
}

export function gaugeGeometry(value: number, size: number): GaugeGeometry {
  const stroke = Math.max(4, Math.round(size / 13));
  const r = (size - stroke) / 2;
  const circumference = 2 * Math.PI * r;
  const sweep = circumference * 0.75;
  const pct = Math.max(0, Math.min(1, value / 100));
  return { stroke, r, circumference, sweep, fill: sweep * pct };
}

/** The in-arc caption only fits once the gauge is big enough. */
export const GAUGE_LABEL_MIN_SIZE = 84;

export function showsGaugeLabel(label: string | undefined, size: number): boolean {
  return Boolean(label) && size >= GAUGE_LABEL_MIN_SIZE;
}
