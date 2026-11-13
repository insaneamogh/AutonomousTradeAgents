import { Text } from 'react-native';
import { useRouter } from 'expo-router';

import { HapticPressable, cn } from '@app/ui';

import { Tile, TileLabel } from '@/components/bento';
import { useTradingMode } from '@/hooks/useTradingMode';

/** Paper vs live. Hidden on servers without the endpoint. */
export function TradingModeTile() {
  const router = useRouter();
  const { data, isError } = useTradingMode();
  if (isError || !data) return null;
  const live = data.mode === 'live';
  return (
    <HapticPressable
      haptic="light"
      onPress={() => router.push('/settings')}
      accessibilityRole="button"
      accessibilityLabel={`Trading mode ${data.mode}. Open Settings`}
      className="flex-1"
      testID="trading-mode-tile"
    >
      <Tile className="gap-1">
        <TileLabel>Mode</TileLabel>
        <Text
          className={cn(
            'text-[28px] font-medium leading-[32px]',
            live ? 'text-rose dark:text-rose-dark' : 'text-text-primary dark:text-text-primary-dark',
          )}
          style={{ letterSpacing: -0.6 }}
        >
          {live ? 'LIVE' : 'PAPER'}
        </Text>
        <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">Settings →</Text>
      </Tile>
    </HapticPressable>
  );
}
