// Pick detail — Design D bento.
//
// Bull/bear tinted tiles · deterministic risk-check tile · Approve/Pass.
// Approve opens a confirm sheet (Modal) with the order economics; the
// actual mutation is the same useDecideApproval the feed uses, so the
// optimistic cache update + invalidations stay consistent.
//
// Reads the proposal from the pending-approvals query cache — no extra
// endpoint. If the proposal is gone (decided elsewhere / expired) we
// show a quiet fallback and a way back.

import { useState } from 'react';
import { Modal, Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';

import { EmptyState } from '@app/ui';

import {
  BentoCTA,
  BentoQuiet,
  DirectionPill,
  HeroHeadline,
  HeroSub,
  Tile,
  TileLabel,
  levelLabel,
} from '@/components/bento';
import { useDecideApproval, usePendingApprovals } from '@/hooks/useApprovals';

const FLAG_COPY: Record<string, string> = {
  wash_sale_warning: 'IRS wash-sale risk on this name',
  sector_unknown: 'Sector classification missing',
};

export default function PickDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const { data: pending } = usePendingApprovals();
  const decide = useDecideApproval();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const p = (pending ?? []).find((x) => x.id === id);

  if (!p) {
    return (
      <SafeAreaView className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
        <View className="flex-1 items-center justify-center px-4">
          <EmptyState
            title="Pick not available"
            description="It may have been decided already or expired."
          />
          <View className="mt-4 w-full">
            <BentoQuiet label="Back to picks" onPress={() => router.back()} />
          </View>
        </View>
      </SafeAreaView>
    );
  }

  const isBuy = p.side === 'BUY';
  const decideAndClose = (outcome: 'approved' | 'declined') => {
    decide.mutate({ proposalId: p.id, outcome });
    setConfirmOpen(false);
    router.back();
  };

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-canvas dark:bg-bg-canvas-dark">
      <ScrollView contentContainerClassName="px-4 pb-16 pt-4 gap-3">
        <Pressable
          onPress={() => router.back()}
          accessibilityRole="button"
          accessibilityLabel="Back to picks"
          className="min-h-[44px] justify-center"
        >
          <Text className="text-[13px] text-text-secondary dark:text-text-secondary-dark">
            ← Picks
          </Text>
        </Pressable>

        <View className="flex-row items-end justify-between">
          <View>
            <HeroHeadline>{p.symbol}</HeroHeadline>
            <HeroSub>
              {isBuy ? 'Long' : 'Sell'} · {p.qty} sh ·{' '}
              {p.orderType === 'LIMIT' && p.limitPrice
                ? `limit $${p.limitPrice.toFixed(2)}`
                : 'market'}
            </HeroSub>
          </View>
          <DirectionPill
            label={`${levelLabel(p.convictionLevel)} CONVICTION`}
            tone={isBuy ? 'mint' : 'rose'}
          />
        </View>

        <View className="flex-row gap-3">
          <Tile className="flex-1 gap-1.5 bg-mint-subtle dark:bg-mint-subtle-dark">
            <Text className="text-[10px] font-semibold uppercase tracking-[1.1px] text-mint dark:text-mint-dark">
              Bull case
            </Text>
            <Text className="text-[12px] leading-[18px] text-text-primary dark:text-text-primary-dark">
              {p.bullCase}
            </Text>
          </Tile>
          <Tile className="flex-1 gap-1.5 bg-rose-subtle dark:bg-rose-subtle-dark">
            <Text className="text-[10px] font-semibold uppercase tracking-[1.1px] text-rose dark:text-rose-dark">
              Bear case
            </Text>
            <Text className="text-[12px] leading-[18px] text-text-primary dark:text-text-primary-dark">
              {p.bearCase}
            </Text>
          </Tile>
        </View>

        <Tile className="gap-1.5">
          <TileLabel>Why now</TileLabel>
          <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
            {p.rationale}
          </Text>
        </Tile>

        <Tile className="gap-2">
          <TileLabel>Risk check</TileLabel>
          <Row k="Risk level" v={levelLabel(p.riskLevel)} />
          <Row
            k="Notional"
            v={`$${Math.round(p.estimatedNotional).toLocaleString('en-US')}`}
          />
          {p.stopLoss != null && <Row k="Stop loss" v={`$${p.stopLoss.toFixed(2)}`} />}
          {p.targetPrice != null && <Row k="Target" v={`$${p.targetPrice.toFixed(2)}`} />}
          {(p.informationalFlags ?? []).map((f) => (
            <Text
              key={f}
              className="text-[11px] text-warning dark:text-warning-dark"
            >
              ⚠ {FLAG_COPY[f] ?? f}
            </Text>
          ))}
          {(p.informationalFlags ?? []).length === 0 && (
            <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
              All blocking rules clear — flags would show here.
            </Text>
          )}
        </Tile>

        <View className="flex-row gap-3">
          <View className="flex-1">
            <BentoCTA
              label="Approve"
              onPress={() => setConfirmOpen(true)}
              disabled={decide.isPending}
              accessibilityLabel={`Approve ${p.symbol} ${isBuy ? 'buy' : 'sell'}`}
            />
          </View>
          <View className="flex-1">
            <BentoQuiet
              label="Pass"
              onPress={() => decideAndClose('declined')}
              disabled={decide.isPending}
              accessibilityLabel={`Decline ${p.symbol} pick`}
            />
          </View>
        </View>
      </ScrollView>

      <Modal
        visible={confirmOpen}
        transparent
        animationType="slide"
        onRequestClose={() => setConfirmOpen(false)}
      >
        <View className="flex-1 justify-end bg-black/50">
          <View className="rounded-t-xl bg-bg-tile px-4 pb-10 pt-4 dark:bg-bg-tile-dark">
            <Text className="text-[16px] font-medium text-text-primary dark:text-text-primary-dark">
              Confirm order
            </Text>
            <View className="mt-3 gap-2">
              <Row
                k={isBuy ? 'Buy' : 'Sell'}
                v={`${p.qty} sh · ~$${Math.round(p.estimatedNotional).toLocaleString('en-US')}`}
              />
              {p.stopLoss != null && <Row k="Stop loss" v={`$${p.stopLoss.toFixed(2)}`} />}
              <Row k="Broker" v="Alpaca paper" />
            </View>
            <View className="mt-4 gap-2">
              <BentoCTA
                label={decide.isPending ? 'Submitting…' : 'Confirm approve'}
                onPress={() => decideAndClose('approved')}
                disabled={decide.isPending}
                accessibilityLabel={`Confirm approval of ${p.symbol} order`}
              />
              <BentoQuiet label="Cancel" onPress={() => setConfirmOpen(false)} />
            </View>
            <Text className="mt-3 text-center text-[10px] text-text-tertiary dark:text-text-tertiary-dark">
              Decision is audit-logged with your user id and timestamp.
            </Text>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <View className="flex-row items-center justify-between">
      <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">{k}</Text>
      <Text
        className="text-[12px] font-medium text-text-primary dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'] }}
      >
        {v}
      </Text>
    </View>
  );
}
