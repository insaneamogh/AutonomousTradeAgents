/**
 * Dashboard — the money screen. 12-col bento (§7.2), `gap-5`.
 *
 *   hero equity (col-8)          ·  pulse rail (col-4)
 *   4-up stat cards
 *   Opportunity Radar (col-7)    ·  Agent Activity (col-5)
 *   Ghost P&L (col-5)            ·  Veto ledger (col-7)
 *   System health (col-12)
 *
 * Every card renders shimmer while pending OR empty — the dashboard never
 * prints "No data" (§8.2).
 */

import { useAccount } from '@/hooks/useAccount';
import { useActivity } from '@/hooks/useActivity';
import { usePendingApprovals } from '@/hooks/useApprovals';
import { useHealthFull } from '@/hooks/useHealthFull';
import type { ComponentHealth, ComponentStatus } from '@/hooks/useHealthFull';
import { useGhostSummary, useVetoLedger } from '@/hooks/useInsights';
import { useOpenPositions } from '@/hooks/usePositions';

import { ago, ruleLabel, signedPct, signedUsd, tone, usd } from '../format';
import { useNav } from '../nav';
import {
  Button,
  Card,
  CardHead,
  Cell,
  DataStreamInterrupted,
  DeltaPill,
  Label,
  Numeral,
  Pill,
  Row,
  ScorePill,
  Skel,
  SkelRows,
  Stack,
  StatTile,
} from '../primitives';
import { useRegime } from '../regime';

export function DashboardScreen() {
  const account = useAccount();
  const positions = useOpenPositions();
  const pending = usePendingApprovals();
  const activity = useActivity(12);
  const health = useHealthFull();
  const ghost = useGhostSummary(30);
  const vetoes = useVetoLedger(30);
  const regime = useRegime();
  const { go } = useNav();

  // The account read is the spine of this screen — if it's gone, the whole
  // page is a lie. Everything else degrades to shimmer inside its own card.
  if (account.isError) {
    return (
      <DataStreamInterrupted
        code="ACCOUNT_READ_FAILED"
        node="api · /v1/account"
        onRetry={() => void account.refetch()}
      />
    );
  }

  const acct = account.data;
  const deployed = acct ? Math.max(0, acct.equity - acct.cash) : 0;
  const deployedPct = acct && acct.equity > 0 ? (deployed / acct.equity) * 100 : 0;

  return (
    <>
      <div className="pg-grid pg-fade-up">
        {/* ── Hero: portfolio ─────────────────────────────────── */}
        <Cell span={8}>
          <Card variant="hero" style={{ gap: 20 }}>
            <CardHead
              label={acct ? `Portfolio · ${acct.brokerName}` : 'Portfolio'}
              right={
                acct ? (
                  <Row gap={8}>
                    <Pill>{acct.isPaper ? 'PAPER' : 'LIVE'}</Pill>
                    <Pill tone={acct.status === 'connected' ? 'bull' : 'bear'}>
                      {acct.status.toUpperCase()}
                    </Pill>
                  </Row>
                ) : null
              }
            />

            {acct ? (
              <Row gap={20} style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
                <span className="pg-num-hero">{usd(acct.equity)}</span>
                <div style={{ paddingBottom: 8 }}>
                  <DeltaPill
                    text={`${signedUsd(acct.todayPnl)} · ${signedPct(acct.todayPnlPct)}`}
                    tone={tone(acct.todayPnl)}
                  />
                </div>
              </Row>
            ) : (
              <Skel h={60} w="55%" />
            )}

            <Stack gap={8}>
              <Row style={{ justifyContent: 'space-between' }}>
                <Label>Capital deployed</Label>
                <span className="pg-num pg-dim" style={{ fontSize: 12 }}>
                  {acct ? `${deployedPct.toFixed(1)}%` : '—'}
                </span>
              </Row>
              <div className="pg-bar">
                <i style={{ width: `${Math.min(100, deployedPct)}%`, backgroundColor: 'var(--pg-primary-fixed-dim)' }} />
              </div>
            </Stack>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 12 }}>
              <MiniStat label="Cash" value={acct ? usd(acct.cash) : null} />
              <MiniStat label="Buying power" value={acct ? usd(acct.buyingPower) : null} />
              <MiniStat label="Deployed" value={acct ? usd(deployed) : null} />
            </div>
          </Card>
        </Cell>

        {/* ── Pulse rail (§6.3) ───────────────────────────────── */}
        <Cell span={4}>
          <Stack gap={20} style={{ flex: 1 }}>
            <PulseTile
              label="Market mode"
              value={regime.label}
              caption={regime.caption}
              toneName={regime.regime}
              loading={regime.loading}
            />
            <PulseTile
              label="Open positions"
              value={positions.data ? String(positions.data.length) : '—'}
              caption={
                positions.data && positions.data.length > 0
                  ? `${positions.data.filter((p) => p.exitMode === 'agent').length} agent-managed`
                  : 'Agent holds nothing right now'
              }
              toneName="neutral"
              loading={positions.isLoading}
              onOpen={() => go({ name: 'positions' })}
            />
            <PulseTile
              label="Pending picks"
              value={pending.data ? String(pending.data.length) : '—'}
              caption={
                pending.data && pending.data.length > 0
                  ? 'Awaiting your approval'
                  : 'Inbox clear'
              }
              toneName={pending.data && pending.data.length > 0 ? 'bull' : 'neutral'}
              loading={pending.isLoading}
              onOpen={() => go({ name: 'picks' })}
            />
          </Stack>
        </Cell>

        {/* ── 4-up stat cards ─────────────────────────────────── */}
        <Cell span={3}>
          <StatTile
            label="Today P&L"
            value={acct ? signedUsd(acct.todayPnl) : '—'}
            caption={acct ? `${signedPct(acct.todayPnlPct)} on ${usd(acct.equity)}` : undefined}
            tone={tone(acct?.todayPnl)}
            loading={account.isLoading}
          />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Risk saved"
            value={ghost.data ? usd(ghost.data.savedUsd) : '—'}
            caption="Losses avoided by vetoes · 30d"
            tone={ghost.data && ghost.data.savedUsd > 0 ? 'bull' : 'neutral'}
            loading={ghost.isLoading}
          />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Regret"
            value={ghost.data ? usd(ghost.data.missedUsd) : '—'}
            caption="Missed on declined picks · 30d"
            tone={ghost.data && ghost.data.missedUsd > 0 ? 'bear' : 'neutral'}
            loading={ghost.isLoading}
          />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Vetoes"
            value={vetoes.data ? String(vetoes.data.totalVetoes) : '—'}
            caption={vetoes.data ? `${usd(vetoes.data.totalBlockedNotional)} blocked · 30d` : undefined}
            loading={vetoes.isLoading}
          />
        </Cell>

        {/* ── Opportunity Radar (§6.5) ────────────────────────── */}
        <Cell span={7}>
          <Card>
            <CardHead
              label="Opportunity radar"
              right={
                pending.data && pending.data.length > 0 ? (
                  <Button size="sm" kind="ghost" onClick={() => go({ name: 'picks' })} ariaLabel="Open all picks">
                    All picks →
                  </Button>
                ) : null
              }
            />
            {pending.isLoading || !pending.data ? (
              <RadarSkeleton />
            ) : pending.data.length === 0 ? (
              /* Empty is NOT loading. The house rule is "skeleton, never
                 'No data'" for data that is *on its way*; a genuinely
                 empty inbox is a state, and shimmering at it forever
                 reads as a hung request. */
              <div className="pg-empty">
                <p className="pg-empty-title">Inbox clear</p>
                <p className="pg-empty-body">
                  Every proposal has been actioned. The next scheduled scan will
                  surface new candidates, or run the council on a ticker yourself.
                </p>
              </div>
            ) : (
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>Ticker</th>
                    <th>Plan</th>
                    <th className="pg-num-right">Notional</th>
                    <th className="pg-num-right">R:R</th>
                    <th className="pg-num-right">Conviction</th>
                  </tr>
                </thead>
                <tbody>
                  {pending.data.slice(0, 6).map((p) => (
                    <tr
                      key={p.id}
                      className="pg-row-btn"
                      tabIndex={0}
                      role="button"
                      aria-label={`Open the ${p.symbol} pick`}
                      onClick={() => go({ name: 'pick', id: p.id })}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          go({ name: 'pick', id: p.id });
                        }
                      }}
                    >
                      <td>
                        <Numeral size={16} weight={600}>
                          {p.symbol}
                        </Numeral>
                      </td>
                      <td>
                        <Stack gap={2}>
                          <span style={{ fontSize: 13 }}>
                            {p.side} {p.qty} @ {p.orderType === 'LIMIT' && p.limitPrice ? usd(p.limitPrice, 2) : 'MKT'}
                          </span>
                          <span className="pg-caption">{ago(p.proposedAt)}</span>
                        </Stack>
                      </td>
                      <td className="pg-num-right">{usd(p.estimatedNotional)}</td>
                      <td className="pg-num-right">{p.rMultiple != null ? `${p.rMultiple.toFixed(2)}R` : '—'}</td>
                      <td className="pg-num-right">
                        <ScorePill score={p.convictionLevel * 20} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </Cell>

        {/* ── Agent activity ──────────────────────────────────── */}
        <Cell span={5}>
          <Card>
            <CardHead label="Agent activity" />
            {activity.isLoading || !activity.data || activity.data.length === 0 ? (
              <SkelRows rows={6} />
            ) : (
              <Stack gap={0}>
                {activity.data.slice(0, 7).map((entry, i) => (
                  <Row
                    key={entry.id}
                    gap={12}
                    style={{
                      alignItems: 'flex-start',
                      padding: '10px 0',
                      borderTop: i === 0 ? undefined : '1px solid var(--pg-card-border)',
                    }}
                  >
                    <span
                      aria-hidden
                      style={{
                        width: 6,
                        height: 6,
                        borderRadius: 9999,
                        marginTop: 7,
                        flex: 'none',
                        backgroundColor: activityColor(entry.kind),
                      }}
                    />
                    <Stack gap={2} style={{ flex: 1 }}>
                      <Row gap={8}>
                        <Numeral size={13} weight={600}>
                          {entry.symbol}
                        </Numeral>
                        <span className="label-caps" style={{ fontSize: 10 }}>
                          {entry.kind}
                        </span>
                      </Row>
                      <span className="pg-body-sm pg-truncate" title={entry.headline}>
                        {entry.headline}
                      </span>
                    </Stack>
                    <span className="pg-caption pg-num" style={{ flex: 'none' }}>
                      {ago(entry.timestamp)}
                    </span>
                  </Row>
                ))}
              </Stack>
            )}
          </Card>
        </Cell>

        {/* ── Ghost P&L ───────────────────────────────────────── */}
        <Cell span={5}>
          <Card>
            <CardHead label="Ghost P&L · 30d" right={<Pill>COUNTERFACTUAL</Pill>} />
            {ghost.isLoading || !ghost.data ? (
              <SkelRows rows={3} h={26} />
            ) : (
              <Stack gap={12}>
                <GhostRow
                  title="Vetoed by risk"
                  count={ghost.data.vetoed.count}
                  pnl={ghost.data.vetoed.ghostPnl}
                  pending={ghost.data.vetoed.pendingCount}
                />
                <GhostRow
                  title="Declined by you"
                  count={ghost.data.declined.count}
                  pnl={ghost.data.declined.ghostPnl}
                  pending={ghost.data.declined.pendingCount}
                />
                <span className="pg-caption">
                  What the picks you didn’t take would have done, marked to the same horizon.
                </span>
              </Stack>
            )}
          </Card>
        </Cell>

        {/* ── Veto ledger ─────────────────────────────────────── */}
        <Cell span={7}>
          <Card>
            <CardHead
              label="Veto ledger · 30d"
              right={
                <Button size="sm" kind="ghost" onClick={() => go({ name: 'insights' })} ariaLabel="Open insights">
                  Insights →
                </Button>
              }
            />
            {vetoes.isLoading || !vetoes.data || vetoes.data.rules.length === 0 ? (
              <SkelRows rows={4} h={18} />
            ) : (
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th className="pg-num-right">Fired</th>
                    <th className="pg-num-right">Blocked</th>
                    <th className="pg-num-right">Last</th>
                  </tr>
                </thead>
                <tbody>
                  {vetoes.data.rules.slice(0, 6).map((rule) => (
                    <tr key={rule.rule}>
                      <td>
                        <span className="pg-num" style={{ fontSize: 13 }}>
                          {ruleLabel(rule.rule)}
                        </span>
                      </td>
                      <td className="pg-num-right">{rule.count}</td>
                      <td className="pg-num-right">{usd(rule.blockedNotional)}</td>
                      <td className="pg-num-right pg-dim">{ago(rule.lastAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </Cell>

        {/* ── System health ───────────────────────────────────── */}
        <Cell span={12}>
          <Card variant="dense">
            <CardHead
              label="System"
              right={health.data ? <span className="pg-caption">as of {ago(health.data.generatedAt)}</span> : null}
            />
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, minmax(0,1fr))', gap: 12 }}>
              {health.data
                ? (
                    [
                      ['Council', health.data.council],
                      ['Approvals', health.data.approvals],
                      ['Broker', health.data.broker],
                      ['Reconciler', health.data.reconciler],
                      ['LLM cost', health.data.llmCost],
                    ] as [string, ComponentHealth][]
                  ).map(([name, comp]) => <HealthTile key={name} name={name} comp={comp} />)
                : Array.from({ length: 5 }, (_, i) => <Skel key={i} h={62} />)}
            </div>
          </Card>
        </Cell>
      </div>
    </>
  );
}

/* ── pieces ──────────────────────────────────────────────────────── */

function MiniStat({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="pg-inset">
      <Label>{label}</Label>
      <div style={{ marginTop: 6 }}>
        {value == null ? <Skel h={18} w="70%" /> : <Numeral size={18}>{value}</Numeral>}
      </div>
    </div>
  );
}

function PulseTile({
  label,
  value,
  caption,
  toneName,
  loading,
  onOpen,
}: {
  label: string;
  value: string;
  caption: string;
  toneName: 'bull' | 'bear' | 'neutral';
  loading: boolean;
  onOpen?: () => void;
}) {
  const wash =
    toneName === 'bull'
      ? 'var(--pg-bull-wash)'
      : toneName === 'bear'
        ? 'var(--pg-bear-wash)'
        : 'transparent';
  const body = (
    <Card variant="dense" style={{ gap: 8, backgroundImage: `linear-gradient(135deg, ${wash}, transparent 60%)` }}>
      <Label>{label}</Label>
      {loading ? (
        <Skel h={26} w="55%" />
      ) : (
        <Numeral size={26} tone={toneName}>
          {value}
        </Numeral>
      )}
      {loading ? <Skel h={11} w="75%" /> : <span className="pg-caption pg-truncate">{caption}</span>}
    </Card>
  );

  if (!onOpen) return body;
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`${label}: ${value}`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen();
        }
      }}
      style={{ display: 'flex', cursor: 'pointer', borderRadius: 'var(--pg-r-xl)' }}
    >
      {body}
    </div>
  );
}

function GhostRow({
  title,
  count,
  pnl,
  pending,
}: {
  title: string;
  count: number;
  pnl: number;
  pending: number;
}) {
  return (
    <div className="pg-inset">
      <Row style={{ justifyContent: 'space-between' }}>
        <Stack gap={3}>
          <span style={{ fontSize: 13, fontWeight: 500 }}>{title}</span>
          <span className="pg-caption">
            {count} finalised{pending > 0 ? ` · ${pending} still open` : ''}
          </span>
        </Stack>
        <Numeral size={20} tone={tone(pnl)}>
          {signedUsd(pnl)}
        </Numeral>
      </Row>
    </div>
  );
}

function statusColor(status: ComponentStatus): string {
  if (status === 'ok') return 'var(--pg-bull)';
  if (status === 'danger') return 'var(--pg-error)';
  if (status === 'warning') return 'var(--pg-bear)';
  return 'var(--pg-outline)';
}

function HealthTile({ name, comp }: { name: string; comp: ComponentHealth }) {
  return (
    <div className="pg-inset">
      <Row gap={8}>
        <span
          aria-hidden
          style={{ width: 7, height: 7, borderRadius: 9999, backgroundColor: statusColor(comp.status), flex: 'none' }}
        />
        <span className="label-caps" style={{ fontSize: 10, color: 'var(--pg-on-surface-variant)' }}>
          {name}
        </span>
        <span className="pg-caption" style={{ marginLeft: 'auto' }}>
          {comp.status}
        </span>
      </Row>
      <div className="pg-caption" style={{ marginTop: 6 }}>
        {comp.label}
      </div>
    </div>
  );
}

function activityColor(kind: string): string {
  if (kind === 'filled' || kind === 'approved') return 'var(--pg-bull)';
  if (kind === 'vetoed' || kind === 'declined') return 'var(--pg-bear)';
  return 'var(--pg-outline)';
}

function RadarSkeleton() {
  return (
    <Stack gap={12}>
      {Array.from({ length: 4 }, (_, i) => (
        <Row key={i} gap={12}>
          <Skel h={16} w={64} />
          <Skel h={16} w="34%" />
          <div style={{ flex: 1 }} />
          <Skel h={16} w={84} />
          <Skel h={22} w={52} r={999} />
        </Row>
      ))}
    </Stack>
  );
}
