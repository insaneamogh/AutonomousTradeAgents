// Approval inbox — wired to the API.
//
// usePendingApprovals = useQuery
// useDecideApproval   = useMutation (optimistic; invalidates account + activity on settle)
// useRunAgent         = useMutation (triggers a server-side council run; invalidates pending + activity)

import { useState } from 'react';
import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import type { ApprovalProposalDto } from '@app/shared-types';
import {
  ApprovalCard,
  Button,
  Card,
  EmptyState,
  ErrorState,
  SkeletonCardStack,
  cn,
  secondsUntil,
} from '@app/ui';
import type { ApprovalProposal } from '@app/ui';

import { useDecideApproval, usePendingApprovals } from '@/hooks/useApprovals';
import { useRunAgent } from '@/hooks/useRunAgent';

// Rotated through on each "Run council" tap — gives variety in mock mode.
const TICKERS = ['NVDA', 'AAPL', 'MSFT', 'TSLA', 'AMD', 'AMZN', 'GOOGL'] as const;

interface RunResultBanner {
  symbol: string;
  finalAction: string;
  riskReason: string;
  vetoRule?: string | null;
  llmMock: boolean;
  ok: boolean;
}

export default function ApprovalsScreen() {
  const { data, isLoading, isError, refetch } = usePendingApprovals();
  const decide = useDecideApproval();
  const runAgent = useRunAgent();

  const [tickerIndex, setTickerIndex] = useState(0);
  const [lastRun, setLastRun] = useState<RunResultBanner | null>(null);

  const pending = data ?? [];

  const handleDecide = (proposalId: string, outcome: 'approved' | 'declined') => {
    decide.mutate({ proposalId, outcome });
  };

  const handleRunCouncil = () => {
    const symbol = TICKERS[tickerIndex % TICKERS.length];
    setTickerIndex((i) => i + 1);
    setLastRun(null);
    runAgent.mutate(
      { symbol, horizon: 'short' },
      {
        onSuccess: (res) =>
          setLastRun({
            symbol,
            finalAction: res.finalAction,
            riskReason: res.riskReason,
            vetoRule: res.riskVetoRule,
            llmMock: res.llmMock,
            ok: res.riskApproved,
          }),
        onError: () =>
          setLastRun({
            symbol,
            finalAction: 'ERROR',
            riskReason: "Couldn't reach the agent server.",
            llmMock: false,
            ok: false,
          }),
      },
    );
  };

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-4">
        <Header />

        <CouncilTrigger
          symbol={TICKERS[tickerIndex % TICKERS.length]}
          pending={runAgent.isPending}
          onPress={handleRunCouncil}
          lastRun={lastRun}
        />

        {isLoading ? (
          <Card variant="default">
            <SkeletonCardStack rows={4} />
          </Card>
        ) : isError ? (
          <Card variant="default">
            <ErrorState
              title="Couldn't load approvals"
              description="The agent server isn't reachable. Try again in a moment."
              onRetry={() => refetch()}
            />
          </Card>
        ) : pending.length === 0 ? (
          <View className="mt-4">
            <EmptyState
              title="No pending approvals"
              description="Tap 'Run council' above to have the agent generate a proposal."
            />
          </View>
        ) : (
          pending.map((p) => (
            <ApprovalCard
              key={p.id}
              proposal={toUiProposal(p)}
              onApprove={(x) => handleDecide(x.id, 'approved')}
              onDecline={(x) => handleDecide(x.id, 'declined')}
              onExpire={() => refetch()}
              busy={decide.isPending}
            />
          ))
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function Header() {
  return (
    <View>
      <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
        Pending
      </Text>
      <Text className="mt-1 text-[24px] font-semibold leading-[30px] text-text-primary dark:text-text-primary-dark">
        Trade approvals
      </Text>
      <Text className="mt-1 text-[13px] text-text-secondary dark:text-text-secondary-dark">
        Every proposal shows the bull case, the bear case, and the risk rule that
        fired. Approve consciously — paper trading is free, your time is not.
      </Text>
    </View>
  );
}

interface CouncilTriggerProps {
  symbol: string;
  pending: boolean;
  onPress: () => void;
  lastRun: RunResultBanner | null;
}

function CouncilTrigger({ symbol, pending, onPress, lastRun }: CouncilTriggerProps) {
  return (
    <Card variant="inset" className="gap-3">
      <View className="flex-row items-center justify-between">
        <View className="flex-1">
          <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
            Run council
          </Text>
          <Text className="mt-1 text-[15px] text-text-primary dark:text-text-primary-dark">
            Next ticker:{' '}
            <Text
              className="font-semibold"
              style={{ fontVariant: ['tabular-nums'] }}
            >
              {symbol}
            </Text>
          </Text>
        </View>
        <Button
          label={pending ? 'Running…' : 'Run'}
          variant="primary"
          size="md"
          loading={pending}
          onPress={onPress}
          accessibilityLabel={`Run the agent council on ${symbol}`}
        />
      </View>
      {lastRun && <CouncilResultLine result={lastRun} />}
    </Card>
  );
}

function CouncilResultLine({ result }: { result: RunResultBanner }) {
  const tone = result.ok
    ? 'text-accent-primary dark:text-accent-primary-dark'
    : result.finalAction === 'ERROR'
      ? 'text-loss dark:text-loss-dark'
      : 'text-warning dark:text-warning-dark';
  return (
    <View className="gap-1 border-t border-border-subtle pt-2 dark:border-border-subtle-dark">
      <View className="flex-row items-center gap-2">
        <Text
          className={cn('text-[11px] font-bold uppercase tracking-[1.2px]', tone)}
        >
          {result.finalAction}
        </Text>
        <Text
          className="text-[13px] font-semibold text-text-primary dark:text-text-primary-dark"
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {result.symbol}
        </Text>
        {result.llmMock && (
          <Text className="text-[11px] font-medium text-text-tertiary dark:text-text-tertiary-dark">
            · mock LLM
          </Text>
        )}
      </View>
      <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">
        {result.riskReason}
        {result.vetoRule ? ` (${result.vetoRule})` : ''}
      </Text>
    </View>
  );
}

/**
 * Convert the wire DTO (ISO date strings) into the ApprovalCard's view-model
 * (Date object + seconds-until). Cheap function — happens on each render
 * but the math is trivial.
 */
function toUiProposal(p: ApprovalProposalDto): ApprovalProposal {
  return {
    id: p.id,
    symbol: p.symbol,
    side: p.side,
    qty: p.qty,
    orderType: p.orderType,
    limitPrice: p.limitPrice,
    estimatedNotional: p.estimatedNotional,
    rationale: p.rationale,
    bullCase: p.bullCase,
    bearCase: p.bearCase,
    riskLevel: p.riskLevel,
    convictionLevel: p.convictionLevel,
    proposedAt: new Date(p.proposedAt),
    expiresInSeconds: secondsUntil(p.expiresAt) || undefined,
    informationalFlags: p.informationalFlags,
  };
}
