/**
 * Insights — the regret desk: veto ledger, ghost P&L, calibration
 * scorecard. Three tabs over three existing queries.
 */

import { useState } from 'react';

import { useCalibrationScorecard } from '@/hooks/useCalibration';
import { useGhostSummary, useVetoLedger } from '@/hooks/useInsights';

import { ago, ruleLabel, signedUsd, tone, usd } from '../format';
import {
  Card,
  CardHead,
  Cell,
  DataStreamInterrupted,
  Label,
  Numeral,
  PageHead,
  Pill,
  Row,
  ScoreBar,
  SkelRows,
  Stack,
  StatTile,
} from '../primitives';

type Tab = 'vetoes' | 'ghost' | 'calibration';

const TABS: { id: Tab; label: string }[] = [
  { id: 'vetoes', label: 'Veto ledger' },
  { id: 'ghost', label: 'Ghost P&L' },
  { id: 'calibration', label: 'Calibration' },
];

export function InsightsScreen() {
  const [tab, setTab] = useState<Tab>('vetoes');
  const vetoes = useVetoLedger(30);
  const ghost = useGhostSummary(30);
  const scorecard = useCalibrationScorecard(180);

  return (
    <>
      <PageHead
        title="Insights"
        sub="What the risk engine stopped, what it cost, and whether your overrides beat the loop."
        right={
          <Row gap={8}>
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                className={`pg-btn pg-btn-${tab === t.id ? 'primary' : 'secondary'} pg-btn-sm`}
                onClick={() => setTab(t.id)}
                aria-pressed={tab === t.id}
              >
                {t.label}
              </button>
            ))}
          </Row>
        }
      />

      {tab === 'vetoes' ? (
        vetoes.isError ? (
          <DataStreamInterrupted code="VETO_LEDGER_FAILED" node="api · /v1/risk/vetoes" onRetry={() => void vetoes.refetch()} />
        ) : (
          <div className="pg-grid pg-fade-up">
            <Cell span={4}>
              <StatTile
                label="Vetoes fired"
                value={vetoes.data ? String(vetoes.data.totalVetoes) : '—'}
                caption="Last 30 days"
                loading={vetoes.isLoading}
              />
            </Cell>
            <Cell span={4}>
              <StatTile
                label="Notional blocked"
                value={vetoes.data ? usd(vetoes.data.totalBlockedNotional) : '—'}
                caption="Orders never sent"
                loading={vetoes.isLoading}
              />
            </Cell>
            <Cell span={4}>
              <StatTile
                label="Distinct rules"
                value={vetoes.data ? String(vetoes.data.rules.length) : '—'}
                caption="Each one named and auditable"
                loading={vetoes.isLoading}
              />
            </Cell>
            <Cell span={12}>
              <Card>
                <CardHead label="Rules that fired" right={<Pill>DETERMINISTIC</Pill>} />
                {vetoes.isLoading || !vetoes.data || vetoes.data.rules.length === 0 ? (
                  <SkelRows rows={6} h={20} />
                ) : (
                  <table className="pg-table">
                    <thead>
                      <tr>
                        <th>Rule</th>
                        <th className="pg-num-right">Fired</th>
                        <th className="pg-num-right">Blocked notional</th>
                        <th className="pg-num-right">Ghost P&L</th>
                        <th className="pg-num-right">Loss prevented</th>
                        <th className="pg-num-right">Last</th>
                      </tr>
                    </thead>
                    <tbody>
                      {vetoes.data.rules.map((r) => (
                        <tr key={r.rule}>
                          <td>
                            <span className="pg-num" style={{ fontSize: 13 }}>
                              {ruleLabel(r.rule)}
                            </span>
                          </td>
                          <td className="pg-num-right">{r.count}</td>
                          <td className="pg-num-right">{usd(r.blockedNotional)}</td>
                          <td className="pg-num-right">
                            <span className={(r.ghostPnl ?? 0) < 0 ? 'pg-bear' : 'pg-bull'}>
                              {r.ghostPnl != null ? signedUsd(r.ghostPnl) : '—'}
                            </span>
                          </td>
                          <td className="pg-num-right pg-bull">
                            {r.preventedLossUsd != null ? usd(r.preventedLossUsd) : '—'}
                          </td>
                          <td className="pg-num-right pg-dim">{ago(r.lastAt)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>
            </Cell>
          </div>
        )
      ) : null}

      {tab === 'ghost' ? (
        ghost.isError ? (
          <DataStreamInterrupted code="GHOST_SUMMARY_FAILED" node="api · /v1/ghost/summary" onRetry={() => void ghost.refetch()} />
        ) : (
          <div className="pg-grid pg-fade-up">
            <Cell span={6}>
              <StatTile
                label="Loss avoided"
                value={ghost.data ? usd(ghost.data.savedUsd) : '—'}
                caption="What vetoed picks would have lost"
                tone="bull"
                loading={ghost.isLoading}
              />
            </Cell>
            <Cell span={6}>
              <StatTile
                label="Upside missed"
                value={ghost.data ? usd(ghost.data.missedUsd) : '—'}
                caption="What declined picks would have made"
                tone="bear"
                loading={ghost.isLoading}
              />
            </Cell>
            <Cell span={6}>
              <Card>
                <CardHead label="Vetoed by risk" />
                {ghost.isLoading || !ghost.data ? (
                  <SkelRows rows={3} h={22} />
                ) : (
                  <Stack gap={12}>
                    <Numeral size={40} tone={tone(ghost.data.vetoed.ghostPnl)}>
                      {signedUsd(ghost.data.vetoed.ghostPnl)}
                    </Numeral>
                    <span className="pg-body-sm">
                      {ghost.data.vetoed.count} finalised · {ghost.data.vetoed.pendingCount} still marking
                    </span>
                  </Stack>
                )}
              </Card>
            </Cell>
            <Cell span={6}>
              <Card>
                <CardHead label="Declined by you" />
                {ghost.isLoading || !ghost.data ? (
                  <SkelRows rows={3} h={22} />
                ) : (
                  <Stack gap={12}>
                    <Numeral size={40} tone={tone(ghost.data.declined.ghostPnl)}>
                      {signedUsd(ghost.data.declined.ghostPnl)}
                    </Numeral>
                    <span className="pg-body-sm">
                      {ghost.data.declined.count} finalised · {ghost.data.declined.pendingCount} still marking
                    </span>
                  </Stack>
                )}
              </Card>
            </Cell>
            <Cell span={12}>
              <Card variant="dense">
                <span className="pg-caption">
                  Ghost P&L marks every pick you didn’t take to the same horizon it would have run. It is a
                  counterfactual, not a fill — it never touches the broker.
                </span>
              </Card>
            </Cell>
          </div>
        )
      ) : null}

      {tab === 'calibration' ? (
        scorecard.isError ? (
          <DataStreamInterrupted
            code="SCORECARD_FAILED"
            node="api · /v1/review/scorecard"
            onRetry={() => void scorecard.refetch()}
          />
        ) : (
          <div className="pg-grid pg-fade-up">
            <Cell span={4}>
              <StatTile
                label="Agreement"
                value={scorecard.data ? `${scorecard.data.agreementPct.toFixed(0)}%` : '—'}
                caption={scorecard.data ? `${scorecard.data.windowDays}-day window` : undefined}
                tone={scorecard.data && scorecard.data.agreementPct >= 60 ? 'bull' : 'neutral'}
                loading={scorecard.isLoading}
              />
            </Cell>
            <Cell span={4}>
              <StatTile
                label="Overrides"
                value={scorecard.data ? String(scorecard.data.overrides.count) : '—'}
                caption="You disagreed with the loop"
                loading={scorecard.isLoading}
              />
            </Cell>
            <Cell span={4}>
              <StatTile
                label="You were right"
                value={scorecard.data ? `${scorecard.data.overrides.operatorWinRatePct.toFixed(0)}%` : '—'}
                caption={
                  scorecard.data
                    ? `${scorecard.data.overrides.operatorWins} operator · ${scorecard.data.overrides.reflectionWins} reflection`
                    : undefined
                }
                tone={scorecard.data && scorecard.data.overrides.operatorWinRatePct >= 50 ? 'bull' : 'bear'}
                loading={scorecard.isLoading}
              />
            </Cell>
            <Cell span={12}>
              <Card>
                <CardHead label="Agreement by month" />
                {scorecard.isLoading || !scorecard.data || scorecard.data.months.length === 0 ? (
                  <SkelRows rows={4} h={22} />
                ) : (
                  <Stack gap={14}>
                    {scorecard.data.months.map((m) => (
                      <Stack gap={6} key={m.month}>
                        <Row style={{ justifyContent: 'space-between' }}>
                          <Label>{m.month}</Label>
                          <span className="pg-caption pg-num">
                            {m.agreementPct.toFixed(0)}% · {m.totalReviewed} reviewed
                          </span>
                        </Row>
                        <ScoreBar score={m.agreementPct} />
                      </Stack>
                    ))}
                  </Stack>
                )}
              </Card>
            </Cell>
          </div>
        )
      ) : null}
    </>
  );
}
