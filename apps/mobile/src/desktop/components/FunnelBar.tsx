/**
 * Shared stepped-bar building blocks for the two funnel views —
 * ContractFunnel (which contract survived, for one symbol) and
 * ScanFunnel (which symbols ever reach a paid LLM pass at all). Both
 * render the same "labeled row, proportional bar, survivor count,
 * dropped count" shape; only the data source and the row-specific copy
 * differ, so that part lives here once instead of twice.
 */

import type { CSSProperties } from 'react';

export type FunnelScale = 'linear' | 'log';

/** The minimal shape a funnel row needs — `FunnelStageDto` already
 * matches this structurally, so ContractFunnel needs no data mapping to
 * use it; ScanFunnel builds its own values into this same shape. */
export interface FunnelBarDatum {
  key: string;
  label: string;
  survivors: number;
  dropped: number;
}

export function fmtInt(n: number): string {
  return n.toLocaleString('en-US');
}

export function barWidthPct(survivors: number, base: number, scale: FunnelScale): number {
  if (base <= 0 || survivors <= 0) return 0;
  if (scale === 'log') {
    const denom = Math.log(base + 1);
    return denom > 0 ? Math.min(100, (Math.log(survivors + 1) / denom) * 100) : 0;
  }
  return Math.min(100, (survivors / base) * 100);
}

/** The key of the first row with zero survivors, or null if none —
 * that's the stage the funnel actually died at; rows are naturally all
 * zero from there on, so only the first one is worth calling out. */
export function firstZeroStageKey(rows: FunnelBarDatum[]): string | null {
  for (const r of rows) {
    if (r.survivors === 0) return r.key;
  }
  return null;
}

export function FunnelBarRow({
  stage,
  index,
  base,
  scale,
  isZero,
}: {
  stage: FunnelBarDatum;
  index: number;
  base: number;
  scale: FunnelScale;
  isZero: boolean;
}) {
  const pct = barWidthPct(stage.survivors, base, scale);
  const textColor: CSSProperties['color'] = isZero ? 'var(--pg-bear-text)' : undefined;
  const fillColor = isZero ? 'var(--pg-bear)' : 'var(--pg-primary)';

  return (
    <div
      style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}
      aria-label={`${stage.label}: ${fmtInt(stage.survivors)} of ${fmtInt(base)}${
        index === 0 ? '' : `, ${fmtInt(stage.dropped)} dropped`
      }`}
    >
      <span
        className="pg-caption pg-truncate"
        title={stage.label}
        style={{ width: 150, flex: 'none', color: textColor }}
      >
        {stage.label}
      </span>
      <div className="pg-bar" style={{ flex: 1, height: 16 }} role="presentation" aria-hidden="true">
        <i
          data-stage={stage.key}
          style={{
            width: `${pct}%`,
            minWidth: stage.survivors > 0 ? 2 : 0,
            backgroundColor: fillColor,
          }}
        />
      </div>
      <span className="pg-num-right pg-num" style={{ width: 68, flex: 'none', color: textColor }}>
        {fmtInt(stage.survivors)}
      </span>
      <span className="pg-num-right pg-num pg-dim" style={{ width: 80, flex: 'none' }}>
        {index === 0 ? '' : `−${fmtInt(stage.dropped)}`}
      </span>
    </div>
  );
}
