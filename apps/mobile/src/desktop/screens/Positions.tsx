/** Positions / portfolio — open agent-managed holdings + manual close. */

import { useAccount } from '@/hooks/useAccount';
import { useClosePosition, useOpenPositions } from '@/hooks/usePositions';
import { useDecisionTimeline } from '@/hooks/useDecisionTimeline';

import { ago, signedPct, signedUsd, tone, usd } from '../format';
import {
  Button,
  Card,
  CardHead,
  Cell,
  DataStreamInterrupted,
  Label,
  Numeral,
  PageHead,
  Pill,
  Row,
  SkelRows,
  Stack,
  StatTile,
} from '../primitives';
import { useState } from 'react';

export function PositionsScreen() {
  const positions = useOpenPositions();
  const account = useAccount();
  const close = useClosePosition();
  const [selected, setSelected] = useState<string | null>(null);

  if (positions.isError) {
    return (
      <DataStreamInterrupted
        code="POSITIONS_READ_FAILED"
        node="api · /v1/positions"
        onRetry={() => void positions.refetch()}
      />
    );
  }

  const rows = positions.data ?? [];
  const unrealized = rows.reduce((sum, p) => sum + (p.unrealizedPnl ?? 0), 0);
  const acct = account.data;

  return (
    <>
      <PageHead
        title="Positions"
        sub="Everything the agent is holding, and who owns each exit."
        right={<Pill tone={rows.length > 0 ? 'bull' : 'neutral'}>{rows.length} OPEN</Pill>}
      />

      <div className="pg-grid pg-fade-up">
        <Cell span={3}>
          <StatTile label="Equity" value={acct ? usd(acct.equity) : '—'} loading={account.isLoading} />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Today P&L"
            value={acct ? signedUsd(acct.todayPnl) : '—'}
            caption={acct ? signedPct(acct.todayPnlPct) : undefined}
            tone={tone(acct?.todayPnl)}
            loading={account.isLoading}
          />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Unrealised"
            value={rows.length > 0 ? signedUsd(unrealized) : '—'}
            caption="Across open positions"
            tone={tone(unrealized)}
            loading={positions.isLoading}
          />
        </Cell>
        <Cell span={3}>
          <StatTile label="Cash" value={acct ? usd(acct.cash) : '—'} caption={acct ? `${usd(acct.buyingPower)} buying power` : undefined} loading={account.isLoading} />
        </Cell>

        <Cell span={selected ? 8 : 12}>
          <Card>
            <CardHead label="Open positions" />
            {positions.isLoading ? (
              <SkelRows rows={5} h={20} />
            ) : rows.length === 0 ? (
              /* A genuine "0 open" result, not a slow request — an empty
                 inbox is a state, and shimmering at it forever reads as
                 a hung request. Same convention as Picks/Dashboard. */
              <div className="pg-empty">
                <p className="pg-empty-title">No open positions</p>
                <p className="pg-empty-body">
                  Nothing the agent is currently holding for this account.
                </p>
              </div>
            ) : (
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>Ticker</th>
                    <th>Exit owner</th>
                    <th className="pg-num-right">Qty</th>
                    <th className="pg-num-right">Entry</th>
                    <th className="pg-num-right">Last</th>
                    <th className="pg-num-right">Stop / target</th>
                    <th className="pg-num-right">Unrealised</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((p) => (
                    <tr
                      key={p.decisionId ?? `broker:${p.symbol}`}
                      className={p.decisionId ? 'pg-row-btn' : undefined}
                      onClick={
                        p.decisionId
                          ? () => setSelected(p.decisionId === selected ? null : p.decisionId)
                          : undefined
                      }
                    >
                      <td>
                        <Stack gap={2}>
                          <Row gap={8}>
                            <Numeral size={15} weight={600}>
                              {p.symbol}
                            </Numeral>
                            <Pill tone={p.direction === 'short' ? 'bear' : 'bull'}>
                              {p.direction.toUpperCase()}
                            </Pill>
                            {!p.managed ? <Pill tone="neutral">UNMANAGED</Pill> : null}
                          </Row>
                          <span className="pg-caption">
                            {p.managed
                              ? `opened ${ago(p.openedAt)}`
                              : `held at broker — no council decision behind it`}
                          </span>
                        </Stack>
                      </td>
                      <td>
                        <Pill tone={p.exitMode === 'agent' ? 'bull' : 'neutral'}>
                          {p.exitMode === 'agent' ? 'AGENT' : 'MANUAL'}
                        </Pill>
                      </td>
                      <td className="pg-num-right">{p.qty}</td>
                      <td className="pg-num-right">{p.avgEntryPrice != null ? usd(p.avgEntryPrice, 2) : '—'}</td>
                      <td className="pg-num-right">{p.lastPrice != null ? usd(p.lastPrice, 2) : '—'}</td>
                      <td className="pg-num-right pg-dim">
                        {p.stopLoss != null ? usd(p.stopLoss, 2) : '—'} / {p.targetPrice != null ? usd(p.targetPrice, 2) : '—'}
                      </td>
                      <td className="pg-num-right">
                        <span className={p.unrealizedPnl != null && p.unrealizedPnl < 0 ? 'pg-bear' : 'pg-bull'}>
                          {signedUsd(p.unrealizedPnl)}
                        </span>
                      </td>
                      <td className="pg-num-right">
                        {p.decisionId ? (
                          <Button
                            size="sm"
                            onClick={() => close.mutate(p.decisionId!)}
                            disabled={close.isPending}
                            ariaLabel={`Close the ${p.symbol} position now`}
                          >
                            Close
                          </Button>
                        ) : (
                          <span className="pg-caption pg-dim">close at broker</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {close.data && !close.data.closed ? (
              <div className="pg-inset" style={{ borderColor: 'var(--pg-bear)' }}>
                <Label>Close refused</Label>
                <div className="pg-body-sm pg-bear" style={{ marginTop: 6 }}>
                  {close.data.detail ?? close.data.error}
                </div>
              </div>
            ) : null}
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

function TimelineCard({ decisionId, onClose }: { decisionId: string; onClose: () => void }) {
  const timeline = useDecisionTimeline(decisionId);
  return (
    <Card>
      <CardHead
        label="Trade biography"
        right={
          <Button size="sm" kind="ghost" onClick={onClose} ariaLabel="Close the trade biography">
            Close
          </Button>
        }
      />
      {timeline.isLoading || !timeline.data ? (
        <SkelRows rows={5} />
      ) : (
        <Stack gap={0}>
          {timeline.data.events.map((e, i) => (
            <Row key={`${e.kind}-${i}`} gap={10} style={{ alignItems: 'flex-start', padding: '10px 0', borderTop: i === 0 ? undefined : '1px solid var(--pg-card-border)' }}>
              <span aria-hidden style={{ width: 6, height: 6, borderRadius: 9999, marginTop: 7, flex: 'none', background: 'var(--pg-outline)' }} />
              <Stack gap={2} style={{ flex: 1 }}>
                <span style={{ fontSize: 13, fontWeight: 500 }}>{e.title}</span>
                <span className="pg-caption">{e.detail}</span>
              </Stack>
              <span className="pg-caption pg-num" style={{ flex: 'none' }}>
                {ago(e.at)}
              </span>
            </Row>
          ))}
        </Stack>
      )}
    </Card>
  );
}
