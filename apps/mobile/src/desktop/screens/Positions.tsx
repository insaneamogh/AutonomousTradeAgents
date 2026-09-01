/** Positions / portfolio — open agent-managed holdings + manual close +
 * closed-position history (what was opened, closed, and what it realized). */

import { useAccount } from '@/hooks/useAccount';
import {
  useCloseUnmanagedPosition,
  useClosedPositions,
  useClosePosition,
  useOpenPositions,
} from '@/hooks/usePositions';
import { runErrorMessage } from '@/lib/api';
import { DEMO_DISABLED_REASON, useIsDemoSession } from '@/lib/demoSession';
import type { ClosedPositionDto, OpenPositionDto } from '@app/shared-types';

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
import { TimelineCard } from '../TradeBiography';
import { useState } from 'react';

/** What actually closed the position, in plain English — mirrors the
 * backend's own `_CLOSE_REASON_LABEL` (position_manager.py) plus the two
 * reasons stamped elsewhere (`user_manual` by close_position_now,
 * `external_broker` by order_sync). Duplicated client-side rather than
 * sent as display text from the API — same convention this file already
 * uses for CLOSE_ERROR_COPY (further down, for close/cancel error codes). */
const CLOSE_REASON_LABEL: Record<string, string> = {
  agent_time: 'time stop reached',
  agent_signal: 'council flipped to SELL',
  agent_expiry: 'closed ahead of expiry',
  option_take_profit: 'premium take-profit hit',
  option_stop_loss: 'premium stop-loss hit',
  option_trail_stop: 'trailing stop hit',
  user_manual: 'closed in the app',
  external_broker: 'closed directly at Alpaca',
};

function closeReasonLabel(reason: string | null): string {
  if (!reason) return 'reason unknown';
  return CLOSE_REASON_LABEL[reason] ?? reason.replace(/_/g, ' ');
}

export function PositionsScreen() {
  const [view, setView] = useState<'open' | 'closed'>('open');
  const positions = useOpenPositions();
  const closedQuery = useClosedPositions({ limit: 100 });
  const account = useAccount();
  const close = useClosePosition();
  const closeUnmanaged = useCloseUnmanagedPosition();
  const [selected, setSelected] = useState<string | null>(null);
  const isDemo = useIsDemoSession();

  // A native confirm() before anything irreversible fires — same gate the
  // mobile app already applies via Alert.alert, just the web-native form
  // of it (no bespoke modal system exists in this desktop tree yet).
  const confirmAndClose = (p: OpenPositionDto) => {
    const pending = p.status === 'pending_fill';
    const message = pending
      ? `Cancel the working ${p.symbol} order? This cancels it at the broker before it fills — nothing was ever bought or sold.`
      : `Close ${p.qty} ${p.symbol} now? This places a market sell through the same risk checks the agent uses. Resting stop/target orders are cancelled first.`;
    if (!window.confirm(message)) return;
    close.mutate(p.decisionId!);
  };

  const confirmAndCloseUnmanaged = (p: OpenPositionDto) => {
    const message = `Close ${p.qty} ${p.symbol} now? This position has no council decision behind it — closing places a market order directly, through the same risk checks as any other close.`;
    if (!window.confirm(message)) return;
    closeUnmanaged.mutate(p.symbol);
  };

  if (view === 'open' && positions.isError) {
    return (
      <DataStreamInterrupted
        code="POSITIONS_READ_FAILED"
        node="api · /v1/positions"
        onRetry={() => void positions.refetch()}
      />
    );
  }
  if (view === 'closed' && closedQuery.isError) {
    return (
      <DataStreamInterrupted
        code="POSITIONS_HISTORY_READ_FAILED"
        node="api · /v1/positions/history"
        onRetry={() => void closedQuery.refetch()}
      />
    );
  }

  const rows = positions.data ?? [];
  const unrealized = rows.reduce((sum, p) => sum + (p.unrealizedPnl ?? 0), 0);
  const closedRows = closedQuery.data?.positions ?? [];
  const acct = account.data;

  return (
    <>
      <PageHead
        title="Positions"
        sub={
          view === 'closed'
            ? 'What was opened, closed, and what it realized.'
            : isDemo
              ? `${DEMO_DISABLED_REASON} — closing and cancelling are turned off.`
              : 'Everything the agent is holding, and who owns each exit.'
        }
        right={
          <Row gap={8}>
            <Button
              size="sm"
              kind={view === 'open' ? 'primary' : 'ghost'}
              onClick={() => setView('open')}
              ariaLabel="Show open positions"
            >
              Open
            </Button>
            <Button
              size="sm"
              kind={view === 'closed' ? 'primary' : 'ghost'}
              onClick={() => setView('closed')}
              ariaLabel="Show closed positions"
            >
              Closed
            </Button>
            {view === 'closed' ? (
              <Pill tone={closedRows.length > 0 ? 'bull' : 'neutral'}>
                {closedQuery.data?.total ?? closedRows.length} CLOSED
              </Pill>
            ) : (
              <Pill tone={rows.length > 0 ? 'bull' : 'neutral'}>{rows.length} OPEN</Pill>
            )}
          </Row>
        }
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
            <CardHead label={view === 'closed' ? 'Closed positions' : 'Open positions'} />
            {view === 'closed' ? (
              closedQuery.isLoading ? (
                <SkelRows rows={5} h={20} />
              ) : closedRows.length === 0 ? (
                <div className="pg-empty">
                  <p className="pg-empty-title">Nothing closed yet</p>
                  <p className="pg-empty-body">
                    Once a position closes — by the agent, by you, or directly at Alpaca — it
                    shows here with what it realized.
                  </p>
                </div>
              ) : (
                <div style={{ overflowX: 'auto' }}>
                  <table className="pg-table">
                    <thead>
                      <tr>
                        <th>Ticker</th>
                        <th>Closed by</th>
                        <th className="pg-num-right">Qty</th>
                        <th className="pg-num-right">Entry → exit</th>
                        <th className="pg-num-right">Realized</th>
                        <th>Closed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {closedRows.map((p: ClosedPositionDto) => (
                        <tr
                          key={p.decisionId}
                          className="pg-row-btn"
                          onClick={() => setSelected(p.decisionId === selected ? null : p.decisionId)}
                        >
                          <td>
                            <Stack gap={2}>
                              <Row gap={8}>
                                <Numeral size={15} weight={600}>
                                  {p.isOption
                                    ? `${p.symbol} $${p.strike?.toFixed(2) ?? '—'} ${(p.contractType ?? '').toUpperCase()}`
                                    : p.symbol}
                                </Numeral>
                                <Pill tone={p.direction === 'short' ? 'bear' : 'bull'}>
                                  {p.direction.toUpperCase()}
                                </Pill>
                              </Row>
                              <span className="pg-caption">
                                opened {ago(p.openedAt)}
                                {p.isOption ? ` · x${p.qty} contract${p.qty === 1 ? '' : 's'}` : ''}
                              </span>
                            </Stack>
                          </td>
                          <td>
                            <Stack gap={2}>
                              <Pill tone={p.exitMode === 'agent' ? 'bull' : 'neutral'}>
                                {p.exitMode === 'agent' ? 'AGENT' : 'MANUAL'}
                              </Pill>
                              <span className="pg-caption">{closeReasonLabel(p.closeReason)}</span>
                            </Stack>
                          </td>
                          <td className="pg-num-right">{p.qty}</td>
                          <td className="pg-num-right pg-dim">
                            {p.avgEntryPrice != null ? usd(p.avgEntryPrice, 2) : '—'}
                            {' → '}
                            {p.exitPrice != null ? usd(p.exitPrice, 2) : '—'}
                            {p.exitPriceSource === 'estimated_from_pnl' ? (
                              <span title="No fill of ours to read — the user closed directly at Alpaca, so this is back-solved from realized P&L rather than a broker fill price.">
                                {' '}
                                (est.)
                              </span>
                            ) : null}
                          </td>
                          <td className="pg-num-right">
                            <span className={p.realizedPnl != null && p.realizedPnl < 0 ? 'pg-bear' : 'pg-bull'}>
                              {signedUsd(p.realizedPnl)}
                            </span>
                          </td>
                          <td>
                            <span className="pg-caption">{ago(p.closedAt)}</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            ) : positions.isLoading ? (
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
              // 8 data columns + the action column don't fit an 8/12-span
              // card once the trade biography panel opens beside it —
              // without its own scroll region the table overflowed the
              // card and clipped the Close button at the right edge
              // instead of the page ever scrolling sideways.
              <div style={{ overflowX: 'auto' }}>
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
                              {p.isOption
                                ? `${p.symbol} $${p.strike?.toFixed(2) ?? '—'} ${(p.contractType ?? '').toUpperCase()}`
                                : p.symbol}
                            </Numeral>
                            <Pill tone={p.direction === 'short' ? 'bear' : 'bull'}>
                              {p.direction.toUpperCase()}
                            </Pill>
                            {p.status === 'pending_fill' ? <Pill tone="neutral">AWAITING FILL</Pill> : null}
                            {!p.managed ? <Pill tone="neutral">UNMANAGED</Pill> : null}
                          </Row>
                          <span className="pg-caption">
                            {p.status === 'pending_fill'
                              ? `approved ${ago(p.openedAt)} — order working at the broker`
                              : p.managed
                                ? `opened ${ago(p.openedAt)}${p.isOption ? ` · x${p.qty} contract${p.qty === 1 ? '' : 's'}` : ''}`
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
                      <td className="pg-num-right">
                        {p.status === 'pending_fill' ? (
                          <span className="pg-caption pg-dim">not filled yet</span>
                        ) : p.lastPrice != null ? (
                          usd(p.lastPrice, 2)
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="pg-num-right pg-dim">
                        {p.isOption
                          ? p.expiryDate
                            ? `exp ${p.expiryDate}`
                            : 'no bracket · exp unknown'
                          : `${p.stopLoss != null ? usd(p.stopLoss, 2) : '—'} / ${p.targetPrice != null ? usd(p.targetPrice, 2) : '—'}`}
                      </td>
                      <td className="pg-num-right">
                        {p.status === 'pending_fill' ? (
                          <span className="pg-caption pg-dim">—</span>
                        ) : (
                          <span className={p.unrealizedPnl != null && p.unrealizedPnl < 0 ? 'pg-bear' : 'pg-bull'}>
                            {signedUsd(p.unrealizedPnl)}
                          </span>
                        )}
                      </td>
                      <td className="pg-num-right">
                        {p.decisionId ? (
                          <Button
                            size="sm"
                            kind={p.status === 'pending_fill' ? 'secondary' : 'primary'}
                            onClick={() => confirmAndClose(p)}
                            disabled={close.isPending || isDemo}
                            title={isDemo ? DEMO_DISABLED_REASON : undefined}
                            ariaLabel={
                              p.status === 'pending_fill'
                                ? `Cancel the working ${p.symbol} order`
                                : `Close the ${p.symbol} position now`
                            }
                          >
                            {p.status === 'pending_fill' ? 'Cancel order' : 'Close'}
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            kind="primary"
                            onClick={() => confirmAndCloseUnmanaged(p)}
                            disabled={closeUnmanaged.isPending || isDemo}
                            title={isDemo ? DEMO_DISABLED_REASON : undefined}
                            ariaLabel={`Close the ${p.symbol} position now — no council decision behind it`}
                          >
                            Close
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
            {view === 'open' &&
              (close.isError || closeUnmanaged.isError ? (
                <div className="pg-inset" style={{ borderColor: 'var(--pg-bear)' }}>
                  <Label>Close refused</Label>
                  <div className="pg-body-sm pg-bear" style={{ marginTop: 6 }}>
                    {closeErrorMessage(close.isError ? close.error : closeUnmanaged.error)}
                  </div>
                </div>
              ) : (close.data && !close.data.closed) || (closeUnmanaged.data && !closeUnmanaged.data.closed) ? (
                <div className="pg-inset" style={{ borderColor: 'var(--pg-bear)' }}>
                  <Label>Close refused</Label>
                  <div className="pg-body-sm pg-bear" style={{ marginTop: 6 }}>
                    {close.data && !close.data.closed
                      ? (close.data.detail ?? close.data.error)
                      : (closeUnmanaged.data!.detail ?? closeUnmanaged.data!.error)}
                  </div>
                </div>
              ) : null)}
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

const CLOSE_ERROR_COPY: Record<string, string> = {
  not_found: "That position or order isn't there any more.",
  not_owner: "That position or order isn't there any more.",
  already_closed: 'Already closed.',
  no_open_position: "This position was already closed — nothing left to close.",
  no_pending_order: 'The order already filled or was already cancelled.',
  close_in_flight: 'A close is already in progress for this position.',
  risk_vetoed: 'A risk rule blocked the close — try again shortly.',
};

/** A 409/404 from close/cancel is thrown, not returned — the "closed:
 * false" branch above only covers the 200-with-risk_vetoed shape. This
 * covers the rest so a failed cancel/close is never silently dropped. */
function closeErrorMessage(err: unknown): string {
  const e = err as { status?: number; body?: { detail?: unknown } } | null;
  const code = typeof e?.body?.detail === 'string' ? e.body.detail : null;
  if (code && CLOSE_ERROR_COPY[code]) return CLOSE_ERROR_COPY[code];
  if (code) return code;
  return runErrorMessage(err);
}

