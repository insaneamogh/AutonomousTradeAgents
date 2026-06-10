/**
 * EmptyState — every list / screen needs one per DESIGN.md §11.
 *
 * Three parts: title (what's missing), description (why / what fills it),
 * optional CTA. No clip-art illustrations in v0 — keeps the bundle small.
 */

import { Text, View } from 'react-native';

import { Button } from './Button';

interface EmptyStateProps {
  title: string;
  description?: string;
  ctaLabel?: string;
  onCta?: () => void;
}

export function EmptyState({ title, description, ctaLabel, onCta }: EmptyStateProps) {
  return (
    <View className="items-center justify-center px-6 py-16">
      <Text className="text-center text-[17px] font-semibold text-text-primary dark:text-text-primary-dark">
        {title}
      </Text>
      {description ? (
        <Text className="mt-2 max-w-[280px] text-center text-[13px] text-text-secondary dark:text-text-secondary-dark">
          {description}
        </Text>
      ) : null}
      {ctaLabel && onCta ? (
        <View className="mt-6">
          <Button label={ctaLabel} onPress={onCta} variant="primary" size="md" />
        </View>
      ) : null}
    </View>
  );
}
