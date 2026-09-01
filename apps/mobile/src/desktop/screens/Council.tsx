/**
 * Council theater — the demo centrepiece.
 *
 * Node-by-node streaming of the LangGraph pass:
 *
 *   Router → Technical · Fundamental · Macro → Selector → Drafter → Risk Officer
 *
 * Every card flips pending → running → done as the polled progress feed
 * advances (`useCouncilProgress`, unchanged). Scores use the mode-locked
 * band palette; the Risk Officer verdict is mint when clear, rose-gold
 * when a named rule fires — and it is the only node whose output can stop
 * the trade, because it is deterministic Python.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { useCouncilProgress } from '@/hooks/useCouncilRun';
import type { AgentRunResponse, CouncilNode, CouncilProgressEvent } from '@app/shared-types';

import { clock, ruleLabel } from '../format';
import { IconBack, IconShield } from '../icons';
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
  Skel,
  Stack,
} from '../primitives';
import { scoreHex } from '../theme';

const ORDER: CouncilNode[] = [
  'router',
  'technical',
  'fundamental',
  'macro',
  'selector',
  'drafter',
  'risk_officer',
];

const LABEL: Record<CouncilNode, string> = {
  router: 'Router',
  technical: 'Technical',
  fundamental: 'Fundamental',
  macro: 'Macro',
  selector: 'Selector',
  drafter: 'Drafter',
  risk_officer: 'Risk officer',
};

const ROLE: Record<CouncilNode, string> = {
  router: 'Reads the regime and picks who deliberates',
  technical: 'Price structure, trend, momentum',
  fundamental: 'Earnings quality, valuation, balance sheet',
  macro: 'Rates, breadth, sector flows',
  selector: 'Chooses the strategy that fits the read',
  drafter: 'Sizes the order and writes the plan',
  risk_officer: 'Deterministic vetoes — the last gate',
};

type NodeState = 'pending' | 'running' | 'done' | 'skipped';

interface NodeRow {
  node: CouncilNode;
  state: NodeState;
  summary: Record<string, unknown> | null;
}

function buildRows(events: CouncilProgressEvent[]): NodeRow[] {
  const seen = new Map<CouncilNode, { status: string; summary: Record<string, unknown> | null }>();
  for (const e of events) seen.set(e.node, { status: e.status, summary: e.summary });
  return ORDER.map((node) => {
    const hit = seen.get(node);
    if (!hit) return { node, state: 'pending', summary: null };
    if (hit.status === 'skipped') return { node, state: 'skipped', summary: null };
    if (hit.status === 'completed') return { node, state: 'done', summary: hit.summary };
    return { node, state: 'running', summary: null };
  });
}

export function CouncilScreen({ runId, symbol }: { runId: string; symbol: string }) {
  const { data, isError, refetch } = useCouncilProgress(runId);
  const { back, go } = useNav();
  const rows = useMemo(() => buildRows(data?.events ?? []), [data?.events]);
  const running = data == null || data.status === 'running';
  const elapsed = useElapsed(running);

  if (isError) {
    return (
      <DataStreamInterrupted
        code="COUNCIL_STREAM_LOST"
        node={`agents · run ${runId.slice(0, 8)}`}
        onRetry={() => void refetch()}
      />
    );
  }

  if (data?.status === 'failed') {
    return (
      <DataStreamInterrupted
        code={data.error ?? 'RUN_FAILED'}
        node="agents · council graph"
        onRetry={() => void refetch()}
      />
    );
  }

  const byNode = (node: CouncilNode) => rows.find((r) => r.node === node) as NodeRow;

  return (
    <>
      <Row>
        <Button kind="ghost" onClick={back} ariaLabel="Back to picks">
          <IconBack size={16} />
          Picks
        </Button>
      </Row>

      <Row
        style={{
          justifyContent: 'space-between',
          alignItems: 'flex-end',
          gap: 20,
          flexWrap: 'wrap',
        }}
      >
        <Stack gap={8}>
          <Row gap={14}>
            <span className="pg-h1 pg-num">{symbol}</span>
            {running ? (
              <Pill tone="bull">
                <span className="pg-live-dot" aria-hidden />
                DELIBERATING
              </Pill>
            ) : (
              <Pill>COMPLETE</Pill>
            )}
          </Row>
          <span className="pg-body-sm">
            {running
              ? 'Seven nodes, one pass. Nothing executes without your approval.'
              : 'Deliberation complete.'}
          </span>
        </Stack>
        <Stack gap={4} style={{ alignItems: 'flex-end' }}>
          <Label>Elapsed</Label>
          <Numeral size={30}>{elapsed}</Numeral>
        </Stack>
      </Row>

      <div className="pg-grid">
        <Cell span={3}>
          <NodeCard row={byNode('router')} />
        </Cell>
        <Cell span={3}>
          <NodeCard row={byNode('technical')} />
        </Cell>
        <Cell span={3}>
          <NodeCard row={byNode('fundamental')} />
        </Cell>
        <Cell span={3}>
          <NodeCard row={byNode('macro')} />
        </Cell>

        <Cell span={4}>
          <NodeCard row={byNode('selector')} />
        </Cell>
        <Cell span={4}>
          <NodeCard row={byNode('drafter')} />
        </Cell>
        <Cell span={4}>
          <NodeCard row={byNode('risk_officer')} />
        </Cell>

        <Cell span={8}>
          {data?.result ? (
            <Verdict result={data.result} onOpenPick={(id) => go({ name: 'pick', id })} />
          ) : (
            <Card variant="hero" style={{ gap: 14, justifyContent: 'center' }}>
              <Label>Verdict</Label>
              <Skel h={34} w="45%" />
              <Skel h={14} w="70%" />
              <Skel h={14} w="55%" />
            </Card>
          )}
        </Cell>

        <Cell span={4}>
          <EventLog events={data?.events ?? []} running={running} />
        </Cell>
      </div>
    </>
  );
}

/* ── node card ───────────────────────────────────────────────────── */

function NodeCard({ row }: { row: NodeRow }) {
  const { node, state, summary } = row;
  const score = typeof summary?.score === 'number' ? summary.score : null;
  const confidence = typeof summary?.confidence === 'number' ? summary.confidence : null;
  const thesis = typeof summary?.thesis === 'string' ? summary.thesis : null;
  const isRisk = node === 'risk_officer';
  const approved = isRisk ? summary?.approved === true : null;

  const accent =
    isRisk && summary != null
      ? approved
        ? 'var(--pg-bull)'
        : 'var(--pg-bear)'
      : score != null
        ? scoreHex(score)
        : 'var(--pg-outline-variant)';

  return (
    <Card
      variant="dense"
      className={state === 'done' ? 'pg-fade-up' : undefined}
      style={{
        gap: 12,
        opacity: state === 'pending' ? 0.55 : state === 'skipped' ? 0.35 : 1,
        borderColor: state === 'done' ? accent : undefined,
        boxShadow:
          state === 'done' && (isRisk || (score ?? 0) >= 85) ? `0 0 24px ${accent}22` : undefined,
        transition: 'opacity 300ms ease, border-color 300ms ease',
      }}
    >
      <Row style={{ justifyContent: 'space-between' }}>
        <Label>{LABEL[node]}</Label>
        <StateChip state={state} />
      </Row>

      {state === 'running' ? (
        <Stack gap={10}>
          <Skel h={26} w="45%" />
          <Skel h={12} />
          <Skel h={12} w="70%" />
        </Stack>
      ) : state === 'done' ? (
        <Stack gap={10}>
          {isRisk ? (
            <Row gap={8}>
              <span aria-hidden style={{ color: accent, display: 'flex' }}>
                <IconShield size={18} />
              </span>
              <Numeral size={20} tone={approved ? 'bull' : 'bear'}>
                {approved ? 'CLEAR' : ruleLabel(String(summary?.vetoRule ?? 'VETO'))}
              </Numeral>
            </Row>
          ) : score != null ? (
            <Stack gap={8}>
              <Row style={{ justifyContent: 'space-between', alignItems: 'flex-end' }}>
                <Numeral size={30} style={{ color: accent }}>
                  {Math.round(score)}
                </Numeral>
                {confidence != null ? (
                  <span className="pg-caption pg-num">conf {Math.round(confidence * 100)}%</span>
                ) : null}
              </Row>
              <ScoreBar score={score} />
            </Stack>
          ) : (
            <Numeral size={18}>{secondaryValue(summary)}</Numeral>
          )}
          {thesis ? (
            <p
              className="pg-body-sm"
              style={{
                display: '-webkit-box',
                WebkitLineClamp: 4,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
              }}
              title={thesis}
            >
              {thesis}
            </p>
          ) : (
            <span className="pg-caption">{ROLE[node]}</span>
          )}
        </Stack>
      ) : (
        <Stack gap={8}>
          <Numeral size={26} style={{ color: 'var(--pg-outline-variant)' }}>
            —
          </Numeral>
          <span className="pg-caption">{ROLE[node]}</span>
        </Stack>
      )}
    </Card>
  );
}

function secondaryValue(summary: Record<string, unknown> | null): string {
  if (!summary) return '—';
  if (typeof summary.regime === 'string') return summary.regime.toUpperCase();
  if (typeof summary.strategy === 'string') return summary.strategy;
  if (typeof summary.action === 'string') return summary.action.toUpperCase();
  return 'DONE';
}

function StateChip({ state }: { state: NodeState }) {
  if (state === 'running') {
    return (
      <Pill tone="bull">
        <span className="pg-live-dot" aria-hidden />
        LIVE
      </Pill>
    );
  }
  if (state === 'done') return <Pill>DONE</Pill>;
  if (state === 'skipped') return <Pill>SKIPPED</Pill>;
  return <Pill>QUEUED</Pill>;
}

/* ── verdict ─────────────────────────────────────────────────────── */

function Verdict({
  result,
  onOpenPick,
}: {
  result: AgentRunResponse;
  onOpenPick: (id: string) => void;
}) {
  const p = result.proposal;
  const cleared = p != null;
  const wash = cleared ? 'var(--pg-bull-wash)' : 'var(--pg-bear-wash)';

  return (
    <Card
      variant="hero"
      style={{ gap: 16, backgroundImage: `linear-gradient(140deg, ${wash}, transparent 60%)` }}
    >
      <CardHead
        label="Verdict"
        right={
          <Row gap={8}>
            {result.regime ? <Pill>{result.regime.toUpperCase()}</Pill> : null}
            {result.llmMock ? (
              <Pill title="No ANTHROPIC_API_KEY — deterministic mock output">MOCK LLM</Pill>
            ) : null}
            <Pill tone={result.riskApproved ? 'bull' : 'bear'}>
              {result.riskApproved ? 'RISK CLEAR' : 'RISK VETO'}
            </Pill>
          </Row>
        }
      />

      {cleared ? (
        <>
          <Row gap={16} style={{ alignItems: 'baseline', flexWrap: 'wrap' }}>
            <span className="pg-h1 pg-num">
              {p.side} {p.qty} {p.symbol}
            </span>
            <Numeral size={22} tone="neutral">
              ≈ ${Math.round(p.estimatedNotional).toLocaleString('en-US')}
            </Numeral>
          </Row>
          <p className="pg-body-md">{p.rationale}</p>
          <Row>
            {/* Platinum/ink CTA — never green. */}
            <Button
              kind="primary"
              onClick={() => onOpenPick(p.id)}
              ariaLabel={`Review and approve the ${p.symbol} proposal`}
            >
              Review &amp; approve →
            </Button>
          </Row>
        </>
      ) : (
        <>
          <span className="pg-h2">
            {result.finalAction === 'VETOED' ? 'Vetoed by the risk engine' : 'Council holds'}
          </span>
          <p className="pg-body-md">{result.riskReason || 'No trade this run.'}</p>
          {result.riskVetoRule ? (
            <Row>
              <Pill tone="bear">{ruleLabel(result.riskVetoRule)}</Pill>
            </Row>
          ) : null}
        </>
      )}
    </Card>
  );
}

/* ── event log ───────────────────────────────────────────────────── */

function EventLog({ events, running }: { events: CouncilProgressEvent[]; running: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [events.length]);

  return (
    <Card style={{ gap: 12 }}>
      <CardHead
        label="Stream"
        right={
          running ? (
            <Pill tone="bull">
              <span className="pg-live-dot" aria-hidden />
              LIVE
            </Pill>
          ) : (
            <Pill>ENDED</Pill>
          )
        }
      />
      <div ref={ref} className="pg-scroll" style={{ maxHeight: 260, overflowY: 'auto' }} aria-live="polite">
        {events.length === 0 ? (
          <Stack gap={10}>
            <Skel h={12} />
            <Skel h={12} w="80%" />
            <Skel h={12} w="60%" />
          </Stack>
        ) : (
          <Stack gap={0}>
            {events.map((e) => (
              <Row
                key={e.seq}
                gap={10}
                style={{
                  padding: '7px 0',
                  borderTop:
                    e.seq === events[0].seq ? undefined : '1px solid var(--pg-card-border)',
                }}
              >
                <span className="pg-num pg-dim" style={{ fontSize: 11, flex: 'none' }}>
                  {clock(e.at)}
                </span>
                <span className="pg-num" style={{ fontSize: 12, flex: 1 }}>
                  {LABEL[e.node]}
                </span>
                <span className="label-caps" style={{ fontSize: 10 }}>
                  {e.status}
                </span>
              </Row>
            ))}
          </Stack>
        )}
      </div>
    </Card>
  );
}

/** mm:ss since mount, frozen once the run ends. */
function useElapsed(running: boolean): string {
  const startedAt = useRef(Date.now());
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!running) return undefined;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [running]);
  const secs = Math.max(0, Math.round((now - startedAt.current) / 1000));
  return `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`;
}
