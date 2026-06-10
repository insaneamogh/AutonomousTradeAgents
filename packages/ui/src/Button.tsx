/**
 * Button — the workhorse primitive.
 *
 * Variants:
 *   primary       Main CTA. One per screen. Uses `accent-primary`.
 *   secondary     Less critical. Outlined surface.
 *   tertiary      Inline / ghost.
 *   destructive   Sell-everything, disconnect broker, etc. Uses `danger`.
 *
 * Sizes: sm (32pt), md (44pt — default), lg (52pt — hero only).
 *
 * IMPORTANT: For the Approve-trade button on the ApprovalCard, use
 * variant="primary" (accent-primary). **Never** import this with a green
 * background — green is reserved for fills + P&L per DESIGN.md.
 */

import { ActivityIndicator, Pressable, Text } from 'react-native';

import { cn } from './utils';

type Variant = 'primary' | 'secondary' | 'tertiary' | 'destructive';
type Size = 'sm' | 'md' | 'lg';

interface ButtonProps {
  label: string;
  onPress: () => void;
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  disabled?: boolean;
  accessibilityLabel?: string;
  testID?: string;
  fullWidth?: boolean;
}

const VARIANT_CLASSES: Record<Variant, string> = {
  primary:
    'bg-accent-primary active:bg-accent-primary-hover dark:bg-accent-primary-dark dark:active:bg-accent-primary-hover-dark',
  secondary:
    'bg-bg-surface-elevated border border-border-strong dark:bg-bg-surface-elevated-dark dark:border-border-strong-dark',
  tertiary: 'bg-transparent',
  destructive:
    'bg-danger active:opacity-80 dark:bg-danger-dark',
};

const VARIANT_LABEL_CLASSES: Record<Variant, string> = {
  primary: 'text-white',
  secondary: 'text-text-primary dark:text-text-primary-dark',
  tertiary: 'text-accent-primary dark:text-accent-primary-dark',
  destructive: 'text-white',
};

const SIZE_CLASSES: Record<Size, string> = {
  sm: 'h-8 px-3',
  md: 'h-11 px-4',
  lg: 'h-[52px] px-6',
};

const SIZE_LABEL_CLASSES: Record<Size, string> = {
  sm: 'text-[13px] font-medium',
  md: 'text-[15px] font-semibold',
  lg: 'text-[17px] font-semibold',
};

export function Button({
  label,
  onPress,
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled = false,
  accessibilityLabel,
  testID,
  fullWidth = false,
}: ButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <Pressable
      onPress={onPress}
      disabled={isDisabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ disabled: isDisabled, busy: loading }}
      testID={testID}
      className={cn(
        'items-center justify-center rounded-md',
        SIZE_CLASSES[size],
        VARIANT_CLASSES[variant],
        fullWidth && 'self-stretch',
        isDisabled && 'opacity-50',
      )}
    >
      {loading ? (
        <ActivityIndicator
          color={variant === 'primary' || variant === 'destructive' ? '#fff' : undefined}
          size="small"
        />
      ) : (
        <Text className={cn(SIZE_LABEL_CLASSES[size], VARIANT_LABEL_CLASSES[variant])}>
          {label}
        </Text>
      )}
    </Pressable>
  );
}
