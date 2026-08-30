/**
 * Decisions — every council pass, browsable.
 *
 * Before this screen existed, a decision's id was only reachable if it
 * had been approved (Positions) or was still pending (Picks) — a
 * strategy-fit HOLD, the majority of any watchlist sweep, was invisible
 * the instant the sweep moved past it. This is the "58 decisions in this
 * window" the Strategies screen counts but never let you open.
 */

import { useState } from 'react';

import { useDecisions } from '@/hooks/useDecisions';
import type { DecisionsFilter } from '@/hooks/useDecisions';

import { ago } from '../format';
import {
  Card,
  CardHead,
  Cell,
  DataStreamInterrupted,
  PageHead,
  Pill,
  Row,
  SkelRows,
  Stack,
} from '../primitives';
import { TimelineCard } from '../TradeBiography';

const ACTIONS: Array<{ id: DecisionsFilter['action'] | 'all'; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'BUY', label: 'Buy' },
  { id: 'SELL', label: 'Sell' },
  { id: 'HOLD', label: 'Hold' },
];

function actionTone(action: string): 'bull' | 'bear' | 'neutral' {
  if (action === 'BUY') return 'bull';
  if (action === 'SELL') return 'bear';
  return 'neutral';
}

/** Why this row is what it is, at a glance — before opening the full
 * biography. Mirrors the three-way split the API's own summary makes. */
function outcomeLabel(d: {
  finalAction: string;
  riskApproved: boolean;
  riskVetoRule: string | null;
  selectedStrategy: string | null;
  userResponse: string | null;
}): string {
  if (d.finalAction !== 'HOLD') {
    if (d.riskApproved) {
      return d.userResponse === 'approved' ? 'approved' : 'awaiting your decision';
    }
    return d.riskVetoRule ? `risk vetoed · ${d.riskVetoRule}` : 'risk vetoed';
  }
  return d.selectedStrategy ? 'drafter held' : 'no strategy fit';
}

export function DecisionsScreen() {
  const [action, setAction] = useState<DecisionsFilter['action'] | 'all'>('all');
  const [symbol, setSymbol] = useState('');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const limit = 25;

  const decisions = useDecisions({
    action: action === 'all' ? undefined : action,
    symbol: symbol.trim() || undefined,
    limit,
    offset,
  });

  if (decisions.isError) {
    return (
      <DataStreamInterrupted
        code="DECISIONS_READ_FAILED"
        node="api · /v1/decisions"
        onRetry={() => void decisions.refetch()}
      />
    );
  }

  const rows = decisions.data?.decisions ?? [];
  const total = decisions.data?.total ?? 0;

  return (
    <>
      <PageHead
        title="Decisions"
        sub="Every council pass, whether or not it ever became a proposal."
        right={<Pill tone="neutral">{total} TOTAL</Pill>}
      />

      <div className="pg-grid pg-fade-up">
        <Cell span={12}>
          <Row gap={8} style={{ flexWrap: 'wrap' }}>
            {ACTIONS.map((a) => (
              <button
                key={a.id}
                type="button"
                className={`pg-btn pg-btn-${action === a.id ? 'primary' : 'secondary'} pg-btn-sm`}
                onClick={() => {
                  setAction(a.id);
                  setOffset(0);
                }}
                aria-pressed={action === a.id}
              >
                {a.label}
              </button>
            ))}
            <input
              className="pg-input"
              placeholder="Filter by ticker…"
              value={symbol}
              onChange={(e) => {
                setSymbol(e.target.value.toUpperCase());
                setOffset(0);
              }}
              style={{ maxWidth: 160 }}
              aria-label="Filter decisions by ticker"
            />
          </Row>
        </Cell>

        <Cell span={selected ? 8 : 12}>
          <Card>
            <CardHead label="Council passes" />
            {decisions.isLoading ? (
              <SkelRows rows={8} h={20} />
            ) : rows.length === 0 ? (
              <div className="pg-empty">
                <p className="pg-empty-title">No decisions match this filter</p>
                <p className="pg-empty-body">
                  {symbol || action !== 'all'
                    ? 'Try clearing the ticker or action filter.'
                    : 'Nothing has run yet — approve a symbol on Picks or wait for the next sweep.'}
                </p>
              </div>
            ) : (
              <>
                <div style={{ overflowX: 'auto' }}>
                  <table className="pg-table">
                    <thead>
                      <tr>
                        <th>Ticker</th>
                        <th>Action</th>
                        <th>Outcome</th>
                        <th>Strategy</th>
                        <th className="pg-num-right">Fit conf.</th>
                        <th className="pg-num-right">When</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((d) => (
                        <tr
                          key={d.id}
                          className="pg-row-btn"
                          onClick={() => setSelected(d.id === selected ? null : d.id)}
                        >
                          <td>
                            <Stack gap={2}>
                              <span className="pg-num" style={{ fontSize: 15, fontWeight: 600 }}>
                                {d.symbol}
                              </span>
                              {d.regime ? <span className="pg-caption">{d.regime} regime</span> : null}
                            </Stack>
                          </td>
                          <td>
                            <Row gap={6}>
                              <Pill tone={actionTone(d.finalAction)}>{d.finalAction}</Pill>
                              {d.approvalMode === 'auto' ? (
                                <Pill tone="warn" title="Executed by the auto-approve sweeper — no human tap">
                                  AUTO
                                </Pill>
                              ) : null}
                            </Row>
                          </td>
                          <td>
                            <span className="pg-caption">{outcomeLabel(d)}</span>
                          </td>
                          <td>
                            <span className="pg-caption">{d.selectedStrategy ?? '—'}</span>
                          </td>
                          <td className="pg-num-right">
                            {d.selectedStrategy ? `${Math.round(d.selectorConfidence * 100)}%` : '—'}
                          </td>
                          <td className="pg-num-right">
                            <span className="pg-caption">{ago(d.triggeredAt)}</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Row style={{ justifyContent: 'space-between', marginTop: 4 }}>
                  <span className="pg-caption">
                    {offset + 1}–{Math.min(offset + limit, total)} of {total}
                  </span>
                  <Row gap={8}>
                    <button
                      type="button"
                      className="pg-btn pg-btn-secondary pg-btn-sm"
                      disabled={offset === 0}
                      onClick={() => setOffset(Math.max(0, offset - limit))}
                    >
                      Newer
                    </button>
                    <button
                      type="button"
                      className="pg-btn pg-btn-secondary pg-btn-sm"
                      disabled={offset + limit >= total}
                      onClick={() => setOffset(offset + limit)}
                    >
                      Older
                    </button>
                  </Row>
                </Row>
              </>
            )}
          </Card>
        </Cell>

        {selected ? (
          <Cell span={4}>
            <TimelineCard decisionId={selected} onClose={() => setSelected(null)} />
          </Cell>
        ) : null}
      </div>
    </>
  );
}
