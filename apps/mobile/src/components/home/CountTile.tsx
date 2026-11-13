// One count, one very large numeral.

import { Text } from 'react-native';

import { HapticPressable } from '@app/ui';

import { Tile, TileLabel } from '@/components/bento';

export function CountTile({
  label,
  value,
  hint,
  onPress,
  testID,
}: {
  label: string;
  value: number | null | undefined;
  hint?: string;
  onPress?: () => void;
  testID?: string;
}) {
  const body = (
    <Tile className="flex-1 gap-1">
      <TileLabel>{label}</TileLabel>
      <Text
        className="text-[44px] font-medium leading-[48px] text-text-primary dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'], letterSpacing: -1 }}
      >
        {value == null ? '—' : value}
      </Text>
      {hint ? (
        <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">{hint}</Text>
      ) : null}
    </Tile>
  );
  if (!onPress) return <>{body}</>;
  return (
    <HapticPressable
      haptic="light"
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`${label}: open`}
      className="flex-1"
      testID={testID}
    >
      {body}
    </HapticPressable>
  );
}
