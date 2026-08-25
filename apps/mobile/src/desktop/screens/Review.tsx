/** Review — grade closed decisions; the operator half of the calibration loop. */

import { useGradeDecision, useReviewAgreement, useReviewQueue } from '@/hooks/useReview';
import type { Grade, ReviewQueueItem } from '@/hooks/useReview';

import { ago, signedUsd, tone, usd } from '../format';
import { IconCheck, IconCross } from '../icons';
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
  ScoreBar,
  SkelRows,
  Stack,
  StatTile,
} from '../primitives';

export function ReviewScreen() {
  const queue = useReviewQueue(30);
  const agreement = useReviewAgreement(30);
  const grade = useGradeDecision(30);

  if (queue.isError) {
    return (
      <DataStreamInterrupted code="REVIEW_READ_FAILED" node="api · /v1/review/queue" onRetry={() => void queue.refetch()} />
    );
  }

  const items = queue.data?.items ?? [];
  const head = items[0] ?? null;

  return (
    <>
      <PageHead
        title="Review"
        sub="Grade what closed. Agreement with the Reflection loop is how the agent earns autonomy."
        right={
          <Pill>
            {queue.data ? `${queue.data.gradedInWindow}/${queue.data.totalInWindow} GRADED` : 'LOADING'}
          </Pill>
        }
      />

      <div className="pg-grid pg-fade-up">
        <Cell span={4}>
          <StatTile
            label="Agreement"
            value={agreement.data ? `${agreement.data.agreementPct.toFixed(0)}%` : '—'}
            caption={agreement.data ? `${agreement.data.totalReviewed} reviewed · 30d` : undefined}
            tone={agreement.data && agreement.data.agreementPct >= 60 ? 'bull' : 'neutral'}
            loading={agreement.isLoading}
          />
        </Cell>
        <Cell span={4}>
          <StatTile label="Queue" value={String(items.length)} caption="Awaiting your grade" loading={queue.isLoading} />
        </Cell>
        <Cell span={4}>
          <StatTile
            label="Graded"
            value={queue.data ? String(queue.data.gradedInWindow) : '—'}
            caption="This window"
            loading={queue.isLoading}
          />
        </Cell>

        <Cell span={7}>
          {queue.isLoading ? (
            <Card>
              <SkelRows rows={6} h={20} />
            </Card>
          ) : head == null ? (
            <Card style={{ gap: 10 }}>
              <Label>Queue clear</Label>
              <p className="pg-body-lg">Nothing left to grade in this window.</p>
              <p className="pg-body-sm">Closed decisions land here the day after they settle.</p>
            </Card>
          ) : (
            <GradeCard
              item={head}
              busy={grade.isPending}
              onGrade={(g) => grade.mutate({ decisionId: head.decisionId, grade: g })}
            />
          )}
        </Cell>

        <Cell span={5}>
          <Card>
            <CardHead label="Up next" />
            {queue.isLoading || items.length <= 1 ? (
              <SkelRows rows={4} h={18} />
            ) : (
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>Ticker</th>
                    <th>Closed</th>
                    <th className="pg-num-right">Realised</th>
                  </tr>
                </thead>
                <tbody>
                  {items.slice(1, 8).map((item) => (
                    <tr key={item.decisionId}>
                      <td>
                        <Numeral size={14} weight={600}>
                          {item.symbol}
                        </Numeral>
                      </td>
                      <td className="pg-caption">{ago(item.triggeredAt)}</td>
                      <td className="pg-num-right">
                        <span className={(item.realizedPnl ?? 0) < 0 ? 'pg-bear' : 'pg-bull'}>
                          {signedUsd(item.realizedPnl)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </Cell>
      </div>
    </>
  );
}

function GradeCard({
  item,
  busy,
  onGrade,
}: {
  item: ReviewQueueItem;
  busy: boolean;
  onGrade: (grade: Grade) => void;
}) {
  return (
    <Card style={{ gap: 16 }}>
      <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <Stack gap={4}>
          <Row gap={12}>
            <span className="pg-h2 pg-num">{item.symbol}</span>
            <Pill tone={item.side === 'BUY' ? 'bull' : 'bear'}>{item.side}</Pill>
            {item.regime ? <Pill>{item.regime.toUpperCase()}</Pill> : null}
          </Row>
          <span className="pg-caption">
            {item.selectedStrategy ?? 'strategy unrecorded'} · triggered {ago(item.triggeredAt)}
          </span>
        </Stack>
        <Stack gap={4} style={{ alignItems: 'flex-end' }}>
          <Label>Realised</Label>
          <Numeral size={30} tone={tone(item.realizedPnl)}>
            {signedUsd(item.realizedPnl)}
          </Numeral>
        </Stack>
      </Row>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 12 }}>
        <Cellette label="Qty" value={item.fillQty != null ? String(item.fillQty) : item.qty != null ? String(item.qty) : '—'} />
        <Cellette label="Avg fill" value={item.fillAvgPrice != null ? usd(item.fillAvgPrice, 2) : '—'} />
        <Cellette label="Selector conf" value={`${Math.round(item.selectorConfidence * 100)}%`} />
      </div>

      <Stack gap={6}>
        <Row style={{ justifyContent: 'space-between' }}>
          <Label>Selector confidence</Label>
          <span className="pg-caption pg-num">{Math.round(item.selectorConfidence * 100)}/100</span>
        </Row>
        <ScoreBar score={item.selectorConfidence * 100} />
      </Stack>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div className="pg-inset" style={{ backgroundImage: 'linear-gradient(160deg, var(--pg-bull-wash), transparent 60%)' }}>
          <Label style={{ color: 'var(--pg-bull-text)' }}>Bull case</Label>
          <p className="pg-body-sm" style={{ marginTop: 6 }}>
            {item.bullCase}
          </p>
        </div>
        <div className="pg-inset" style={{ backgroundImage: 'linear-gradient(160deg, var(--pg-bear-wash), transparent 60%)' }}>
          <Label style={{ color: 'var(--pg-bear-text)' }}>Bear case</Label>
          <p className="pg-body-sm" style={{ marginTop: 6 }}>
            {item.bearCase}
          </p>
        </div>
      </div>

      <Row gap={12}>
        <Button kind="primary" onClick={() => onGrade('good')} disabled={busy} ariaLabel={`Grade the ${item.symbol} decision good`} style={{ flex: 1 }}>
          <IconCheck size={16} />
          Good call
        </Button>
        <Button onClick={() => onGrade('bad')} disabled={busy} ariaLabel={`Grade the ${item.symbol} decision bad`} style={{ flex: 1 }}>
          <IconCross size={16} />
          Bad call
        </Button>
        <Button kind="ghost" onClick={() => onGrade('skip')} disabled={busy} ariaLabel={`Skip grading ${item.symbol}`}>
          Skip
        </Button>
      </Row>
    </Card>
  );
}

function Cellette({ label, value }: { label: string; value: string }) {
  return (
    <div className="pg-inset">
      <Label>{label}</Label>
      <div style={{ marginTop: 6 }}>
        <Numeral size={16}>{value}</Numeral>
      </div>
    </div>
  );
}
