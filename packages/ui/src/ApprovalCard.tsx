/**
 * ApprovalCard — the marquee component.
 *
 * Per DESIGN.md §5: this is the single most important UI surface. Users
 * spend more time on this card than anywhere else; it must convey trust,
 * risk, and reasoning without overwhelming.
 *
 * Hard rules:
 *   - Approve button uses `variant="primary"` (accent-primary). NEVER green.
 *   - Countdown is visible at all times when a trade has an auto-decline.
 *   - Bull and bear case both shown, expandable.
 *   - Decline first (left), Approve second (right). Right-handed users
 *     reach Approve naturally; lefties tap Decline.
 */

import { useEffect, useMemo, useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import { Button } from './Button';
import { Card } from './Card';
import { PriceDisplay } from './PriceDisplay';
import { cn, formatMmSs, formatRelative } from './utils';

export type Side = 'BUY' | 'SELL';

export interface ApprovalProposal {
  id: string;
  symbol: string;
  side: Side;
  qty: number;
  orderType: 'MARKET' | 'LIMIT';
  limitPrice?: number;
  estimatedNotional: number;
  rationale: string;
  bullCase: string;
  bearCase: string;
  /** Integer 1–5; the dot meter renders 5 dots, this many filled. */
  riskLevel: 1 | 2 | 3 | 4 | 5;
  /** Integer 1–5; same as above. */
  convictionLevel: 1 | 2 | 3 | 4 | 5;
  proposedAt: Date;
  /** Seconds until auto-decline. If absent or 0, no countdown shown. */
  expiresInSeconds?: number;
  /** Non-blocking warnings from the risk engine. Known: wash_sale_warning. */
  informationalFlags?: string[];
}

interface ApprovalCardProps {
  proposal: ApprovalProposal;
  onApprove: (proposal: ApprovalProposal) => void;
  onDecline: (proposal: ApprovalProposal) => void;
  onExpire?: (proposal: ApprovalProposal) => void;
  busy?: boolean;
}

export function ApprovalCard({
  proposal,
  onApprove,
  onDecline,
  onExpire,
  busy = false,
}: ApprovalCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState(proposal.expiresInSeconds ?? 0);

  useEffect(() => {
    if (!proposal.expiresInSeconds) return;
    setSecondsLeft(proposal.expiresInSeconds);
    const interval = setInterval(() => {
      setSecondsLeft((s) => {
        if (s <= 1) {
          clearInterval(interval);
          onExpire?.(proposal);
          return 0;
        }
        return s - 1;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, [proposal, onExpire]);

  const relativeTimestamp = useMemo(() => formatRelative(proposal.proposedAt), [proposal.proposedAt]);
  const sideClasses = proposal.side === 'BUY'
    ? 'text-gain dark:text-gain-dark'
    : 'text-loss dark:text-loss-dark';

  return (
    <Card variant="default" className="gap-3">
      {/* Header */}
      <View className="flex-row items-center justify-between">
        <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
          Agent proposal
        </Text>
        <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
          {relativeTimestamp}
        </Text>
      </View>

      {/* Informational flags from the risk engine (non-blocking warnings) */}
      <FlagChips flags={proposal.informationalFlags} />

      {/* Side + symbol + qty */}
      <View className="flex-row items-baseline gap-3">
        <Text
          className={cn('text-[20px] font-bold tracking-wide', sideClasses)}
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {proposal.side}
        </Text>
        <Text
          className="text-[20px] font-bold text-text-primary dark:text-text-primary-dark"
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {proposal.symbol}
        </Text>
        <Text
          className="text-[15px] text-text-secondary dark:text-text-secondary-dark"
          style={{ fontVariant: ['tabular-nums'] }}
        >
          {proposal.qty} shares
        </Text>
      </View>

      {/* Price line */}
      <View className="flex-row items-center gap-2">
        <Text className="text-[13px] text-text-secondary dark:text-text-secondary-dark">
          {proposal.orderType === 'LIMIT' && proposal.limitPrice != null
            ? 'Limit @ '
            : 'Market · est. '}
        </Text>
        <PriceDisplay
          value={proposal.orderType === 'LIMIT' ? proposal.limitPrice ?? 0 : proposal.estimatedNotional / proposal.qty}
          tone="primary"
          size="md"
        />
        <Text className="text-[13px] text-text-tertiary dark:text-text-tertiary-dark">
          · est. ${proposal.estimatedNotional.toLocaleString('en-US', { maximumFractionDigits: 2 })}
        </Text>
      </View>

      {/* Risk + Conviction dots */}
      <View className="flex-row gap-6">
        <DotRow label="Risk" level={proposal.riskLevel} tone="warning" />
        <DotRow label="Conviction" level={proposal.convictionLevel} tone="info" />
      </View>

      {/* Rationale (always visible, short) */}
      <Text className="text-[13px] leading-[19px] text-text-primary dark:text-text-primary-dark">
        {proposal.rationale}
      </Text>

      {/* Expandable bull / bear */}
      <Pressable
        onPress={() => setExpanded((v) => !v)}
        accessibilityRole="button"
        accessibilityLabel={expanded ? 'Hide bull and bear case' : 'Show bull and bear case'}
      >
        <Text className="text-[13px] font-medium text-accent-primary dark:text-accent-primary-dark">
          {expanded ? '▾ Hide reasoning' : '▸ Why this trade'}
        </Text>
      </Pressable>

      {expanded && (
        <View className="gap-3">
          <CaseBlock label="Bull case" body={proposal.bullCase} />
          <CaseBlock label="Bear case" body={proposal.bearCase} />
        </View>
      )}

      {/* CTAs */}
      <View className="mt-2 flex-row gap-2">
        <View className="flex-1">
          <Button
            label="Decline"
            variant="secondary"
            size="md"
            onPress={() => onDecline(proposal)}
            disabled={busy}
            fullWidth
            accessibilityLabel={`Decline ${proposal.side} ${proposal.symbol}`}
          />
        </View>
        <View className="flex-1">
          <Button
            label="Approve"
            variant="primary"
            size="md"
            onPress={() => onApprove(proposal)}
            loading={busy}
            fullWidth
            accessibilityLabel={`Approve ${proposal.side} ${proposal.qty} ${proposal.symbol}`}
          />
        </View>
      </View>

      {/* Countdown */}
      {proposal.expiresInSeconds && secondsLeft > 0 ? (
        <Text className="text-center text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
          Auto-decline in {formatMmSs(secondsLeft)}
        </Text>
      ) : null}
    </Card>
  );
}

interface DotRowProps {
  label: string;
  level: 1 | 2 | 3 | 4 | 5;
  tone: 'warning' | 'info';
}

function DotRow({ label, level, tone }: DotRowProps) {
  const fillClass =
    tone === 'warning'
      ? 'bg-warning dark:bg-warning-dark'
      : 'bg-accent-primary dark:bg-accent-primary-dark';
  return (
    <View className="flex-row items-center gap-2">
      <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
        {label}
      </Text>
      <View className="flex-row gap-1">
        {[1, 2, 3, 4, 5].map((i) => (
          <View
            key={i}
            className={cn(
              'h-2 w-2 rounded-full',
              i <= level ? fillClass : 'bg-bg-surface-muted dark:bg-bg-surface-muted-dark',
            )}
          />
        ))}
      </View>
    </View>
  );
}

interface CaseBlockProps {
  label: string;
  body: string;
}

function CaseBlock({ label, body }: CaseBlockProps) {
  return (
    <View className="rounded-md border border-border-subtle bg-bg-surface-muted p-3 dark:border-border-subtle-dark dark:bg-bg-surface-muted-dark">
      <Text className="mb-1 text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
        {label}
      </Text>
      <Text className="text-[13px] leading-[19px] text-text-primary dark:text-text-primary-dark">
        {body}
      </Text>
    </View>
  );
}

/**
 * Non-blocking warnings surfaced from the risk engine. Uses the `warning`
 * design token (yellow), NOT `danger` — these are advisories, not emergencies.
 * The wash-sale flag is the canonical example: IRS won't stop the trade,
 * but you should know you're about to defer a tax loss.
 */
function FlagChips({ flags }: { flags?: string[] }) {
  if (!flags || flags.length === 0) return null;
  return (
    <View className="flex-row flex-wrap gap-1.5">
      {flags.map((flag) => (
        <FlagChip key={flag} flag={flag} />
      ))}
    </View>
  );
}

const FLAG_COPY: Record<string, { label: string; tone: 'warning' | 'info' }> = {
  wash_sale_warning: { label: '⚠ Wash-sale risk', tone: 'warning' },
  sector_unknown: { label: '· Sector unknown', tone: 'info' },
};

function FlagChip({ flag }: { flag: string }) {
  const copy = FLAG_COPY[flag];
  if (!copy) return null;
  const classes =
    copy.tone === 'warning'
      ? 'bg-warning-subtle text-warning dark:bg-warning-subtle-dark dark:text-warning-dark'
      : 'bg-bg-surface-muted text-text-secondary dark:bg-bg-surface-muted-dark dark:text-text-secondary-dark';
  return (
    <Text
      className={cn(
        'self-start rounded-sm px-2 py-0.5 text-[11px] font-semibold',
        classes,
      )}
    >
      {copy.label}
    </Text>
  );
}
