/**
 * Toggle — iOS-style switch primitive.
 *
 * We don't use React Native's stock <Switch> because it can't be
 * themed to our design tokens on Android (the platform default
 * teal/blue clashes with our accent-primary). Hand-rolled is small
 * and matches both palettes exactly.
 *
 * Sizes: md (default) and sm. Use sm only inside dense list rows.
 */

import { useEffect, useRef } from 'react';
import { Animated, Pressable, View } from 'react-native';

import { cn } from './utils';

interface ToggleProps {
  value: boolean;
  onValueChange: (next: boolean) => void;
  size?: 'sm' | 'md';
  disabled?: boolean;
  accessibilityLabel: string;
  testID?: string;
}

const SIZES = {
  md: { track: 'h-7 w-12', thumb: 22, padding: 2 },
  sm: { track: 'h-5 w-9', thumb: 16, padding: 2 },
};

export function Toggle({
  value,
  onValueChange,
  size = 'md',
  disabled = false,
  accessibilityLabel,
  testID,
}: ToggleProps) {
  const { track, thumb, padding } = SIZES[size];
  const anim = useRef(new Animated.Value(value ? 1 : 0)).current;

  useEffect(() => {
    Animated.timing(anim, {
      toValue: value ? 1 : 0,
      duration: 160,
      useNativeDriver: false,
    }).start();
  }, [value, anim]);

  const trackWidth = size === 'md' ? 48 : 36;
  const travel = trackWidth - thumb - padding * 2;
  const thumbX = anim.interpolate({ inputRange: [0, 1], outputRange: [0, travel] });

  return (
    <Pressable
      onPress={() => !disabled && onValueChange(!value)}
      disabled={disabled}
      accessibilityRole="switch"
      accessibilityState={{ checked: value, disabled }}
      accessibilityLabel={accessibilityLabel}
      testID={testID}
      hitSlop={8}
      className={cn(
        'rounded-full',
        track,
        value
          ? 'bg-accent-primary dark:bg-accent-primary-dark'
          : 'bg-bg-surface-muted dark:bg-bg-surface-muted-dark',
        disabled && 'opacity-50',
      )}
    >
      <View className="flex-1 justify-center" style={{ padding }}>
        <Animated.View
          style={{
            width: thumb,
            height: thumb,
            borderRadius: thumb / 2,
            backgroundColor: '#ffffff',
            transform: [{ translateX: thumbX }],
            shadowColor: '#000',
            shadowOffset: { width: 0, height: 1 },
            shadowOpacity: 0.18,
            shadowRadius: 1.5,
            elevation: 2,
          }}
        />
      </View>
    </Pressable>
  );
}
