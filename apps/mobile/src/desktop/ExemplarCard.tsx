/**
 * The story trade — the single most extreme finalized refusal under one
 * rule (docs/IMPL_REFUSAL_LEDGER.md §2.2). "This is the demo" per the doc:
 * the exact contract, the named rule that refused it, the thesis behind
 * it, and what it did afterwards in dollars.
 *
 * Backs onto GET /api/v1/risk/vetoes/{rule}/exemplar
 * (`apps/api/app/routers/insights.py`'s `veto_exemplar` / `VetoExemplarResponse`
 * — the real, now-shipped shape this component reads). An earlier version
 * of this file was written before that endpoint existed and guessed a
 * different shape (a `found: boolean` field, `price`/`markPrice`/
 * `tradingDaysElapsed`/`notionalPctOfEquity`/`capPct`/`approvalMode`/
 * `riskProfile`) — the real response has none of those, so every read
 * against the guessed names was silently `undefined` and the card always
 * rendered its empty state. `build_veto_exemplar` only ever returns a
 * FINALIZED ghost (or none at all, signaled by a 404 — never a `found:
 * false`), so `ghostPnl` here is always a real number and `horizonDays` IS
 * the elapsed trading-day count, not something to compute separately.
 */

import { useVetoExemplar } from '@/hooks/useInsights';
import { ApiError } from '@/lib/api';

import { ago, ruleLabel, usd } from './format';
import { Button, Card, CardHead, Label, Pill, Row, SkelRows, Stack } from './primitives';
import type { Tone } from './primitives';

function wouldHaveTone(ghostPnl: number): Tone {
  if (ghostPnl < 0) return 'bull';
  if (ghostPnl > 0) return 'warn';
  return 'neutral';
}

function wouldHaveVerdict(ghostPnl: number, preventedLossUsd: number): string {
  if (ghostPnl < 0) return `That refusal saved ${usd(preventedLossUsd)}.`;
  if (ghostPnl > 0) return `That refusal would have made ${usd(ghostPnl)}.`;
  return 'That refusal was a wash.';
}

export function ExemplarCard({ rule, onClose }: { rule: string; onClose: () => void }) {
  const exemplar = useVetoExemplar(rule);
  const d = exemplar.data;
  // The real endpoint's ONLY "nothing to show" signal is a 404 (see the
  // module docstring) — never a data shape with found:false. Any OTHER
  // error (network failure, 500, ...) is genuinely unexpected and gets
  // the "not available" copy below instead of "no finalized refusal yet".
  const notFinalizedYet =
    exemplar.error instanceof ApiError && exemplar.error.status === 404;

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
      ) : notFinalizedYet ? (
        <div className="pg-empty">
          <p className="pg-empty-title">No finalized refusal yet</p>
          <p className="pg-empty-body">
            Every {ruleLabel(rule)} refusal in this window is still marking. Check back once a ghost
            finalizes.
          </p>
        </div>
      ) : exemplar.isError || !d ? (
        <div className="pg-empty">
          <p className="pg-empty-title">Exemplar not available</p>
          <p className="pg-empty-body">
            {`GET /api/v1/risk/vetoes/${rule}/exemplar didn't respond — try again shortly.`}
          </p>
        </div>
      ) : (
        <Stack gap={14}>
          <Stack gap={4}>
            <Row gap={8} style={{ flexWrap: 'wrap' }}>
              <span className="pg-num" style={{ fontSize: 16, fontWeight: 600 }}>
                {d.occSymbol ?? d.symbol}
              </span>
              <Pill tone="bear">refused by {ruleLabel(d.rule)}</Pill>
            </Row>
            <span className="pg-caption">{ago(d.triggeredAt)}</span>
          </Stack>

          <p className="pg-body-sm">
            The council wanted {d.qty} contract{d.qty === 1 ? '' : 's'} at {usd(d.entryPrice, 2)}
            {d.estimatedNotional != null ? <> ({usd(d.estimatedNotional)})</> : null}.
          </p>

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

          {d.lastPrice != null ? (
            <p className="pg-body-sm">
              {d.horizonDays} session{d.horizonDays === 1 ? '' : 's'} later, the contract was worth{' '}
              <span className="pg-num">{usd(d.lastPrice, 2)}</span>.
            </p>
          ) : null}

          <div className="pg-inset">
            <Row gap={8}>
              <Pill tone={wouldHaveTone(d.ghostPnl)}>
                {d.ghostPnl < 0 ? 'SAVED' : d.ghostPnl > 0 ? 'MISSED' : 'EVEN'}
              </Pill>
              <span style={{ fontWeight: 600, fontSize: 13 }}>
                {wouldHaveVerdict(d.ghostPnl, d.preventedLossUsd)}
              </span>
            </Row>
          </div>
        </Stack>
      )}
    </Card>
  );
}
