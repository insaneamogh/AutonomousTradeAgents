/**
 * Home tiles — the first thing on the Dashboard.
 *
 * Principles taken from a reference product (calm, few colours, very large
 * numerals, one fact per tile, dots where a count is the point, segmented
 * switches instead of menus, small purposeful motion). The look is this
 * app's own Platinum Glass tokens: no hex in here, only `var(--pg-*)`.
 *
 * Every tile degrades on its own. A failed or missing endpoint prints a
 * plain "unavailable" in that tile and never throws, because the autonomy
 * and trading-mode endpoints are newer than some deployed servers.
 */

import { useEffect, useState } from 'react';
import type { VetoRuleDto } from '@app/shared-types';

import { useAccount } from '@/hooks/useAccount';
import { usePendingApprovals } from '@/hooks/useApprovals';
import { useAutonomy } from '@/hooks/useAutonomy';
import type { AutonomyCheck, AutonomyState, AutonomyStatus } from '@/hooks/useAutonomy';
import { useGhostSummary, useVetoLedger } from '@/hooks/useInsights';
import type { InsightsMarket } from '@/hooks/useInsights';
import { useOpenPositions } from '@/hooks/usePositions';
import { useTradingMode } from '@/hooks/useTradingMode';

import { money, signedPct, signedUsd, tone as toneOf } from '../format';
import type { Currency } from '../format';
import { useNav } from '../nav';
import { Pill, Skel } from '../primitives';
import type { Tone } from '../primitives';

/* ── small shared pieces ─────────────────────────────────────────── */

/** A segmented switch (instead of a menu). 44px targets. */
export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="pg-seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          className="pg-seg-btn"
          aria-pressed={value === o.id}
          onClick={() => onChange(o.id)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Tile({
  label,
  span = 1,
  onOpen,
  openLabel,
  expanded,
  children,
}: {
  label: string;
  span?: 1 | 2;
  onOpen?: () => void;
  openLabel?: string;
  expanded?: boolean;
  children: React.ReactNode;
}) {
  const interactive = onOpen != null;
  return (
    <div
      className={`pg-card pg-tile${interactive ? ' pg-tile--tap' : ''}`}
      data-span={span}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-label={interactive ? (openLabel ?? label) : undefined}
      aria-expanded={interactive && expanded != null ? expanded : undefined}
      onClick={onOpen}
      onKeyDown={
        interactive
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onOpen();
              }
            }
          : undefined
      }
    >
      <span className="label-caps">{label}</span>
      {children}
    </div>
  );
}

function bigTone(t: Tone): string {
  return t === 'bull' ? 'var(--pg-bull-text)' : t === 'bear' ? 'var(--pg-bear-text)' : 'var(--pg-on-surface)';
}

/* ── Autonomy ────────────────────────────────────────────────────── */

const VERDICT: Record<AutonomyState, { word: string; tone: Tone }> = {
  running: { word: 'RUNNING', tone: 'bull' },
  idle: { word: 'IDLE', tone: 'neutral' },
  blocked: { word: 'BLOCKED', tone: 'bear' },
};

/** Failing checks first, so the thing to fix is at the top of the list. */
export function orderChecks(checks: AutonomyCheck[]): AutonomyCheck[] {
  return [...checks].sort((a, b) => Number(a.ok) - Number(b.ok));
}

export function AutonomyTileView({
  status,
  loading,
  failed,
}: {
  status: AutonomyStatus | undefined;
  loading: boolean;
  failed: boolean;
}) {
  const [open, setOpen] = useState(false);

  if (loading) {
    return (
      <Tile label="Autonomy" span={2}>
        <Skel h={44} w="50%" />
        <Skel h={12} w="80%" />
      </Tile>
    );
  }
  // Errored or 404 (older server): say so, never throw.
  if (failed || !status || !VERDICT[status.state]) {
    return (
      <Tile label="Autonomy" span={2}>
        <span className="pg-tile-word pg-dim">—</span>
        <span className="pg-caption">status unavailable</span>
      </Tile>
    );
  }

  const v = VERDICT[status.state];
  const checks = orderChecks(Array.isArray(status.checks) ? status.checks : []);
  return (
    <Tile
      label="Autonomy"
      span={2}
      onOpen={() => setOpen((o) => !o)}
      openLabel={`Autonomy: ${v.word}. ${open ? 'Hide' : 'Show'} checklist`}
      expanded={open}
    >
      <span className="pg-tile-word" style={{ color: bigTone(v.tone) }}>
        {v.word}
      </span>
      <span className="pg-body-sm">{status.headline}</span>
      {checks.length > 0 ? (
        <div className="pg-dots pg-dots--sm" role="img" aria-label={`${checks.filter((c) => c.ok).length} of ${checks.length} checks passing`}>
          {checks.map((c, i) => (
            <i
              key={c.id}
              data-kind={c.ok ? 'ok' : c.severity === 'blocker' ? 'fail' : 'warn'}
              style={{ ['--i' as string]: i }}
            />
          ))}
        </div>
      ) : null}
      {open ? (
        <ul className="pg-checklist" onClick={(e) => e.stopPropagation()}>
          {checks.map((c) => (
            <li key={c.id} data-ok={c.ok ? 'true' : 'false'}>
              <span className="pg-checklist-label">
                <span aria-hidden>{c.ok ? '✓' : c.severity === 'blocker' ? '✕' : '!'}</span> {c.label}
              </span>
              <span className="pg-caption">{c.detail}</span>
              {!c.ok && c.fix ? (
                <span className="pg-checklist-fix">
                  <b>Fix:</b> {c.fix}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </Tile>
  );
}

function AutonomyTile() {
  const q = useAutonomy();
  return <AutonomyTileView status={q.data} loading={q.isLoading} failed={q.isError} />;
}

/* ── Next sweep ──────────────────────────────────────────────────── */

export function countdown(targetIso: string, nowMs: number): string {
  const ms = Date.parse(targetIso) - nowMs;
  if (Number.isNaN(ms)) return '—';
  if (ms <= 0) return 'due now';
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  return `${m}m ${String(sec).padStart(2, '0')}s`;
}

function SweepTile({ nextSweepAt }: { nextSweepAt: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <Tile label="Next sweep">
      <span className="pg-tile-num pg-num">{countdown(nextSweepAt, now)}</span>
      <span className="pg-caption">the desk looks for picks</span>
    </Tile>
  );
}

/* ── Refusal Ledger dot grid ─────────────────────────────────────── */

export type DotKind = 'avoided' | 'blocked' | 'pending';

/** Draw at most this many dots; the rest are summarised as "+N". */
export const MAX_DOTS = 240;

/**
 * One dot per refusal.
 *
 * The ledger endpoint does NOT return per-refusal rows, only per-rule
 * aggregates: `count`, `markedCount` (how many already have a mark) and the
 * two-sided USD split of those marks (`lossAvoidedUsd`, `upsideBlockedUsd`).
 * So the dots are DRAWN FROM THE PER-RULE COUNTS: unmarked refusals are
 * pending (grey); the marked ones are split between "avoided a loss" and
 * "blocked upside" in proportion to the two USD magnitudes (at least one of
 * each when both exist). The totals beside the grid are the real numbers;
 * the colour split is an apportionment. When per-refusal rows ship, replace
 * this with a direct map.
 */
export function ledgerDots(rules: Pick<VetoRuleDto, 'count' | 'markedCount' | 'lossAvoidedUsd' | 'upsideBlockedUsd' | 'markedPnl'>[]): DotKind[] {
  let avoided = 0;
  let blocked = 0;
  let pending = 0;
  for (const r of rules) {
    const count = Math.max(0, Math.floor(r.count ?? 0));
    const marked = Math.min(count, Math.max(0, Math.floor(r.markedCount ?? 0)));
    const loss = Math.max(0, r.lossAvoidedUsd ?? 0);
    const up = Math.max(0, r.upsideBlockedUsd ?? 0);
    let a = 0;
    let b = 0;
    if (marked > 0 && loss + up > 0) {
      a = Math.round((marked * loss) / (loss + up));
      if (loss > 0 && up > 0 && marked >= 2) a = Math.min(marked - 1, Math.max(1, a));
      b = marked - a;
    } else if (marked > 0 && r.markedPnl != null && r.markedPnl !== 0) {
      if (r.markedPnl < 0) a = marked;
      else b = marked;
    }
    avoided += a;
    blocked += b;
    pending += count - a - b; // marked-but-unclassifiable dots stay grey
  }
  return [
    ...Array<DotKind>(avoided).fill('avoided'),
    ...Array<DotKind>(blocked).fill('blocked'),
    ...Array<DotKind>(pending).fill('pending'),
  ];
}

export function LedgerDotGrid({ dots }: { dots: DotKind[] }) {
  const shown = dots.slice(0, MAX_DOTS);
  const hidden = dots.length - shown.length;
  const n = (k: DotKind) => dots.filter((d) => d === k).length;
  return (
    <>
      <div
        className="pg-dots"
        role="img"
        aria-label={`${dots.length} refusals: ${n('avoided')} would have lost, ${n('blocked')} would have made money, ${n('pending')} pending`}
      >
        {shown.map((kind, i) => (
          <i key={i} data-kind={kind} style={{ ['--i' as string]: Math.min(i, 60) }} />
        ))}
      </div>
      {hidden > 0 ? <span className="pg-caption">+{hidden} more not drawn</span> : null}
    </>
  );
}

function LedgerTile() {
  const [market, setMarket] = useState<InsightsMarket>('US');
  const ledger = useVetoLedger(30, market);
  const ghost = useGhostSummary(30, market);
  const currency: Currency = market === 'IN' ? 'INR' : 'USD';

  const vetoed = ghost.data?.vetoed;
  const avoided = vetoed?.lossAvoidedUsd ?? ghost.data?.savedUsd ?? 0;
  const upside = vetoed?.upsideBlockedUsd ?? 0;

  let body: React.ReactNode;
  if (ledger.isLoading) {
    body = <Skel h={72} />;
  } else if (ledger.isError || !ledger.data) {
    body = <span className="pg-caption">ledger unavailable</span>;
  } else if (ledger.data.totalVetoes === 0) {
    body = <span className="pg-caption">No refusals in the last 30 days.</span>;
  } else {
    const rules = ledger.data.rules ?? [];
    const dots = rules.length > 0 ? ledgerDots(rules) : Array<DotKind>(ledger.data.totalVetoes).fill('pending');
    body = <LedgerDotGrid dots={dots} />;
  }

  return (
    <Tile label="Refusal Ledger · 30d" span={2}>
      <Segmented
        label="Currency"
        value={market}
        onChange={setMarket}
        options={[
          { id: 'US', label: 'USD' },
          { id: 'IN', label: 'INR' },
        ]}
      />
      {body}
      <div className="pg-legend">
        <span>
          <i data-kind="avoided" /> would have lost
        </span>
        <span>
          <i data-kind="blocked" /> would have made money
        </span>
        <span>
          <i data-kind="pending" /> pending
        </span>
      </div>
      <div className="pg-ledger-totals">
        <div>
          <span className="label-caps">Losses avoided</span>
          <span className="pg-tile-num pg-num" style={{ color: 'var(--pg-bull-text)' }}>
            {ghost.data ? money(avoided, currency) : '—'}
          </span>
        </div>
        <div>
          <span className="label-caps">Upside blocked</span>
          <span className="pg-tile-num pg-num" style={{ color: 'var(--pg-warn)' }}>
            {ghost.data ? money(upside, currency) : '—'}
          </span>
        </div>
      </div>
    </Tile>
  );
}

/* ── Simple fact tiles ───────────────────────────────────────────── */

function EquityTile() {
  const account = useAccount();
  const a = account.data;
  return (
    <Tile label="Equity today">
      {account.isLoading ? (
        <Skel h={40} w="70%" />
      ) : a ? (
        <>
          <span className="pg-tile-num pg-num">{money(a.equity, 'USD')}</span>
          <span className="pg-caption" style={{ color: bigTone(toneOf(a.todayPnl)) }}>
            {signedUsd(a.todayPnl)} · {signedPct(a.todayPnlPct)}
          </span>
        </>
      ) : (
        <span className="pg-caption">unavailable</span>
      )}
    </Tile>
  );
}

function PositionsTile() {
  const positions = useOpenPositions();
  const { go } = useNav();
  return (
    <Tile label="Open positions" onOpen={() => go({ name: 'positions' })} openLabel="Open positions">
      {positions.isLoading ? (
        <Skel h={40} w="40%" />
      ) : positions.data ? (
        <span className="pg-tile-num pg-num">{positions.data.length}</span>
      ) : (
        <span className="pg-caption">unavailable</span>
      )}
    </Tile>
  );
}

function PendingTile() {
  const pending = usePendingApprovals();
  const { go } = useNav();
  const n = pending.data?.length;
  return (
    <Tile label="Pending picks" onOpen={() => go({ name: 'picks' })} openLabel="Pending picks, open Picks">
      {pending.isLoading ? (
        <Skel h={40} w="40%" />
      ) : n != null ? (
        <>
          <span className="pg-tile-num pg-num" style={{ color: n > 0 ? 'var(--pg-bull-text)' : undefined }}>
            {n}
          </span>
          <span className="pg-caption">{n > 0 ? 'awaiting you' : 'inbox clear'}</span>
        </>
      ) : (
        <span className="pg-caption">unavailable</span>
      )}
    </Tile>
  );
}

function ModeTile() {
  const mode = useTradingMode();
  const { go } = useNav();
  if (mode.isError) return null; // endpoint absent on older servers
  return (
    <Tile label="Trading mode" onOpen={() => go({ name: 'settings' })} openLabel="Trading mode, open Settings">
      {mode.isLoading || !mode.data ? (
        <Skel h={30} w="50%" />
      ) : (
        <>
          <span>
            <Pill tone={mode.data.mode === 'live' ? 'bear' : 'neutral'}>
              {mode.data.mode === 'live' ? 'LIVE' : 'PAPER'}
            </Pill>
          </span>
          <span className="pg-caption">{mode.data.mode === 'live' ? 'real money' : 'practice money'}</span>
        </>
      )}
    </Tile>
  );
}

export function HomeTiles() {
  const autonomy = useAutonomy();
  const next = autonomy.data?.nextSweepAt ?? null;
  return (
    <div className="pg-tiles pg-fade-up">
      <AutonomyTile />
      <EquityTile />
      <PositionsTile />
      <PendingTile />
      <ModeTile />
      {next ? <SweepTile nextSweepAt={next} /> : null}
      <LedgerTile />
    </div>
  );
}
