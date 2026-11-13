/**
 * Insights — the regret desk: veto ledger, ghost P&L, calibration
 * scorecard. Three tabs over three existing queries.
 */

import { useState } from 'react';
import type { VetoRuleDto } from '@app/shared-types';

import { useCalibrationScorecard } from '@/hooks/useCalibration';
import { useFunnel } from '@/hooks/useFunnel';
import { useGhostSummary, useVetoLedger } from '@/hooks/useInsights';
import type { InsightsMarket } from '@/hooks/useInsights';
import { useScanFunnel } from '@/hooks/useScanFunnel';

import { ContractFunnel } from '../components/ContractFunnel';
import { ScanFunnel } from '../components/ScanFunnel';
import { ExemplarCard } from '../ExemplarCard';
import {
  ago,
  pendingAwareCaption,
  money,
  riskProfileCaption,
  ruleLabel,
  signedMoney,
  stillMarkingCaption,
  tone,
  emptyBucketCaption,
} from '../format';
import type { Currency } from '../format';
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
import type { Tone } from '../primitives';

function wouldHaveTone(ghostPnl: number | null | undefined): Tone {
  if (ghostPnl == null) return 'neutral';
  if (ghostPnl < 0) return 'bull';
  if (ghostPnl > 0) return 'warn';
  return 'neutral';
}

function wouldHaveLabel(ghostPnl: number | null | undefined): string {
  if (ghostPnl == null) return 'pending';
  if (ghostPnl < 0) return 'saved';
  if (ghostPnl > 0) return 'missed';
  return 'even';
}

/** The per-rule "would have" cell — §4.1/§4.3 of the doc: a ghost with no
 * mark at all renders the literal word "pending", never `$0`; a positive
 * ghost (money the veto COST, not saved) renders amber "missed" rather than
 * being hidden next to the wins.
 *
 * Falls back to `markedPnl` when `ghostPnl` is null, labelled "so far". A
 * ghost finalizes only after `horizonDays` TRADING days, so on 2026-09-04
 * all 101 marked refusals were still `partial` and every row here rendered
 * "pending" — while the table already held real counterfactuals priced
 * against live Alpaca option quotes. Showing nothing was strictly less
 * honest than showing a provisional number that says it is provisional. */
function WouldHaveCell({ rule, currency }: { rule: VetoRuleDto; currency: Currency }) {
  const settled = rule.ghostPnl;
  const provisional = settled == null ? rule.markedPnl : null;
  const value = settled ?? provisional;

  if (value == null) {
    return <span className="pg-caption pg-dim">pending</span>;
  }
  return (
    <Row gap={6} style={{ justifyContent: 'flex-end' }}>
      <span className="pg-num">{signedMoney(value, currency)}</span>
      <Pill tone={wouldHaveTone(value)}>{wouldHaveLabel(value)}</Pill>
      {provisional != null ? (
        <span className="pg-caption pg-dim">so far</span>
      ) : null}
    </Row>
  );
}

/** Currency-aware twins of format.ts's `ghostSideUsd` / `pendingAwareUsd`
 * (which are USD-only). Same rules, same wording; only the symbol differs. */
function ghostSideMoney(
  settled: number | null | undefined,
  sideAmount: number | null | undefined,
  currency: Currency,
): string | null {
  if (settled != null && settled !== 0) return money(settled, currency);
  if (sideAmount != null && sideAmount !== 0) return money(sideAmount, currency);
  return null;
}

function pendingAwareMoney(
  amount: number | null | undefined,
  pendingCount: number,
  markedAmount: number | null | undefined,
  currency: Currency,
): string {
  if (pendingCount > 0 && (amount == null || amount === 0)) {
    if (markedAmount != null && markedAmount !== 0) {
      return `${money(markedAmount, currency)} so far · ${pendingCount} still marking`;
    }
    const sym = currency === 'INR' ? '₹' : '$';
    return `${sym}— · ${pendingCount} mark${pendingCount === 1 ? '' : 's'} pending`;
  }
  return money(amount, currency);
}

const MARKETS: { id: InsightsMarket; label: string }[] = [
  { id: 'US', label: 'USD' },
  { id: 'IN', label: 'INR' },
];

const FUNNEL_WINDOW_DAYS = 30;

type Tab = 'vetoes' | 'ghost' | 'calibration';

const TABS: { id: Tab; label: string }[] = [
  { id: 'vetoes', label: 'Veto ledger' },
  { id: 'ghost', label: 'Ghost P&L' },
  { id: 'calibration', label: 'Calibration' },
];

export function InsightsScreen() {
  const [tab, setTab] = useState<Tab>('vetoes');
  const [selectedRule, setSelectedRule] = useState<string | null>(null);
  const [market, setMarket] = useState<InsightsMarket>('US');
  const vetoes = useVetoLedger(30, market);
  const ghost = useGhostSummary(30, market);
  // Prefer what the server says it returned; fall back to the toggle.
  const fallbackCurrency: Currency = market === 'IN' ? 'INR' : 'USD';
  const vetoCurrency: Currency = vetoes.data?.currency ?? fallbackCurrency;
  const ghostCurrency: Currency = ghost.data?.currency ?? fallbackCurrency;
  const scorecard = useCalibrationScorecard(180);
  const funnel = useFunnel(FUNNEL_WINDOW_DAYS);
  const scanFunnel = useScanFunnel();

  return (
    <>
      <PageHead
        title="Insights"
        sub="What the risk engine stopped, what it cost, and whether your overrides beat the loop."
        right={
          <Row gap={8} style={{ flexWrap: 'wrap' }}>
            <div role="group" aria-label="Ledger currency" style={{ display: 'flex', gap: 4 }}>
              {MARKETS.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className={`pg-btn pg-btn-${market === m.id ? 'primary' : 'secondary'} pg-btn-sm`}
                  onClick={() => {
                    setMarket(m.id);
                    setSelectedRule(null);
                  }}
                  aria-pressed={market === m.id}
                  aria-label={`Show the ${m.label} ledger`}
                >
                  {m.label}
                </button>
              ))}
            </div>
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

      {scanFunnel.isError ? (
        <div className="pg-grid pg-fade-up">
          <Cell span={12}>
            <DataStreamInterrupted
              code="SCAN_FUNNEL_READ_FAILED"
              node="api · /v1/insights/scan-funnel"
              onRetry={() => void scanFunnel.refetch()}
              compact
            />
          </Cell>
        </div>
      ) : (
        <div className="pg-grid pg-fade-up">
          <Cell span={12}>
            <ScanFunnel
              universe={scanFunnel.data?.universe ?? { eligibleCount: null, examinedCount: null, refreshedAt: null }}
              sweep={scanFunnel.data?.sweep ?? null}
              loading={scanFunnel.isLoading}
            />
          </Cell>
        </div>
      )}

      {funnel.isError ? (
        <div className="pg-grid pg-fade-up">
          <Cell span={12}>
            <DataStreamInterrupted
              code="FUNNEL_READ_FAILED"
              node="api · /v1/insights/funnel"
              onRetry={() => void funnel.refetch()}
              compact
            />
          </Cell>
        </div>
      ) : (
        <div className="pg-grid pg-fade-up">
          <Cell span={12}>
            <ContractFunnel
              stages={funnel.data?.aggregate.stages ?? []}
              loading={funnel.isLoading}
              windowDays={FUNNEL_WINDOW_DAYS}
              runs={funnel.data?.aggregate.runs}
              bought={funnel.data?.aggregate.bought}
            />
          </Cell>
        </div>
      )}

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
                value={vetoes.data ? money(vetoes.data.totalBlockedNotional, vetoCurrency) : '—'}
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
            <Cell span={selectedRule && market === 'US' ? 8 : 12}>
              <Card>
                <CardHead label="Rules that fired" right={<Pill>DETERMINISTIC</Pill>} />
                <span className="pg-caption" style={{ display: 'block', marginTop: -6, marginBottom: 4 }}>
                  {vetoes.data ? riskProfileCaption(vetoes.data.riskProfile) : ' '}
                </span>
                {vetoes.isLoading || !vetoes.data || vetoes.data.rules.length === 0 ? (
                  <SkelRows rows={6} h={20} />
                ) : (
                  <div style={{ overflowX: 'auto' }}>
                    <table className="pg-table">
                      <thead>
                        <tr>
                          <th>Rule</th>
                          <th className="pg-num-right">Fired</th>
                          <th className="pg-num-right">Blocked notional</th>
                          <th className="pg-num-right">Would have</th>
                          <th className="pg-num-right">Last</th>
                        </tr>
                      </thead>
                      <tbody>
                        {vetoes.data.rules.map((r) => (
                          <tr
                            key={r.rule}
                            className="pg-row-btn"
                            // The story-trade exemplar is a US-ledger lookup; don't
                            // open a dollar trade inside the rupee ledger.
                            onClick={
                              market === 'US'
                                ? () => setSelectedRule(r.rule === selectedRule ? null : r.rule)
                                : undefined
                            }
                          >
                            <td>
                              <span className="pg-num" style={{ fontSize: 13 }}>
                                {ruleLabel(r.rule)}
                              </span>
                            </td>
                            <td className="pg-num-right">{r.count}</td>
                            <td className="pg-num-right">{money(r.blockedNotional, vetoCurrency)}</td>
                            <td className="pg-num-right">
                              <WouldHaveCell rule={r} currency={vetoCurrency} />
                            </td>
                            <td className="pg-num-right pg-dim">{ago(r.lastAt)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </Cell>

            {selectedRule && market === 'US' ? (
              <Cell span={4}>
                <ExemplarCard rule={selectedRule} onClose={() => setSelectedRule(null)} />
              </Cell>
            ) : null}

            <Cell span={12}>
              <Card>
                <CardHead
                  label={
                    vetoes.data
                      ? `Risk also shrank ${vetoes.data.totalTrims} trade${vetoes.data.totalTrims === 1 ? '' : 's'}`
                      : 'Risk also shrank trades'
                  }
                  right={<Pill tone="warn">TRIM · NOT A VETO</Pill>}
                />
                {vetoes.isLoading || !vetoes.data ? (
                  <SkelRows rows={2} h={18} />
                ) : vetoes.data.trims.length === 0 ? (
                  <div className="pg-empty">
                    <p className="pg-empty-body">No trims in this window — every approved trade sized as proposed.</p>
                  </div>
                ) : (
                  <>
                    <span className="pg-caption" style={{ display: 'block', marginBottom: 8 }}>
                      A trim resized an APPROVED trade smaller; it never counts toward the {vetoes.data.totalVetoes}{' '}
                      vetoes above — a veto let nothing through, a trim let a smaller trade through.
                    </span>
                    <table className="pg-table">
                      <thead>
                        <tr>
                          <th>Rule</th>
                          <th className="pg-num-right">Trades resized</th>
                        </tr>
                      </thead>
                      <tbody>
                        {vetoes.data.trims.map((t) => (
                          <tr key={t.rule}>
                            <td>
                              <span className="pg-num" style={{ fontSize: 13 }}>
                                {ruleLabel(t.rule)}
                              </span>
                            </td>
                            <td className="pg-num-right">{t.count}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
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
                value={
                  ghost.data
                    ? ghostSideMoney(ghost.data.savedUsd, ghost.data.vetoed.lossAvoidedUsd, ghostCurrency) ??
                      pendingAwareMoney(
                        ghost.data.savedUsd,
                        ghost.data.vetoed.pendingCount,
                        ghost.data.savedSoFarUsd,
                        ghostCurrency,
                      )
                    : '—'
                }
                caption={
                  ghost.data
                    ? ghost.data.vetoed.upsideBlockedUsd
                      ? `vs ${money(ghost.data.vetoed.upsideBlockedUsd, ghostCurrency)} of upside these vetoes also blocked`
                      : pendingAwareCaption(
                          'What vetoed picks would have lost',
                          ghost.data.savedUsd,
                          ghost.data.vetoed.pendingCount,
                          ghost.data.vetoed.oldestPendingRemainingTradingDays,
                        )
                    : 'What vetoed picks would have lost'
                }
                tone="bull"
                loading={ghost.isLoading}
              />
            </Cell>
            <Cell span={6}>
              <StatTile
                label="Upside missed"
                value={
                  ghost.data
                    ? ghost.data.declined.count === 0
                      ? '—'
                      : ghostSideMoney(
                          ghost.data.missedUsd,
                          ghost.data.declined.upsideBlockedUsd,
                          ghostCurrency,
                        ) ??
                        pendingAwareMoney(
                          ghost.data.missedUsd,
                          ghost.data.declined.pendingCount,
                          ghost.data.missedSoFarUsd,
                          ghostCurrency,
                        )
                    : '—'
                }
                caption={
                  ghost.data
                    ? emptyBucketCaption(
                        ghost.data.declined.count,
                        'No picks declined by hand — auto-approve is on',
                      ) ??
                      pendingAwareCaption(
                        'What declined picks would have made',
                        ghost.data.missedUsd,
                        ghost.data.declined.pendingCount,
                        ghost.data.declined.oldestPendingRemainingTradingDays,
                      )
                    : 'What declined picks would have made'
                }
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
                      {signedMoney(ghost.data.vetoed.ghostPnl, ghostCurrency)}
                    </Numeral>
                    <span className="pg-body-sm">
                      {stillMarkingCaption(
                        ghost.data.vetoed.count,
                        ghost.data.vetoed.pendingCount,
                        ghost.data.vetoed.oldestPendingRemainingTradingDays,
                      )}
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
                      {signedMoney(ghost.data.declined.ghostPnl, ghostCurrency)}
                    </Numeral>
                    <span className="pg-body-sm">
                      {stillMarkingCaption(
                        ghost.data.declined.count,
                        ghost.data.declined.pendingCount,
                        ghost.data.declined.oldestPendingRemainingTradingDays,
                      )}
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
