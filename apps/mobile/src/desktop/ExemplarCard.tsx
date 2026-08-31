/**
 * The story trade — the single most extreme finalized refusal under one
 * rule (docs/IMPL_REFUSAL_LEDGER.md §2.2). "This is the demo" per the doc:
 * the exact contract, the named rule that refused it, the thesis behind
 * it, and what it did afterwards in dollars.
 *
 * Backs onto GET /api/v1/risk/vetoes/{rule}/exemplar, which did not exist
 * server-side as of this writing — every field below is read defensively
 * so the card degrades to omission rather than crashing if the shape it
 * eventually ships with differs from `VetoExemplarResponse`.
 */

import { useVetoExemplar } from '@/hooks/useInsights';

import { ago, riskProfileCaption, ruleLabel, usd } from './format';
import { Button, Card, CardHead, Label, Pill, Row, SkelRows, Stack } from './primitives';
import type { Tone } from './primitives';

function wouldHaveTone(ghostPnl: number | null | undefined): Tone {
  if (ghostPnl == null) return 'neutral';
  if (ghostPnl < 0) return 'bull';
  if (ghostPnl > 0) return 'warn';
  return 'neutral';
}

function wouldHaveVerdict(ghostPnl: number, preventedLossUsd: number | null | undefined): string {
  if (ghostPnl < 0) return `That refusal saved ${usd(preventedLossUsd ?? -ghostPnl)}.`;
  if (ghostPnl > 0) return `That refusal would have made ${usd(ghostPnl)}.`;
  return 'That refusal was a wash.';
}

export function ExemplarCard({ rule, onClose }: { rule: string; onClose: () => void }) {
  const exemplar = useVetoExemplar(rule);
  const d = exemplar.data;

  return (
    <Card>
      <CardHead
        label="Story trade"
        right={
          <Button size="sm" kind="ghost" onClick={onClose} ariaLabel="Close the story trade">
            Close
          </Button>
        }
      />
      {exemplar.isLoading ? (
        <SkelRows rows={7} />
      ) : exemplar.isError ? (
        <div className="pg-empty">
          <p className="pg-empty-title">Exemplar not available</p>
          <p className="pg-empty-body">
            {`GET /api/v1/risk/vetoes/${rule}/exemplar didn't respond — this endpoint may not be built yet.`}
          </p>
        </div>
      ) : !d || !d.found ? (
        <div className="pg-empty">
          <p className="pg-empty-title">No finalized refusal yet</p>
          <p className="pg-empty-body">
            Every {ruleLabel(rule)} refusal in this window is still marking. Check back once a ghost
            finalizes.
          </p>
        </div>
      ) : (
        <Stack gap={14}>
          <Stack gap={4}>
            <Row gap={8} style={{ flexWrap: 'wrap' }}>
              <span className="pg-num" style={{ fontSize: 16, fontWeight: 600 }}>
                {d.occSymbol ?? d.symbol ?? 'Unknown contract'}
              </span>
              <Pill tone="bear">refused by {ruleLabel(d.rule ?? rule)}</Pill>
              {d.approvalMode === 'auto' ? (
                <Pill tone="warn" title="Executed by the auto-approve sweeper — no human tap">
                  AUTO
                </Pill>
              ) : null}
            </Row>
            {d.triggeredAt ? <span className="pg-caption">{ago(d.triggeredAt)}</span> : null}
          </Stack>

          {d.qty != null && d.price != null ? (
            <p className="pg-body-sm">
              The council wanted {d.qty} contract{d.qty === 1 ? '' : 's'} at {usd(d.price, 2)}
              {d.estimatedNotional != null ? (
                <>
                  {' '}
                  ({usd(d.estimatedNotional)}
                  {d.notionalPctOfEquity != null ? ` = ${d.notionalPctOfEquity.toFixed(1)}% of equity` : ''}
                  {d.capPct != null ? `, cap ${d.capPct}%` : ''})
                </>
              ) : null}
              .
            </p>
          ) : null}

          {d.bullCase ? (
            <Stack gap={4}>
              <Label>Bull case</Label>
              <p className="pg-body-sm">{d.bullCase}</p>
            </Stack>
          ) : null}

          {d.bearCase ? (
            <Stack gap={4}>
              <Label>Bear case</Label>
              <p className="pg-body-sm">{d.bearCase}</p>
            </Stack>
          ) : null}

          {d.markPrice != null ? (
            <p className="pg-body-sm">
              {d.tradingDaysElapsed != null
                ? `${d.tradingDaysElapsed} session${d.tradingDaysElapsed === 1 ? '' : 's'} later, `
                : 'Since then, '}
              the contract was worth <span className="pg-num">{usd(d.markPrice, 2)}</span>.
            </p>
          ) : null}

          <div className="pg-inset">
            {d.ghostPnl == null ? (
              <span className="pg-caption pg-dim">pending — still marking, no verdict yet</span>
            ) : (
              <Row gap={8}>
                <Pill tone={wouldHaveTone(d.ghostPnl)}>
                  {d.ghostPnl < 0 ? 'SAVED' : d.ghostPnl > 0 ? 'MISSED' : 'EVEN'}
                </Pill>
                <span style={{ fontWeight: 600, fontSize: 13 }}>
                  {wouldHaveVerdict(d.ghostPnl, d.preventedLossUsd)}
                </span>
              </Row>
            )}
          </div>

          <span className="pg-caption">{riskProfileCaption(d.riskProfile)}</span>
        </Stack>
      )}
    </Card>
  );
}
