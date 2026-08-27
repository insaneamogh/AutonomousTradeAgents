/**
 * Trade biography — the timeline for one decision, shared by Positions
 * (a filled/pending position) and Decisions (any decision, including a
 * HOLD that never became a position).
 *
 * The "proposed" event's `data.analysts` carries each analyst that ran —
 * role, score, confidence, thesis — the same per-agent output the theater
 * shows live, kept around so it's still inspectable after the run ends.
 * Before this it was fetched but never rendered: the API had it, the UI
 * dropped it on the floor.
 */

import { useDecisionTimeline } from '@/hooks/useDecisionTimeline';

import { ago } from './format';
import { Button, Card, CardHead, Pill, Row, SkelRows, Stack } from './primitives';

interface AnalystSummary {
  role: string;
  score: number | null;
  confidence: number | null;
  thesis: string;
}

const ROLE_LABEL: Record<string, string> = {
  technical: 'Technical',
  fundamental: 'Fundamental',
  macro: 'Macro',
};

function AnalystRow({ a }: { a: AnalystSummary }) {
  return (
    <Stack gap={4} style={{ padding: '8px 0' }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <span style={{ fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          {ROLE_LABEL[a.role] ?? a.role}
        </span>
        <Row gap={8}>
          {a.score != null ? <span className="pg-caption pg-num">score {Math.round(a.score)}</span> : null}
          {a.confidence != null ? (
            <span className="pg-caption pg-num">conf {Math.round(a.confidence * 100)}%</span>
          ) : null}
        </Row>
      </Row>
      {a.thesis ? <span className="pg-caption">{a.thesis}</span> : null}
    </Stack>
  );
}

export function TimelineCard({ decisionId, onClose }: { decisionId: string; onClose: () => void }) {
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
          {timeline.data.events.map((e, i) => {
            const analysts = (e.data?.analysts as AnalystSummary[] | undefined) ?? [];
            const selectedStrategy = e.data?.selectedStrategy as string | null | undefined;
            const selectorRationale = e.data?.selectorRationale as string | null | undefined;
            return (
              <Stack
                key={`${e.kind}-${i}`}
                gap={0}
                style={{ padding: '10px 0', borderTop: i === 0 ? undefined : '1px solid var(--pg-card-border)' }}
              >
                <Row gap={10} style={{ alignItems: 'flex-start' }}>
                  <span
                    aria-hidden
                    style={{ width: 6, height: 6, borderRadius: 9999, marginTop: 7, flex: 'none', background: 'var(--pg-outline)' }}
                  />
                  <Stack gap={2} style={{ flex: 1 }}>
                    <span style={{ fontSize: 13, fontWeight: 500 }}>{e.title}</span>
                    <span className="pg-caption">{e.detail}</span>
                  </Stack>
                  <span className="pg-caption pg-num" style={{ flex: 'none' }}>
                    {ago(e.at)}
                  </span>
                </Row>
                {selectedStrategy ? (
                  <div className="pg-inset" style={{ marginTop: 8, marginLeft: 16 }}>
                    <Row style={{ justifyContent: 'space-between' }}>
                      <span className="pg-caption">strategy fit</span>
                      <Pill tone="neutral">{selectedStrategy}</Pill>
                    </Row>
                    {selectorRationale ? (
                      <span className="pg-caption" style={{ display: 'block', marginTop: 4 }}>
                        {selectorRationale}
                      </span>
                    ) : null}
                  </div>
                ) : null}
                {analysts.length > 0 ? (
                  <div className="pg-inset" style={{ marginTop: 8, marginLeft: 16 }}>
                    {analysts.map((a) => (
                      <AnalystRow key={a.role} a={a} />
                    ))}
                  </div>
                ) : null}
              </Stack>
            );
          })}
        </Stack>
      )}
    </Card>
  );
}
