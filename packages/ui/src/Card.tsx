/**
 * Card — the default container primitive.
 *
 * Surface color flips with theme. Light mode: white surface, 1px border, no
 * shadow. Dark mode: elevated surface, subtle shadow.
 *
 * Don't stack shadows on shadows — one shadow level per surface.
 */

import { View } from 'react-native';
import type { ViewProps } from 'react-native';

import { cn } from './utils';

interface CardProps extends ViewProps {
  /** `elevated` lifts cards used as modal/sheet content. Default is `default`. */
  variant?: 'default' | 'elevated' | 'inset';
}

const VARIANT_CLASSES: Record<NonNullable<CardProps['variant']>, string> = {
  default:
    'bg-bg-surface dark:bg-bg-surface-dark border border-border-subtle dark:border-border-subtle-dark',
  elevated:
    'bg-bg-surface-elevated dark:bg-bg-surface-elevated-dark border border-border-subtle dark:border-border-subtle-dark shadow-sm',
  inset:
    'bg-bg-surface-muted dark:bg-bg-surface-muted-dark border border-border-subtle dark:border-border-subtle-dark',
};

export function Card({ variant = 'default', className, children, ...rest }: CardProps) {
  return (
    <View className={cn('rounded-lg p-4', VARIANT_CLASSES[variant], className)} {...rest}>
      {children}
    </View>
  );
}
