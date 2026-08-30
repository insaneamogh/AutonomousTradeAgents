/**
 * Platinum Glass primitives (STITCH_DESIGN_SYSTEM.md §6).
 *
 * Plain DOM + the `var(--pg-*)` tokens from `theme.ts`. No raw hex here
 * except the mode-locked score bands, which are behavioural rather than
 * brand (§2.5) and are centralised in `scoreHex()`.
 *
 * Web-only by construction — nothing in this file is reachable from the
 * native bundle (see `DesktopShell`).
 */

import type { CSSProperties, ReactNode } from 'react';

import { scoreBand, scoreHex } from './theme';

export type Tone = 'bull' | 'bear' | 'neutral' | 'warn';

/* ── Layout ──────────────────────────────────────────────────────── */

/**
 * A 12-column bento cell (§7.2).
 *
 * The span is a `data-span` attribute rather than an inline `gridColumn`
 * so the laptop reflow media query in `theme.ts` can override it — inline
 * styles would out-specificity any breakpoint rule.
 */
export function Cell({
  span,
  children,
  style,
}: {
  span: 3 | 4 | 5 | 6 | 7 | 8 | 12;
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <div className="pg-cell" data-span={span} style={style}>
      {children}
    </div>
  );
}

/** Vertical stack with a token gap. */
export function Stack({
  gap = 12,
  children,
  style,
}: {
  gap?: number;
  children: ReactNode;
  style?: CSSProperties;
}) {
  return <div style={{ display: 'flex', flexDirection: 'column', gap, minWidth: 0, ...style }}>{children}</div>;
}

/** Horizontal row. */
export function Row({
  gap = 12,
  children,
  style,
}: {
  gap?: number;
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap, minWidth: 0, ...style }}>{children}</div>
  );
}

/* ── Card (§6.1) ─────────────────────────────────────────────────── */

export function Card({
  children,
  variant,
  className,
  style,
}: {
  children: ReactNode;
  variant?: 'dense' | 'flush' | 'hero';
  className?: string;
  style?: CSSProperties;
}) {
  const cls = ['pg-card', variant ? `pg-card--${variant}` : '', className ?? '']
    .filter(Boolean)
    .join(' ');
  return (
    <div className={cls} style={{ flex: 1, ...style }}>
      {children}
    </div>
  );
}

/** `.label-caps` section label (§3.2). Every section label uses this. */
export function Label({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <span className="label-caps" style={style}>
      {children}
    </span>
  );
}

/** Card header: caps label on the left, optional accessory on the right. */
export function CardHead({ label, right }: { label: ReactNode; right?: ReactNode }) {
  return (
    <Row style={{ justifyContent: 'space-between', alignItems: 'center' }}>
      <Label>{label}</Label>
      {right}
    </Row>
  );
}

/* ── Numerals (§3) ───────────────────────────────────────────────── */

export function Numeral({
  children,
  size = 20,
  tone = 'neutral',
  weight = 500,
  style,
}: {
  children: ReactNode;
  size?: number;
  tone?: Tone;
  weight?: number;
  style?: CSSProperties;
}) {
  const color =
    tone === 'bull' ? 'var(--pg-bull-text)' : tone === 'bear' ? 'var(--pg-bear-text)' : 'var(--pg-on-surface)';
  return (
    <span className="pg-num" style={{ fontSize: size, lineHeight: 1.05, fontWeight: weight, color, ...style }}>
      {children}
    </span>
  );
}

/* ── Pills (§6.4 / §6.10) ────────────────────────────────────────── */

export function Pill({
  children,
  tone = 'neutral',
  glow = false,
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  glow?: boolean;
  title?: string;
}) {
  const cls = [
    'pg-pill',
    tone === 'bull'
      ? 'pg-pill--bull'
      : tone === 'bear'
        ? 'pg-pill--bear'
        : tone === 'warn'
          ? 'pg-pill--warn'
          : '',
    glow ? 'pg-pill--glow' : '',
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

/**
 * Conviction / score pill using the mode-locked 5-band palette.
 * Colour is never the only signal — the band name is in the title + aria.
 */
export function ScorePill({ score, suffix = '' }: { score: number; suffix?: string }) {
  const hex = scoreHex(score);
  const band = scoreBand(score);
  return (
    <span
      className="pg-pill"
      title={`${band} · ${Math.round(score)}/100`}
      aria-label={`${band}, ${Math.round(score)} out of 100`}
      style={{
        color: hex,
        backgroundColor: 'var(--pg-track)',
        borderColor: hex,
        boxShadow: score >= 85 ? `0 0 15px ${hex}33` : undefined,
      }}
    >
      {Math.round(score)}
      {suffix}
    </span>
  );
}

/** Thin score bar, same band palette. */
export function ScoreBar({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(100, score));
  return (
    <div className="pg-bar" role="presentation">
      <i style={{ width: `${pct}%`, backgroundColor: scoreHex(pct) }} />
    </div>
  );
}

/** Delta pill: `+1.24%` mint, `−0.30%` rose, `0.00%` neutral (§6.4). */
export function DeltaPill({ text, tone }: { text: string; tone: Tone }) {
  const glyph = tone === 'bull' ? '▲' : tone === 'bear' ? '▼' : '•';
  return (
    <Pill tone={tone}>
      <span aria-hidden style={{ fontSize: 9 }}>
        {glyph}
      </span>
      {text}
    </Pill>
  );
}

/* ── Buttons (§9) ────────────────────────────────────────────────── */

export function Button({
  children,
  onClick,
  kind = 'secondary',
  size,
  disabled,
  ariaLabel,
  style,
  type = 'button',
}: {
  children: ReactNode;
  onClick?: () => void;
  kind?: 'primary' | 'secondary' | 'ghost';
  size?: 'sm';
  disabled?: boolean;
  ariaLabel?: string;
  style?: CSSProperties;
  type?: 'button' | 'submit';
}) {
  const cls = ['pg-btn', `pg-btn-${kind}`, size === 'sm' ? 'pg-btn-sm' : ''].filter(Boolean).join(' ');
  return (
    <button type={type} className={cls} onClick={onClick} disabled={disabled} aria-label={ariaLabel} style={style}>
      {children}
    </button>
  );
}

export function IconButton({
  children,
  onClick,
  ariaLabel,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  ariaLabel: string;
  title?: string;
}) {
  return (
    <button type="button" className="pg-icon-btn" onClick={onClick} aria-label={ariaLabel} title={title ?? ariaLabel}>
      <span aria-hidden>{children}</span>
    </button>
  );
}

/* ── Skeletons (§8.1) — never "No data" on the dashboard ─────────── */

export function Skel({ h = 14, w = '100%', r }: { h?: number; w?: number | string; r?: number }) {
  return <div className="pg-skel" style={{ height: h, width: w, borderRadius: r }} />;
}

/** N shimmer rows, for a card whose data is pending OR genuinely empty. */
export function SkelRows({ rows = 4, h = 16 }: { rows?: number; h?: number }) {
  return (
    <Stack gap={10}>
      {Array.from({ length: rows }, (_, i) => (
        <Skel key={i} h={h} w={`${100 - i * 7}%`} />
      ))}
    </Stack>
  );
}

/* ── Error: "Data Stream Interrupted" (§8.3) ─────────────────────── */

export function DataStreamInterrupted({
  code,
  node,
  onRetry,
  compact = false,
}: {
  code: string;
  node: string;
  onRetry?: () => void;
  compact?: boolean;
}) {
  return (
    <div style={{ position: 'relative', flex: 1 }}>
      <div
        aria-hidden
        style={{
          position: 'absolute',
          inset: -4,
          borderRadius: 32,
          backgroundColor: 'var(--pg-error)',
          opacity: 0.2,
          filter: 'blur(48px)',
          pointerEvents: 'none',
        }}
      />
      <div
        className="pg-card"
        style={{
          position: 'relative',
          borderRadius: 24,
          alignItems: 'center',
          textAlign: 'center',
          gap: 18,
          padding: compact ? 24 : 40,
        }}
        role="alert"
      >
        <div style={{ position: 'relative', width: 96, height: 96 }}>
          <div
            aria-hidden
            className="pg-ping"
            style={{
              position: 'absolute',
              inset: 0,
              borderRadius: 9999,
              border: '1px solid var(--pg-error)',
              opacity: 0.4,
            }}
          />
          <div
            style={{
              position: 'absolute',
              inset: 0,
              borderRadius: 9999,
              backgroundColor: 'var(--pg-error-container)',
              opacity: 0.16,
              border: '1px solid var(--pg-error)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 40,
              color: 'var(--pg-error)',
            }}
          >
            <WifiOffGlyph />
          </div>
        </div>

        <h2 className="pg-h2" style={{ letterSpacing: '-0.02em' }}>
          Data Stream Interrupted
        </h2>
        <p className="pg-body-sm" style={{ maxWidth: 460 }}>
          The desk lost its uplink to the API. Nothing was executed — the deterministic path refuses to act on a
          stale read.
        </p>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, width: '100%', maxWidth: 460 }}>
          <div className="pg-inset" style={{ textAlign: 'left' }}>
            <Label>Error code</Label>
            <div className="pg-num pg-truncate" style={{ fontSize: 14, marginTop: 6, color: 'var(--pg-error)' }}>
              {code}
            </div>
          </div>
          <div className="pg-inset" style={{ textAlign: 'left' }}>
            <Label>Node status</Label>
            <div className="pg-num pg-truncate" style={{ fontSize: 14, marginTop: 6 }}>
              {node}
            </div>
          </div>
        </div>

        {onRetry ? (
          <Button kind="primary" onClick={onRetry} ariaLabel="Retry the request">
            Retry
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function WifiOffGlyph() {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
      <path d="M2 2l20 20" strokeLinecap="round" />
      <path d="M8.5 16.4a5 5 0 017 0" strokeLinecap="round" />
      <path d="M5 12.9a10 10 0 014.2-2.5" strokeLinecap="round" />
      <path d="M19 12.9a10 10 0 00-4.6-2.6" strokeLinecap="round" />
      <path d="M1.8 9.4A15 15 0 016 6.9" strokeLinecap="round" />
      <path d="M22.2 9.4A15 15 0 0011.4 5.1" strokeLinecap="round" />
      <circle cx="12" cy="20" r="1" fill="currentColor" stroke="none" />
    </svg>
  );
}

/* ── Stat tile ───────────────────────────────────────────────────── */

export function StatTile({
  label,
  value,
  caption,
  tone = 'neutral',
  loading = false,
  accessory,
}: {
  label: string;
  value: ReactNode;
  caption?: ReactNode;
  tone?: Tone;
  loading?: boolean;
  accessory?: ReactNode;
}) {
  return (
    <Card variant="dense" style={{ gap: 10, justifyContent: 'space-between' }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Label>{label}</Label>
        {accessory}
      </Row>
      {loading ? (
        <Skel h={28} w="60%" />
      ) : (
        <Numeral size={28} tone={tone}>
          {value}
        </Numeral>
      )}
      {loading ? <Skel h={11} w="80%" /> : caption ? <span className="pg-caption pg-truncate">{caption}</span> : null}
    </Card>
  );
}

/* ── Page heading ────────────────────────────────────────────────── */

export function PageHead({
  title,
  sub,
  right,
}: {
  title: string;
  sub?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <Row style={{ justifyContent: 'space-between', alignItems: 'flex-end', gap: 20, flexWrap: 'wrap' }}>
      <Stack gap={6}>
        <h1 className="pg-h2">{title}</h1>
        {sub ? <span className="pg-body-sm">{sub}</span> : null}
      </Stack>
      {right}
    </Row>
  );
}
