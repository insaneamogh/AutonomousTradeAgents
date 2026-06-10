/**
 * ErrorState — what to show when a fetch fails or a screen can't render.
 *
 * Per DESIGN.md §11:
 *   - Clear cause
 *   - Suggested action
 *   - Retry button
 *
 * Distinct from `EmptyState` — empty is "nothing here yet", error is
 * "something broke." Don't conflate them. If a fetch returns an empty
 * array, that's `EmptyState`. If a fetch throws, that's this.
 */

import { Text, View } from 'react-native';

import { Button } from './Button';

interface ErrorStateProps {
  title?: string;
  description?: string;
  onRetry?: () => void;
  retryLabel?: string;
}

export function ErrorState({
  title = 'Something went wrong',
  description = "We couldn't load this right now. Check your connection or try again.",
  onRetry,
  retryLabel = 'Try again',
}: ErrorStateProps) {
  return (
    <View className="items-center justify-center px-6 py-16">
      <View className="mb-3 h-8 w-8 items-center justify-center rounded-full bg-loss-subtle dark:bg-loss-subtle-dark">
        <Text className="text-[17px] font-bold text-loss dark:text-loss-dark">!</Text>
      </View>
      <Text className="text-center text-[17px] font-semibold text-text-primary dark:text-text-primary-dark">
        {title}
      </Text>
      <Text className="mt-2 max-w-[280px] text-center text-[13px] text-text-secondary dark:text-text-secondary-dark">
        {description}
      </Text>
      {onRetry ? (
        <View className="mt-6">
          <Button label={retryLabel} onPress={onRetry} variant="secondary" size="md" />
        </View>
      ) : null}
    </View>
  );
}
