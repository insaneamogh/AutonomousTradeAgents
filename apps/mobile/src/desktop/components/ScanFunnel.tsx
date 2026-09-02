/**
 * Symbol Scan Funnel — eligible universe -> examined this sweep ->
 * cleared the deterministic math -> admitted to the LLM
 * (docs/PLAN_1000_SYMBOL_SCAN.md's tiered shape).
 *
 * A DIFFERENT question from ContractFunnel: that one shows which
 * CONTRACT survived, for one symbol that already reached a paid pass.
 * This one shows how many SYMBOLS ever reach a paid pass at all. Do not
 * conflate the two — they read from different endpoints, on different
 * cadences (once-daily universe refresh vs. the most recent sweep).
 *
 * Presentational only, like ContractFunnel — no data fetching.
 */

import type { ScanFunnelSweepDto, ScanFunnelUniverseDto } from '@app/shared-types';

import { Card, CardHead, Pill, Row, SkelRows, Stack } from '../primitives';
import { ago } from '../format';
import { firstZeroStageKey, fmtInt, FunnelBarRow } from './FunnelBar';
import type { FunnelBarDatum } from './FunnelBar';

export interface ScanFunnelProps {
  universe: ScanFunnelUniverseDto;
  sweep: ScanFunnelSweepDto | null;
  loading?: boolean;
}

// Not a closed set (see daily_cron.py's rolled_up skip_reason values) —
// an unmapped reason still renders, just as its raw string.
const CAPPED_REASON_LABEL: Record<string, string> = {
  below_min_llm_score: 'Below the minimum LLM score',
  llm_symbol_cap_reached: 'Per-sweep LLM symbol cap reached',
  llm_daily_symbol_cap_reached: 'Daily LLM symbol cap reached',
  llm_hourly_symbol_cap_reached: 'Hourly LLM symbol cap reached',
  llm_daily_budget_exhausted: 'Daily LLM budget exhausted',
};

function cappedReasonLabel(reason: string): string {
  return CAPPED_REASON_LABEL[reason] ?? reason;
}

export function ScanFunnel({ universe, sweep, loading = false }: ScanFunnelProps) {
  const isEmpty = !loading && sweep === null;

  // Tier 0 (eligible universe) can be legitimately absent even while
  // sweeps keep running (UNIVERSE_REFRESH_ENABLED=0) — never fabricate
  // it as a zero-width bar; fall back to the sweep's own base instead.
  const potentialBars: { key: string; label: string; value: number }[] = [];
  if (universe.eligibleCount != null) {
    potentialBars.push({ key: 'eligible_universe', label: 'Eligible universe', value: universe.eligibleCount });
  }
  if (sweep) {
    potentialBars.push(
      { key: 'examined_this_sweep', label: 'Examined this sweep', value: sweep.watchlistSize },
      { key: 'cleared_math', label: 'Cleared the math', value: sweep.clearedMath },
      { key: 'admitted_to_llm', label: 'Admitted to the LLM', value: sweep.admittedToLlm },
    );
  }
  const rows: FunnelBarDatum[] = potentialBars.map((bar, i) => ({
    key: bar.key,
    label: bar.label,
    survivors: bar.value,
    dropped: i === 0 ? 0 : Math.max(0, potentialBars[i - 1].value - bar.value),
  }));
  const base = rows[0]?.survivors ?? 0;
  const zeroKey = firstZeroStageKey(rows);

  const cappedEntries = sweep ? Object.entries(sweep.cappedBreakdown) : [];

  return (
    <Card>
      <CardHead
        label="Symbol scan funnel"
        right={
          sweep?.kind === 'triggered' ? (
            <Pill tone="warn" title="A triggered loop's watchlist is 1-3 symbols by design, not a full sweep">
              TRIGGERED · NARROW SAMPLE
            </Pill>
          ) : sweep?.kind === 'baseline' ? (
            <Pill>BASELINE SWEEP</Pill>
          ) : null
        }
      />

      {loading ? (
        <SkelRows rows={4} h={18} />
      ) : isEmpty ? (
        <EmptyState />
      ) : (
        <Stack gap={16}>
          <Stack gap={10}>
            {rows.map((r, i) => (
              <FunnelBarRow key={r.key} stage={r} index={i} base={base} scale="linear" isZero={r.key === zeroKey} />
            ))}
            {universe.eligibleCount != null && universe.examinedCount != null ? (
              <span className="pg-caption pg-dim" style={{ paddingLeft: 162 }}>
                {`${fmtInt(universe.examinedCount)} examined in the most recent daily refresh · refreshed ${ago(universe.refreshedAt)}`}
              </span>
            ) : null}
          </Stack>

          {cappedEntries.length > 0 ? (
            <Stack gap={6} style={{ borderTop: '1px solid var(--pg-card-border)', paddingTop: 10 }}>
              <span className="pg-caption pg-dim">Cleared the math but didn&rsquo;t reach the LLM</span>
              {cappedEntries.map(([reason, count]) => (
                <Row key={reason} style={{ justifyContent: 'space-between' }}>
                  <span className="pg-body-sm">{cappedReasonLabel(reason)}</span>
                  <span className="pg-num pg-dim">{fmtInt(count)}</span>
                </Row>
              ))}
            </Stack>
          ) : null}
        </Stack>
      )}
    </Card>
  );
}

function EmptyState() {
  return (
    <div className="pg-empty">
      <p className="pg-empty-title">No sweep has completed yet.</p>
      <p className="pg-empty-body">
        Nothing has scored a watchlist here yet — wait for the next scheduled sweep, or confirm
        COUNCIL_SCHEDULER_ENABLED is set.
      </p>
    </div>
  );
}
