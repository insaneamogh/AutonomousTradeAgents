/**
 * Picks — the approval queue, plus the on-demand council launcher.
 *
 * Data comes straight from the existing `usePendingApprovals` /
 * `useWatchlist` / `useStartCouncilRun` hooks. Nothing new is fetched.
 */

import { useState } from 'react';

import { usePendingApprovals } from '@/hooks/useApprovals';
import { useStartCouncilRun } from '@/hooks/useCouncilRun';
import { useWatchlist } from '@/hooks/useWatchlist';
import type { ApprovalProposalDto } from '@app/shared-types';

import { ago, usd } from '../format';
import { useNav } from '../nav';
import {
  Button,
  Card,
  CardHead,
  Cell,
  DataStreamInterrupted,
  Label,
  Numeral,
  Pill,
  Row,
  ScoreBar,
  ScorePill,
  Skel,
  SkelRows,
  Stack,
  PageHead,
} from '../primitives';
import { IconSpark } from '../icons';

export function PicksScreen() {
  const pending = usePendingApprovals();
  const { go } = useNav();

  if (pending.isError) {
    return (
      <DataStreamInterrupted
        code="APPROVALS_READ_FAILED"
        node="api · /v1/approvals/pending"
        onRetry={() => void pending.refetch()}
      />
    );
  }

  const picks = pending.data ?? [];

  return (
    <>
      <PageHead
        title="Picks"
        sub="Every proposal the council cleared. Nothing executes until you approve it."
        right={<Pill tone={picks.length > 0 ? 'bull' : 'neutral'}>{picks.length} PENDING</Pill>}
      />

      <div className="pg-grid pg-fade-up">
        <Cell span={8}>
          <Stack gap={20} style={{ flex: 1 }}>
            {pending.isLoading ? (
              <>
                <Card>
                  <SkelRows rows={4} h={20} />
                </Card>
                <Card>
                  <SkelRows rows={4} h={20} />
                </Card>
              </>
            ) : picks.length === 0 ? (
              <Card style={{ gap: 10 }}>
                <Label>Inbox clear</Label>
                <p className="pg-body-lg">No pick is waiting on you.</p>
                <p className="pg-body-sm">
                  Run the council on a watchlist name to generate one, or wait for the daily pass.
                </p>
              </Card>
            ) : (
              picks.map((pick) => <PickCard key={pick.id} pick={pick} onOpen={() => go({ name: 'pick', id: pick.id })} />)
            )}
          </Stack>
        </Cell>

        <Cell span={4}>
          <CouncilLauncher />
        </Cell>
      </div>
    </>
  );
}

function PickCard({ pick, onOpen }: { pick: ApprovalProposalDto; onOpen: () => void }) {
  const conviction = pick.convictionLevel * 20;
  return (
    <Card>
      <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <Stack gap={4}>
          <Row gap={10}>
            <Numeral size={30} weight={600}>
              {pick.symbol}
            </Numeral>
            <Pill tone={pick.side === 'BUY' ? 'bull' : 'bear'}>{pick.side}</Pill>
          </Row>
          <span className="pg-caption">
            {pick.qty} shares · {pick.orderType === 'LIMIT' && pick.limitPrice ? usd(pick.limitPrice, 2) : 'market'} ·
            proposed {ago(pick.proposedAt)}
          </span>
        </Stack>
        <Stack gap={6} style={{ alignItems: 'flex-end' }}>
          <ScorePill score={conviction} />
          <Numeral size={20}>{usd(pick.estimatedNotional)}</Numeral>
        </Stack>
      </Row>

      <p className="pg-body-sm" style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
        {pick.rationale}
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: 12 }}>
        <PlanStat label="Stop" value={pick.stopLoss != null ? usd(pick.stopLoss, 2) : '—'} />
        <PlanStat label="Target" value={pick.targetPrice != null ? usd(pick.targetPrice, 2) : '—'} />
        <PlanStat label="R:R" value={pick.rMultiple != null ? `${pick.rMultiple.toFixed(2)}R` : '—'} />
        <PlanStat label="Time stop" value={pick.timeStopDays != null ? `${pick.timeStopDays}d` : '—'} />
      </div>

      <Stack gap={6}>
        <Row style={{ justifyContent: 'space-between' }}>
          <Label>Conviction</Label>
          <span className="pg-caption pg-num">{conviction}/100</span>
        </Row>
        <ScoreBar score={conviction} />
      </Stack>

      <Row style={{ justifyContent: 'space-between' }}>
        {pick.expiresAt ? <span className="pg-caption">Auto-declines {ago(pick.expiresAt)}</span> : <span />}
        <Button kind="primary" onClick={onOpen} ariaLabel={`Review the ${pick.symbol} pick`}>
          Review pick →
        </Button>
      </Row>
    </Card>
  );
}

function PlanStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="pg-inset">
      <Label>{label}</Label>
      <div style={{ marginTop: 6 }}>
        <Numeral size={16}>{value}</Numeral>
      </div>
    </div>
  );
}

/** Kicks off a council run and drops the user straight into the theater. */
function CouncilLauncher() {
  const watchlist = useWatchlist();
  const start = useStartCouncilRun();
  const { go } = useNav();
  const [symbol, setSymbol] = useState('');

  const run = (raw: string) => {
    const ticker = raw.trim().toUpperCase();
    if (!ticker) return;
    start.mutate(
      { symbol: ticker, horizon: 'short' },
      { onSuccess: (res) => go({ name: 'council', runId: res.runId, symbol: res.symbol }) },
    );
  };

  return (
    <Card style={{ gap: 16, alignSelf: 'flex-start' }}>
      <CardHead
        label="Run the council"
        right={
          <span style={{ color: 'var(--pg-bull-text)' }}>
            <IconSpark size={16} />
          </span>
        }
      />
      <p className="pg-body-sm">
        Seven nodes deliberate live — Router, three analysts, Selector, Drafter, Risk Officer. The risk verdict is
        deterministic Python, not the model.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(symbol);
        }}
        style={{ display: 'flex', gap: 8 }}
      >
        <input
          className="pg-input"
          style={{ flex: 1, minWidth: 0 }}
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="Ticker, e.g. NVDA"
          aria-label="Ticker to run the council on"
          maxLength={8}
        />
        <Button kind="primary" type="submit" disabled={start.isPending || symbol.trim().length === 0} ariaLabel="Start council run">
          {start.isPending ? 'Starting…' : 'Run'}
        </Button>
      </form>

      {start.isError ? (
        <span className="pg-body-sm pg-bear">Couldn’t start the run. The agent server may be cold.</span>
      ) : null}

      <Stack gap={8}>
        <Label>Watchlist</Label>
        {watchlist.isLoading ? (
          <Row gap={8}>
            <Skel h={30} w={70} r={999} />
            <Skel h={30} w={70} r={999} />
            <Skel h={30} w={70} r={999} />
          </Row>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {(watchlist.data ?? []).map((item) => (
              <button
                key={item.id}
                type="button"
                className="pg-btn pg-btn-secondary pg-btn-sm"
                onClick={() => run(item.symbol)}
                disabled={start.isPending}
                aria-label={`Run the council on ${item.symbol}`}
              >
                <span className="pg-num">{item.symbol}</span>
              </button>
            ))}
          </div>
        )}
      </Stack>
    </Card>
  );
}
