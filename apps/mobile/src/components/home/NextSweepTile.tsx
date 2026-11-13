// Countdown to the next council sweep. Hidden when the server has none.

import { useEffect, useState } from 'react';
import { Text } from 'react-native';

import { Tile, TileLabel } from '@/components/bento';
import { useAutonomy } from '@/hooks/useAutonomy';

import { formatCountdown } from './format';

export function NextSweepTile() {
  const { data } = useAutonomy();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const next = data?.nextSweepAt;
  if (!next) return null;
  const label = formatCountdown(next, now);
  if (!label) return null;
  return (
    <Tile className="flex-1 gap-1" testID="next-sweep">
      <TileLabel>Next sweep</TileLabel>
      <Text
        className="text-[28px] font-medium leading-[32px] text-text-primary dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'], letterSpacing: -0.6 }}
      >
        {label}
      </Text>
    </Tile>
  );
}
