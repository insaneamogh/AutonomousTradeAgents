// One fact: is the desk trading by itself. A single large verdict word;
// tap to open the checklist, where every failing check carries its fix.

import { useState } from 'react';
import { Text, View } from 'react-native';

import { HapticPressable, Skeleton, cn } from '@app/ui';

import { Tile, TileLabel } from '@/components/bento';
import { useAutonomy } from '@/hooks/useAutonomy';

const VERDICT: Record<string, { word: string; tone: string }> = {
  running: { word: 'RUNNING', tone: 'text-mint dark:text-mint-dark' },
  idle: { word: 'IDLE', tone: 'text-text-primary dark:text-text-primary-dark' },
  blocked: { word: 'BLOCKED', tone: 'text-rose dark:text-rose-dark' },
};

export function AutonomyTile() {
  const { data, isLoading, isError } = useAutonomy();
  const [open, setOpen] = useState(false);

  if (isLoading) {
    return (
      <Tile className="gap-2">
        <TileLabel>Autonomy</TileLabel>
        <Skeleton className="h-10 w-40" />
        <Skeleton className="h-4 w-3/4" />
      </Tile>
    );
  }
  if (isError || !data) {
    return (
      <Tile className="gap-1" testID="autonomy-unavailable">
        <TileLabel>Autonomy</TileLabel>
        <Text className="text-[28px] font-medium leading-[32px] text-text-tertiary dark:text-text-tertiary-dark">
          Status unavailable
        </Text>
        <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
          The server could not report whether the desk is trading.
        </Text>
      </Tile>
    );
  }

  const v = VERDICT[data.state] ?? { word: String(data.state).toUpperCase(), tone: '' };
  const checks = data.checks ?? [];
  const failing = checks.filter((c) => !c.ok).length;

  return (
    <HapticPressable
      haptic="light"
      onPress={() => setOpen((o) => !o)}
      accessibilityRole="button"
      accessibilityState={{ expanded: open }}
      accessibilityLabel={`Autonomy ${v.word}. ${open ? 'Hide' : 'Show'} checklist`}
      testID="autonomy-toggle"
    >
      <Tile className="gap-1.5">
        <TileLabel>Autonomy</TileLabel>
        <Text
          className={cn('text-[44px] font-medium leading-[48px]', v.tone)}
          style={{ letterSpacing: -1 }}
        >
          {v.word}
        </Text>
        <Text className="text-[13px] leading-[18px] text-text-secondary dark:text-text-secondary-dark">
          {data.headline}
        </Text>
        <Text className="text-[11px] text-text-tertiary dark:text-text-tertiary-dark">
          {failing > 0 ? `${failing} of ${checks.length} checks failing` : `${checks.length} checks ok`}
          {open ? ' · tap to hide' : ' · tap for checklist'}
        </Text>
        {open && (
          <View className="mt-2 gap-2.5" testID="autonomy-checklist">
            {checks.map((c) => (
              <View key={c.id} className="flex-row gap-2.5">
                <View
                  className={cn(
                    'mt-1.5 h-2 w-2 rounded-full',
                    c.ok
                      ? 'bg-mint dark:bg-mint-dark'
                      : c.severity === 'blocker'
                        ? 'bg-rose dark:bg-rose-dark'
                        : 'bg-text-tertiary dark:bg-text-tertiary-dark',
                  )}
                />
                <View className="flex-1 gap-0.5">
                  <Text className="text-[13px] font-medium text-text-primary dark:text-text-primary-dark">
                    {c.label}
                  </Text>
                  <Text className="text-[12px] text-text-secondary dark:text-text-secondary-dark">
                    {c.detail}
                  </Text>
                  {!c.ok && c.fix ? (
                    <Text className="text-[12px] font-medium text-text-primary dark:text-text-primary-dark">
                      Fix: {c.fix}
                    </Text>
                  ) : null}
                </View>
              </View>
            ))}
          </View>
        )}
      </Tile>
    </HapticPressable>
  );
}
