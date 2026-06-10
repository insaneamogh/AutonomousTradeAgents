/**
 * SkeletonLoader — shimmer placeholder for loading states.
 *
 * DESIGN.md §11: skeletons not spinners. Spinners feel slower because they
 * tell the user "I'm working" but not "this is what will appear." A
 * skeleton previews the shape so the eventual content swap is calming.
 *
 * Animation: a low-opacity pulse via Reanimated (`useSharedValue` +
 * `withRepeat`). Respects `useReducedMotion()` automatically — Reanimated
 * disables animations when accessibility motion is reduced.
 */

import { useEffect } from 'react';
import { View } from 'react-native';
import Animated, {
  Easing,
  useAnimatedStyle,
  useSharedValue,
  withRepeat,
  withTiming,
} from 'react-native-reanimated';

import { cn } from './utils';

interface SkeletonProps {
  /** Tailwind className for size + shape, e.g. `h-6 w-32 rounded-md`. */
  className?: string;
}

export function Skeleton({ className }: SkeletonProps) {
  const opacity = useSharedValue(0.5);

  useEffect(() => {
    opacity.value = withRepeat(
      withTiming(1, { duration: 900, easing: Easing.inOut(Easing.ease) }),
      -1,
      true,
    );
  }, [opacity]);

  const style = useAnimatedStyle(() => ({ opacity: opacity.value }));

  return (
    <Animated.View
      style={style}
      className={cn(
        'bg-bg-surface-muted dark:bg-bg-surface-muted-dark rounded-md',
        className,
      )}
    />
  );
}

/**
 * Pre-canned skeleton stack — mimics the shape of a typical Card content
 * block. Use this when you don't want to hand-author skeleton shapes.
 */
export function SkeletonCardStack({ rows = 3 }: { rows?: number }) {
  return (
    <View className="gap-3">
      <Skeleton className="h-4 w-24" />
      <Skeleton className="h-8 w-40" />
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={cn('h-4', i === rows - 1 ? 'w-2/3' : 'w-full')} />
      ))}
    </View>
  );
}
