/**
 * Pick detail + approve.
 *
 * Layout: bull / bear glass tiles side by side, the deterministic
 * risk-check panel under them, the order plan and the decision bar on the
 * right rail.
 *
 * The Approve CTA is platinum/ink — NEVER green. Green is reserved for
 * fills and positive P&L. That rule holds in both design systems.
 */

import { useState } from 'react';

import { useAccount } from '@/hooks/useAccount';
import { useDecideApproval, usePendingApprovals } from '@/hooks/useApprovals';
import type { DecisionResponse, ExitMode } from '@app/shared-types';

import { ago, humanize, ruleLabel, usd } from '../format';
import { useNav } from '../nav';
import { IconBack, IconCheck, IconCross, IconShield } from '../icons';
import {
  Button,
  Card,
  CardHead,
  Cell,
  DataStreamInterrupted,
  Label,
  Numeral,
  Pill,
  Row,
  ScoreBar,
  ScorePill,
  Skel,
  Stack,
} from '../primitives';

export function PickDetailScreen({ id }: { id: string }) {
  const pending = usePendingApprovals();
  const account = useAccount();
  const decide = useDecideApproval();
  const { back, go } = useNav();
  const [exitMode, setExitMode] = useState<ExitMode>('agent');
  const [result, setResult] = useState<DecisionResponse | null>(null);

  if (pending.isError) {
    return (
      <DataStreamInterrupted
        code="APPROVALS_READ_FAILED"
        node="api · /v1/approvals/pending"
        onRetry={() => void pending.refetch()}
      />
    );
  }

  const pick = pending.data?.find((p) => p.id === id) ?? null;

  if (pending.isLoading) {
    return (
      <Card>
        <Skel h={40} w="40%" />
        <Skel h={140} />
      </Card>
    );
  }

  // Decided (or expired) — the row is gone from the queue. Show the outcome.
  if (!pick) {
    return (
      <Stack gap={20}>
        <BackBar onBack={back} />
        <Card style={{ gap: 12 }}>
          <Label>Decision recorded</Label>
          <p className="pg-h3">
            {result?.outcome === 'approved' ? 'Approved' : result?.outcome === 'declined' ? 'Declined' : 'No longer pending'}
          </p>
          {result?.order ? (
            <div className="pg-inset">
              <Label>Order</Label>
              <div style={{ marginTop: 6 }}>
                <Numeral size={18}>
                  {result.order.side} {result.order.qty} {result.order.symbol} · {result.order.status}
                </Numeral>
              </div>
              <span className="pg-caption">
                {result.order.filledQty} filled
                {result.order.avgFillPrice != null ? ` @ ${usd(result.order.avgFillPrice, 2)}` : ''}
              </span>
            </div>
          ) : null}
          <Row gap={12}>
            <Button kind="primary" onClick={() => go({ name: 'picks' })} ariaLabel="Back to picks">
              Back to picks
            </Button>
            <Button onClick={() => go({ name: 'positions' })} ariaLabel="Open positions">
              Positions
            </Button>
          </Row>
        </Card>
      </Stack>
    );
  }

  const conviction = pick.convictionLevel * 20;
  const equity = account.data?.equity ?? null;
  const concentration = equity && equity > 0 ? (pick.estimatedNotional / equity) * 100 : null;
  const riskPerShare = pick.stopLoss != null && pick.limitPrice != null ? Math.abs(pick.limitPrice - pick.stopLoss) : null;
  // Options (Phase A) are always `side: "BUY"` (buying a call OR a put,
  // never selling to open) - side/isBuy can't distinguish a bullish call
  // from a bearish put, so every option-aware branch below reads
  // isOption/contractType instead.
  const isOption = pick.isOption === true;
  const contractLabel = isOption ? (pick.contractType ?? '').toUpperCase() : '';

  const submit = (outcome: 'approved' | 'declined') => {
    decide.mutate(
      { proposalId: pick.id, outcome, exitMode: outcome === 'approved' ? exitMode : undefined },
      { onSuccess: (res) => setResult(res) },
    );
  };

  return (
    <>
      <BackBar onBack={back} />

      <Row style={{ justifyContent: 'space-between', alignItems: 'flex-end', gap: 20, flexWrap: 'wrap' }}>
        <Stack gap={8}>
          <Row gap={14}>
            <span className="pg-h1 pg-num">{pick.symbol}</span>
            {isOption ? (
              <Pill tone={pick.contractType === 'put' ? 'bear' : 'bull'}>{contractLabel}</Pill>
            ) : (
              <Pill tone={pick.side === 'BUY' ? 'bull' : 'bear'}>{pick.side}</Pill>
            )}
            <ScorePill score={conviction} />
          </Row>
          <span className="pg-body-sm">
            {isOption
              ? `$${pick.strike?.toFixed(2) ?? '—'} strike · ${pick.qty} contract${pick.qty === 1 ? '' : 's'} · exp ${pick.expiryDate ?? '—'}`
              : `${pick.qty} shares · ${pick.orderType === 'LIMIT' && pick.limitPrice ? `limit ${usd(pick.limitPrice, 2)}` : 'market order'}`} ·
            proposed {ago(pick.proposedAt)}
          </span>
        </Stack>
        <Stack gap={4} style={{ alignItems: 'flex-end' }}>
          <Label>Estimated notional</Label>
          <Numeral size={36}>{usd(pick.estimatedNotional)}</Numeral>
        </Stack>
      </Row>

      <div className="pg-grid pg-fade-up">
        {/* ── Bull / bear tiles ───────────────────────────────── */}
        <Cell span={4}>
          <CaseTile title="Bull case" body={pick.bullCase} tone="bull" />
        </Cell>
        <Cell span={4}>
          <CaseTile title="Bear case" body={pick.bearCase} tone="bear" />
        </Cell>

        {/* ── Decision rail ───────────────────────────────────── */}
        <Cell span={4}>
          <Card style={{ gap: 16 }}>
            <CardHead label="Your decision" />

            <Stack gap={8}>
              <Label>Who owns the exit</Label>
              <Row gap={8}>
                {(['agent', 'manual'] as ExitMode[]).map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    className={`pg-btn pg-btn-${exitMode === mode ? 'primary' : 'secondary'} pg-btn-sm`}
                    onClick={() => setExitMode(mode)}
                    aria-pressed={exitMode === mode}
                    aria-label={`Exit handled by ${mode}`}
                    style={{ flex: 1 }}
                  >
                    {mode === 'agent' ? 'Agent' : 'Me'}
                  </button>
                ))}
              </Row>
              <span className="pg-caption">
                {exitMode === 'agent'
                  ? 'Bracket stop/target at the broker plus the time stop.'
                  : 'The agent never touches this position after the fill.'}
              </span>
            </Stack>

            {result?.riskBlocked ? (
              <div className="pg-inset" style={{ borderColor: 'var(--pg-bear)' }}>
                <Row gap={8}>
                  <span style={{ color: 'var(--pg-bear-text)' }}>
                    <IconShield size={15} />
                  </span>
                  <Label>Risk re-check refused</Label>
                </Row>
                <div className="pg-num pg-bear" style={{ marginTop: 6, fontSize: 13 }}>
                  {ruleLabel(result.riskVetoRule ?? 'veto')}
                </div>
                <span className="pg-caption">{result.riskReason ?? 'The pick stays pending — retry when it clears.'}</span>
              </div>
            ) : null}

            {decide.isError ? <span className="pg-body-sm pg-bear">Decision failed — try again.</span> : null}

            <Stack gap={10}>
              {/* Platinum/ink. Never green. */}
              <Button
                kind="primary"
                onClick={() => submit('approved')}
                disabled={decide.isPending}
                ariaLabel={
                  isOption
                    ? `Approve ${pick.qty} ${pick.symbol} ${contractLabel} $${pick.strike ?? ''}`
                    : `Approve ${pick.side} ${pick.qty} ${pick.symbol}`
                }
              >
                <IconCheck size={16} />
                {decide.isPending
                  ? 'Submitting…'
                  : isOption
                    ? `Approve ${pick.qty} ${pick.symbol} ${contractLabel} $${pick.strike ?? ''}`
                    : `Approve ${pick.side} ${pick.qty} ${pick.symbol}`}
              </Button>
              <Button
                onClick={() => submit('declined')}
                disabled={decide.isPending}
                ariaLabel={`Pass on ${pick.symbol}`}
              >
                <IconCross size={16} />
                Pass
              </Button>
            </Stack>

            <span className="pg-caption">
              Approving routes through the deterministic risk gate one more time before the order is sent.
            </span>
          </Card>
        </Cell>

        {/* ── Deterministic risk checks ───────────────────────── */}
        <Cell span={7}>
          <Card>
            <CardHead
              label="Deterministic risk checks"
              right={<Pill>PYTHON · NOT THE MODEL</Pill>}
            />
            <table className="pg-table">
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Reads</th>
                  <th className="pg-num-right">Value</th>
                </tr>
              </thead>
              <tbody>
                {isOption ? (
                  <>
                    <CheckRow
                      rule="options_premium_size"
                      reads="Sized from a premium-at-risk budget, not an ATR stop"
                      value={pick.ask != null ? usd(pick.ask, 2) : '—'}
                      ok={pick.ask != null}
                    />
                    <CheckRow
                      rule="illiquid_contract"
                      reads="Open interest / volume / spread floor at selection time"
                      value={pick.openInterest != null ? `${pick.openInterest.toLocaleString('en-US')} OI` : '—'}
                      ok={pick.openInterest != null}
                    />
                    <CheckRow
                      rule="iv_unavailable"
                      reads="Missing IV is a hard reject, not a neutral pass"
                      value={pick.impliedVolatility != null ? `${(pick.impliedVolatility * 100).toFixed(1)}% IV` : '—'}
                      ok={pick.impliedVolatility != null}
                    />
                    <CheckRow
                      rule="max_premium_pct"
                      reads="Premium as a share of equity"
                      value={concentration != null ? `${concentration.toFixed(2)}%` : '—'}
                      ok={concentration != null && concentration <= 2}
                    />
                    <CheckRow
                      rule="time_stop"
                      reads="Expiry sweep force-surfaces the position before assignment risk"
                      value={pick.timeStopDays != null ? `${pick.timeStopDays}d` : '—'}
                      ok={pick.timeStopDays != null}
                    />
                  </>
                ) : (
                  <>
                    <CheckRow
                      rule="atr_position_size"
                      reads="Stop placed off ATR, size derived from it"
                      value={pick.stopLoss != null ? usd(pick.stopLoss, 2) : '—'}
                      ok={pick.stopLoss != null}
                    />
                    <CheckRow
                      rule="reward_risk_floor"
                      reads="Target must clear the R-multiple floor"
                      value={pick.rMultiple != null ? `${pick.rMultiple.toFixed(2)}R` : '—'}
                      ok={pick.rMultiple != null && pick.rMultiple >= 1}
                    />
                    <CheckRow
                      rule="max_position_pct"
                      reads="Notional as a share of equity"
                      value={concentration != null ? `${concentration.toFixed(2)}%` : '—'}
                      ok={concentration != null && concentration <= 25}
                    />
                    <CheckRow
                      rule="time_stop"
                      reads="Agent flattens if neither stop nor target hits"
                      value={pick.timeStopDays != null ? `${pick.timeStopDays}d` : '—'}
                      ok={pick.timeStopDays != null}
                    />
                    <CheckRow
                      rule="risk_per_share"
                      reads="Entry to stop distance"
                      value={riskPerShare != null ? usd(riskPerShare, 2) : '—'}
                      ok={riskPerShare != null}
                    />
                  </>
                )}
              </tbody>
            </table>

            {pick.informationalFlags && pick.informationalFlags.length > 0 ? (
              <Stack gap={8}>
                <Label>Informational flags</Label>
                <Row gap={8} style={{ flexWrap: 'wrap' }}>
                  {pick.informationalFlags.map((flag) => (
                    <Pill key={flag} tone="bear" title={humanize(flag)}>
                      {ruleLabel(flag)}
                    </Pill>
                  ))}
                </Row>
                <span className="pg-caption">Non-blocking. The engine surfaced them; it did not veto on them.</span>
              </Stack>
            ) : null}
          </Card>
        </Cell>

        {/* ── Order plan + rationale ──────────────────────────── */}
        <Cell span={5}>
          <Stack gap={20} style={{ flex: 1 }}>
            <Card>
              <CardHead label="Order plan" />
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                {isOption ? (
                  <>
                    <PlanCell label="Contract" value={contractLabel || '—'} />
                    <PlanCell label="Quantity" value={`${pick.qty} contract${pick.qty === 1 ? '' : 's'}`} />
                    <PlanCell label="Strike" value={pick.strike != null ? usd(pick.strike, 2) : '—'} />
                    <PlanCell label="Expiry" value={pick.expiryDate ?? '—'} />
                    <PlanCell label="Premium (ask)" value={pick.ask != null ? usd(pick.ask, 2) : '—'} />
                    <PlanCell label="Stop / target" value="No bracket on options" />
                  </>
                ) : (
                  <>
                    <PlanCell label="Side" value={pick.side} />
                    <PlanCell label="Quantity" value={String(pick.qty)} />
                    <PlanCell label="Type" value={pick.orderType} />
                    <PlanCell label="Limit" value={pick.limitPrice != null ? usd(pick.limitPrice, 2) : 'Market'} />
                    <PlanCell label="Stop loss" value={pick.stopLoss != null ? usd(pick.stopLoss, 2) : '—'} />
                    <PlanCell label="Target" value={pick.targetPrice != null ? usd(pick.targetPrice, 2) : '—'} />
                  </>
                )}
              </div>
              <Stack gap={6}>
                <Row style={{ justifyContent: 'space-between' }}>
                  <Label>Risk level</Label>
                  <span className="pg-caption pg-num">{pick.riskLevel}/5</span>
                </Row>
                <ScoreBar score={(6 - pick.riskLevel) * 20} />
              </Stack>
            </Card>

            <Card>
              <CardHead label="Council rationale" />
              <p className="pg-body-md">{pick.rationale}</p>
            </Card>
          </Stack>
        </Cell>
      </div>
    </>
  );
}

function BackBar({ onBack }: { onBack: () => void }) {
  return (
    <Row>
      <Button kind="ghost" onClick={onBack} ariaLabel="Back">
        <IconBack size={16} />
        Back
      </Button>
    </Row>
  );
}

function CaseTile({ title, body, tone }: { title: string; body: string; tone: 'bull' | 'bear' }) {
  const accent = tone === 'bull' ? 'var(--pg-bull-text)' : 'var(--pg-bear-text)';
  const wash = tone === 'bull' ? 'var(--pg-bull-wash)' : 'var(--pg-bear-wash)';
  return (
    <Card style={{ gap: 12, backgroundImage: `linear-gradient(160deg, ${wash}, transparent 55%)` }}>
      <Row gap={8}>
        <span aria-hidden style={{ color: accent, fontSize: 12 }}>
          {tone === 'bull' ? '▲' : '▼'}
        </span>
        <Label style={{ color: accent }}>{title}</Label>
      </Row>
      <p className="pg-body-md">{body}</p>
    </Card>
  );
}

function PlanCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="pg-inset">
      <Label>{label}</Label>
      <div style={{ marginTop: 6 }}>
        <Numeral size={16}>{value}</Numeral>
      </div>
    </div>
  );
}

function CheckRow({
  rule,
  reads,
  value,
  ok,
}: {
  rule: string;
  reads: string;
  value: string;
  ok: boolean;
}) {
  return (
    <tr>
      <td>
        <Row gap={8}>
          <span aria-hidden style={{ color: ok ? 'var(--pg-bull-text)' : 'var(--pg-outline)', display: 'flex' }}>
            {ok ? <IconCheck size={14} /> : <IconCross size={14} />}
          </span>
          <span className="pg-num" style={{ fontSize: 13 }}>
            {ruleLabel(rule)}
          </span>
        </Row>
      </td>
      <td className="pg-body-sm">{reads}</td>
      <td className="pg-num-right">{value}</td>
    </tr>
  );
}
