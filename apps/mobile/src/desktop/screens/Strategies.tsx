/** Strategies — per-strategy performance from the Reflection ledger. */

import { useStrategiesPerformance } from '@/hooks/useStrategiesPerformance';

import { ago, signedPct, signedUsd, tone } from '../format';
import { useNav } from '../nav';
import {
  Button,
  Card,
  CardHead,
  Cell,
  DataStreamInterrupted,
  Gauge,
  Label,
  Numeral,
  PageHead,
  Pill,
  Row,
  SkelRows,
  Stack,
  StatTile,
} from '../primitives';

export function StrategiesScreen() {
  const perf = useStrategiesPerformance(30);
  const { go } = useNav();

  if (perf.isError) {
    return (
      <DataStreamInterrupted
        code="STRATEGIES_READ_FAILED"
        node="api · /v1/strategies/performance"
        onRetry={() => void perf.refetch()}
      />
    );
  }

  const rows = perf.data?.strategies ?? [];
  const decisions = rows.reduce((s, r) => s + r.decisionsInWindow, 0);
  const wins = rows.reduce((s, r) => s + r.wins, 0);
  const losses = rows.reduce((s, r) => s + r.losses, 0);
  const realized = rows.reduce((s, r) => s + r.realizedPnl, 0);
  const hitRate = wins + losses > 0 ? (wins / (wins + losses)) * 100 : null;

  return (
    <>
      <PageHead
        title="Strategies"
        sub="Confidence is earned: the Reflection loop moves it, not the model’s self-report."
        right={<Pill>{perf.data ? `${perf.data.windowDays}D WINDOW` : '30D WINDOW'}</Pill>}
      />

      <div className="pg-grid pg-fade-up">
        <Cell span={3}>
          <StatTile label="Strategies" value={String(rows.length)} loading={perf.isLoading} caption="Registered and eligible" />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Decisions"
            value={String(decisions)}
            loading={perf.isLoading}
            caption="In the window"
            accessory={
              <Button size="sm" kind="ghost" onClick={() => go({ name: 'decisions' })} ariaLabel="Browse every decision">
                View all →
              </Button>
            }
          />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Hit rate"
            value={hitRate != null ? `${hitRate.toFixed(0)}%` : '—'}
            caption={`${wins}W · ${losses}L`}
            tone={hitRate != null && hitRate >= 50 ? 'bull' : 'neutral'}
            loading={perf.isLoading}
          />
        </Cell>
        <Cell span={3}>
          <StatTile
            label="Realised P&L"
            value={signedUsd(realized)}
            caption="Closed trades in the window"
            tone={tone(realized)}
            loading={perf.isLoading}
          />
        </Cell>

        {perf.isLoading || rows.length === 0 ? (
          <Cell span={12}>
            <Card>
              <CardHead label="Per-strategy performance" />
              <SkelRows rows={5} h={22} />
            </Card>
          </Cell>
        ) : (
          rows.map((s) => (
            <Cell span={4} key={s.strategyId}>
              <Card style={{ gap: 14 }}>
                <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <Stack gap={3}>
                    <span className="pg-h3" style={{ fontSize: 18 }}>
                      {s.displayName}
                    </span>
                    <span className="pg-caption pg-num">{s.strategyId}</span>
                  </Stack>
                  <Pill tone={s.realizedPnl > 0 ? 'bull' : s.realizedPnl < 0 ? 'bear' : 'neutral'}>
                    {signedUsd(s.realizedPnl)}
                  </Pill>
                </Row>

                <Row gap={20} style={{ alignItems: 'center' }}>
                  {/* Hit rate is the honest headline once there are graded
                      trades; before that, show the Reflection confidence. */}
                  {s.wins + s.losses > 0 ? (
                    <Gauge value={(s.wins / (s.wins + s.losses)) * 100} size={96} label="hit rate" suffix="%" />
                  ) : (
                    <Gauge value={s.confidence * 100} size={96} label="confidence" />
                  )}
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0,1fr))', gap: 12, flex: 1 }}>
                    <MiniCell label="Decisions" value={String(s.decisionsInWindow)} />
                    <MiniCell label="Record" value={`${s.wins}–${s.losses}`} />
                    <MiniCell
                      label="Avg win"
                      value={s.avgWinnerPct != null ? signedPct(s.avgWinnerPct, 1) : '—'}
                    />
                    <MiniCell label="Confidence" value={`${Math.round(s.confidence * 100)}/100`} />
                  </div>
                </Row>

                <span className="pg-caption">
                  Last decision {ago(s.lastDecisionAt)} · last reflection {ago(s.lastReflectionAt)}
                </span>
              </Card>
            </Cell>
          ))
        )}
      </div>
    </>
  );
}

function MiniCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="pg-inset">
      <Label>{label}</Label>
      <div style={{ marginTop: 6 }}>
        <Numeral size={15}>{value}</Numeral>
      </div>
    </div>
  );
}
