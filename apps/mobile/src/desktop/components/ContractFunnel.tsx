/**
 * Contract Funnel — stepped horizontal bars showing how many candidate
 * contracts survived each selection stage
 * (packages/engine/engine/options/selection.py::select_contract).
 *
 * Deliberately STEPPED BARS, not a Sankey (docs/IMPL_CONTRACT_FUNNEL_UI.md
 * §4): a Sankey needs a layout library, reads worse at 7 stages, and costs
 * a day. Pure CSS here.
 *
 * Presentational only — no data fetching. Two callers feed it:
 *  - Insights screen passes the window's aggregate (summed) stages plus
 *    windowDays/runs/bought for the header and footer.
 *  - Decisions screen passes ONE run's stages plus its own
 *    rejectionReason/rejectionStage — the HOLD case this view exists for.
 */

import { useState } from 'react';
import type { FunnelStageDto } from '@app/shared-types';

import { Card, CardHead, Pill, Row, SkelRows, Stack } from '../primitives';
import { firstZeroStageKey, fmtInt, FunnelBarRow } from './FunnelBar';
import type { FunnelScale as Scale } from './FunnelBar';

export interface ContractFunnelProps {
  /** Fixed order — matches `_STAGE_REJECTION_REASONS`'s insertion order.
   * Rendered exactly as given; never re-sorted here. */
  stages: FunnelStageDto[];
  loading?: boolean;
  /** Aggregate-only header/footer context (Insights screen). Omit these
   * three for a single-run render (Decisions screen). */
  windowDays?: number;
  runs?: number;
  bought?: number;
  /** Single-run context (Decisions screen). */
  rejectionReason?: string | null;
  rejectionStage?: string | null;
  symbol?: string;
  selectedOcc?: string | null;
}

// Keyed on the exact `rejection_reason` strings `_STAGE_REJECTION_REASONS`
// emits (packages/engine/engine/options/selection.py:180-187) — a lookup
// table, not free text generation. The interpolated count is the dropped
// stage's own `dropped` figure, which by construction equals the PRECEDING
// stage's survivor count (dropped = prev survivors - 0 at the stage that
// emptied the funnel).
const REASON_STAGE_KEY: Record<string, string> = {
  no_matching_contract_type: 'contract_type',
  no_expiry_in_window: 'dte_window',
  no_delta_in_band: 'delta_band',
  no_liquid_contract: 'liquidity',
  no_iv: 'iv_present',
  iv_outside_plausible_band: 'iv_realized_vol_band',
};

const REJECTION_SENTENCE: Record<string, (prevSurvivors: number) => string> = {
  no_matching_contract_type: (n) =>
    `${fmtInt(n)} contracts were in the chain; none matched the contract type this thesis wanted (a call for bullish, a put for bearish).`,
  no_expiry_in_window: (n) =>
    `${fmtInt(n)} contracts were the right type; none had an expiry in the 10–45 day window.`,
  no_delta_in_band: (n) =>
    `${fmtInt(n)} contracts were in the DTE window; none had a delta in the target band (0.35–0.75 for high-conviction setups, 0.25–0.65 otherwise).`,
  no_liquid_contract: (n) =>
    `${fmtInt(n)} contracts were in the delta band; none cleared the liquidity floor (open interest of at least 100, spread no wider than 12%).`,
  no_iv: (n) => `${fmtInt(n)} contracts were liquid; none had implied volatility reported.`,
  iv_outside_plausible_band: (n) =>
    `${fmtInt(n)} contracts had IV reported; none fell inside a plausible band versus realized volatility.`,
};

function rejectionSentence(reason: string, stages: FunnelStageDto[]): string {
  if (reason === 'no_candidates') {
    return 'The options chain fetch returned no candidates at all — nothing reached the first filter.';
  }
  const stageKey = REASON_STAGE_KEY[reason];
  const stage = stages.find((s) => s.key === stageKey);
  const build = REJECTION_SENTENCE[reason];
  if (!stage || !build) {
    return 'No contract cleared every filter this pass.';
  }
  return build(stage.dropped);
}

export function ContractFunnel({
  stages,
  loading = false,
  windowDays,
  runs,
  bought,
  rejectionReason,
  rejectionStage,
  symbol,
  selectedOcc,
}: ContractFunnelProps) {
  const [scale, setScale] = useState<Scale>('linear');

  const isAggregate = windowDays != null;
  const isEmpty = !loading && (stages.length === 0 || (isAggregate && (runs ?? 0) === 0));
  const base = stages[0]?.survivors ?? 0;
  const zeroKey = rejectionStage ?? firstZeroStageKey(stages);

  return (
    <Card>
      <CardHead
        label={isAggregate ? 'Contracts considered' : 'Contract funnel'}
        right={
          <Row gap={12}>
            {isAggregate ? (
              <span className="pg-caption pg-num">
                {`${windowDays}d · ${runs ?? 0} run${(runs ?? 0) === 1 ? '' : 's'} · ${bought ?? 0} bought`}
              </span>
            ) : symbol ? (
              <Pill tone={rejectionReason ? 'bear' : 'bull'}>{symbol}</Pill>
            ) : null}
            {!isEmpty ? <ScaleToggle value={scale} onChange={setScale} /> : null}
          </Row>
        }
      />

      {loading ? (
        <SkelRows rows={7} h={18} />
      ) : isEmpty ? (
        <EmptyState />
      ) : (
        <Stack gap={16}>
          {rejectionReason ? <HoldBanner reason={rejectionReason} stages={stages} /> : null}

          <Stack gap={10}>
            {stages.map((s, i) => (
              <FunnelBarRow key={s.key} stage={s} index={i} base={base} scale={scale} isZero={s.key === zeroKey} />
            ))}
          </Stack>

          {isAggregate ? (
            <Row style={{ justifyContent: 'flex-end', borderTop: '1px solid var(--pg-card-border)', paddingTop: 10 }}>
              <span className="pg-num" style={{ fontWeight: 600, letterSpacing: '0.02em' }}>
                {`WE BOUGHT ${bought ?? 0}`}
              </span>
            </Row>
          ) : selectedOcc ? (
            <Row style={{ justifyContent: 'flex-end' }}>
              <Pill tone="bull">{selectedOcc}</Pill>
            </Row>
          ) : null}
        </Stack>
      )}
    </Card>
  );
}

function HoldBanner({ reason, stages }: { reason: string; stages: FunnelStageDto[] }) {
  return (
    <div className="pg-inset" style={{ borderColor: 'var(--pg-bear)' }} role="alert">
      <Row gap={8}>
        <Pill tone="bear">HELD</Pill>
        <span className="pg-num" style={{ fontSize: 13, color: 'var(--pg-bear-text)' }}>
          {reason}
        </span>
      </Row>
      <span className="pg-body-sm" style={{ display: 'block', marginTop: 6 }}>
        {rejectionSentence(reason, stages)}
      </span>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="pg-empty">
      <p className="pg-empty-title">No options passes yet in this window.</p>
      <p className="pg-empty-body">
        Nothing has run the contract funnel here yet — approve an options watchlist symbol or wait for the next
        sweep.
      </p>
    </div>
  );
}

function ScaleToggle({ value, onChange }: { value: Scale; onChange: (s: Scale) => void }) {
  return (
    <Row gap={6}>
      {(['linear', 'log'] as const).map((s) => (
        <button
          key={s}
          type="button"
          className={`pg-btn pg-btn-${value === s ? 'primary' : 'secondary'} pg-btn-sm`}
          onClick={() => onChange(s)}
          aria-pressed={value === s}
          aria-label={`${s === 'linear' ? 'Linear' : 'Log'} scale`}
        >
          {s === 'linear' ? 'Linear' : 'Log'}
        </button>
      ))}
    </Row>
  );
}
