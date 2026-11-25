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

import { scoreBand } from './theme';

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
  serif = false,
  style,
}: {
  children: ReactNode;
  size?: number;
  tone?: Tone;
  weight?: number;
  /** Newsreader display numerals: for the big figures only (>= 24px). */
  serif?: boolean;
  style?: CSSProperties;
}) {
  const color =
    tone === 'bull' ? 'var(--pg-bull-text)' : tone === 'bear' ? 'var(--pg-bear-text)' : 'var(--pg-on-surface)';
  return (
    <span
      className={serif ? 'pg-serif' : 'pg-num'}
      style={{ fontSize: size, lineHeight: 1.05, ...(serif ? {} : { fontWeight: weight }), color, ...style }}
    >
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
 * Conviction / score as a small ring: clay arc, centred numeral.
 * The band name is in the title + aria, so colour is never the only signal.
 * `ScorePill` keeps its old name so every existing call site gets the ring.
 */
export function ScorePill({ score, suffix = '' }: { score: number; suffix?: string }) {
  const band = scoreBand(score);
  return (
    <span
      title={`${band} · ${Math.round(score)}/100`}
      aria-label={`${band}, ${Math.round(score)} out of 100`}
      role="img"
      style={{ display: 'inline-flex', flex: 'none' }}
    >
      <ScoreRing score={score} size={36} suffix={suffix} />
    </span>
  );
}

/** Thin score bar, accent fill. */
export function ScoreBar({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(100, score));
  return (
    <div className="pg-bar" role="presentation">
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

/* ── Rings ───────────────────────────────────────────────────────── */

const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

/**
 * Small full ring (default 36px). The numeral is an SVG <text> centred with
 * `dominant-baseline: central`, so it sits on the ring's true centre
 * instead of drifting with font metrics the way an HTML overlay does.
 */
export function ScoreRing({
  score,
  size = 36,
  suffix = '',
}: {
  score: number;
  size?: number;
  suffix?: string;
}) {
  const stroke = Math.max(3, Math.round(size / 10));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const half = size / 2;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
      <circle cx={half} cy={half} r={r} fill="none" strokeWidth={stroke} style={{ stroke: 'var(--pg-track)' }} />
      <circle
        cx={half}
        cy={half}
        r={r}
        fill="none"
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${(c * clamp01(score / 100)).toFixed(2)} ${c.toFixed(2)}`}
        transform={`rotate(-90 ${half} ${half})`}
        style={{ stroke: 'var(--pg-accent)' }}
      />
      <text
        x={half}
        y={half + 0.5}
        textAnchor="middle"
        dominantBaseline="central"
        style={{
          fill: 'var(--pg-on-surface)',
          fontFamily: 'var(--pg-font-num)',
          fontWeight: 600,
          fontSize: Math.round(size / 3),
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {Math.round(score)}
        {suffix}
      </text>
    </svg>
  );
}

/**
 * 270° gauge for the headline score (conviction, hit rate, agreement).
 * Open at the bottom, serif numeral in the middle, optional caption in
 * the opening.
 */
export function Gauge({
  value,
  size = 72,
  label,
  suffix = '',
}: {
  value: number;
  size?: number;
  label?: string;
  suffix?: string;
}) {
  const stroke = Math.max(4, Math.round(size / 13));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const sweep = c * 0.75;
  const half = size / 2;
  const numSize = Math.round(size * (label ? 0.31 : 0.36));
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={`${Math.round(value)}${suffix}${label ? ` ${label}` : ''}`}
    >
      <circle
        cx={half}
        cy={half}
        r={r}
        fill="none"
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${sweep.toFixed(2)} ${c.toFixed(2)}`}
        transform={`rotate(135 ${half} ${half})`}
        style={{ stroke: 'var(--pg-track)' }}
      />
      <circle
        cx={half}
        cy={half}
        r={r}
        fill="none"
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${(sweep * clamp01(value / 100)).toFixed(2)} ${c.toFixed(2)}`}
        transform={`rotate(135 ${half} ${half})`}
        style={{ stroke: 'var(--pg-accent)' }}
      />
      <text
        x={half}
        y={label ? half - size * 0.04 : half + 1}
        textAnchor="middle"
        dominantBaseline="central"
        style={{
          fill: 'var(--pg-on-surface)',
          fontFamily: 'var(--pg-font-serif)',
          fontSize: numSize,
          letterSpacing: '-0.02em',
        }}
      >
        {Math.round(value)}
        {suffix}
      </text>
      {label ? (
        <text
          x={half}
          y={half + size * 0.24}
          textAnchor="middle"
          dominantBaseline="central"
          style={{ fill: 'var(--pg-outline)', fontFamily: 'var(--pg-font-sans)', fontSize: 12, fontWeight: 500 }}
        >
          {label}
        </text>
      ) : null}
    </svg>
  );
}

/* ── Money, with cents baseline-aligned and quieter ─────────────── */

/** `$100,412` big with `.30` at half size on the same baseline. */
export function Money({
  value,
  size = 64,
  tone = 'neutral',
}: {
  value: number;
  size?: number;
  tone?: Tone;
}) {
  const abs = Math.abs(value);
  const [whole, cents] = abs.toFixed(2).split('.');
  const sign = value < 0 ? '−' : '';
  const color =
    tone === 'bull' ? 'var(--pg-bull-text)' : tone === 'bear' ? 'var(--pg-bear-text)' : 'var(--pg-on-surface)';
  return (
    <span className="pg-serif pg-money" style={{ fontSize: size, lineHeight: 1, color }}>
      {sign}${Number(whole).toLocaleString('en-US')}
      <s>.{cents}</s>
    </span>
  );
}

/* ── Company tile ────────────────────────────────────────────────── */

/**
 * Brand-hued monogram tile. We have no licence to ship company logos, so
 * the tile is a flat colour keyed off the ticker; known tickers get their
 * recognisable hue, anything else gets a stable muted hue from a hash.
 */
const TICKER_HUE: Record<string, string> = {
  NVDA: '#587a2e',
  AMD: '#41444d',
  MSFT: '#3c6fa8',
  META: '#4a5fb8',
  COIN: '#3659b8',
  TSLA: '#a34853',
  XOM: '#a34a4a',
  AAPL: '#5b5f66',
  GOOGL: '#3d7a6a',
  AMZN: '#a8742f',
};

function tickerColor(symbol: string): string {
  const known = TICKER_HUE[symbol];
  if (known) return known;
  let h = 0;
  for (let i = 0; i < symbol.length; i += 1) h = (h * 31 + symbol.charCodeAt(i)) % 360;
  return `hsl(${h} 32% 38%)`;
}

export function TickerTile({ symbol, size = 36 }: { symbol: string; size?: number }) {
  const clean = symbol.replace(/\d.*$/, '').toUpperCase();
  const text = clean.length <= 3 ? clean : clean.slice(0, 2);
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        borderRadius: Math.round(size * 0.28),
        background: tickerColor(clean),
        color: '#f4f1ea',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        flex: 'none',
        fontWeight: 600,
        fontSize: Math.max(10, Math.round(size / (text.length > 2 ? 3.6 : 3.1))),
        lineHeight: 1,
        letterSpacing: '0.02em',
      }}
    >
      {text}
    </span>
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
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  kind?: 'primary' | 'secondary' | 'ghost';
  size?: 'sm';
  disabled?: boolean;
  ariaLabel?: string;
  style?: CSSProperties;
  type?: 'button' | 'submit';
  /** Native browser tooltip on hover — e.g. why a disabled button is
   * disabled. Optional; most callers don't need it. */
  title?: string;
}) {
  const cls = ['pg-btn', `pg-btn-${kind}`, size === 'sm' ? 'pg-btn-sm' : ''].filter(Boolean).join(' ');
  return (
    <button
      type={type}
      className={cls}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      style={style}
      title={title}
    >
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
