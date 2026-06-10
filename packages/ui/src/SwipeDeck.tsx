/**
 * SwipeDeck — generic stacked-card swiper with three actions.
 *
 * Designed for the Review tab but reusable: any list of items where the
 * operator makes a triage choice (approve / decline / skip — or in the
 * review context, good / bad / skip).
 *
 * Behavior:
 *  - Renders the top card full-bleed with the next-up card peeking below.
 *  - Swipe right → onAction('right'); swipe left → onAction('left');
 *    swipe up → onAction('up').
 *  - Tappable action buttons mirror the swipe gestures (accessibility +
 *    a discoverability path on first launch).
 *  - On action, the top card animates off-screen + the next one slides
 *    into place. When the deck empties, ``renderEmpty()`` shows.
 *
 * Implementation notes:
 *  - Built on react-native-gesture-handler + Reanimated 3 (both already
 *    declared in apps/mobile/package.json from Phase 0).
 *  - The deck is "controlled" — the parent maintains the items array
 *    and removes the top item on action. We never mutate items here so
 *    the parent's optimistic-update flow (TanStack Query) stays clean.
 */

import { useMemo } from 'react';
import { Pressable, Text, View } from 'react-native';
import Animated, {
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  withSpring,
  withTiming,
} from 'react-native-reanimated';
import { Gesture, GestureDetector } from 'react-native-gesture-handler';

import { cn } from './utils';

export type SwipeDirection = 'left' | 'right' | 'up';

interface SwipeAction {
  direction: SwipeDirection;
  label: string;
  tone: 'gain' | 'loss' | 'neutral';
  /** Visual hint icon-style emoji or initial. Optional. */
  icon?: string;
  accessibilityLabel?: string;
}

interface SwipeDeckProps<T> {
  items: T[];
  keyFor: (item: T) => string;
  renderItem: (item: T) => React.ReactNode;
  /** Called with the direction of the action AND the item that was on top. */
  onAction: (direction: SwipeDirection, item: T) => void;
  /** Optional renderer when ``items`` is empty. */
  renderEmpty?: () => React.ReactNode;
  /** Action buttons rendered below the deck. */
  actions: [SwipeAction, SwipeAction, SwipeAction];
}

const SWIPE_THRESHOLD = 100;

export function SwipeDeck<T>({
  items,
  keyFor,
  renderItem,
  onAction,
  renderEmpty,
  actions,
}: SwipeDeckProps<T>) {
  const top = items[0];
  const next = items[1];

  if (!top) {
    return (
      <View className="flex-1 items-center justify-center">
        {renderEmpty?.() ?? null}
      </View>
    );
  }

  return (
    <View className="flex-1 gap-4">
      <View className="flex-1 items-center justify-center">
        {next ? (
          <DeckCard key={`peek-${keyFor(next)}`} peek>
            {renderItem(next)}
          </DeckCard>
        ) : null}
        <TopDeckCard
          key={`top-${keyFor(top)}`}
          item={top}
          onAction={onAction}
          renderItem={renderItem}
        />
      </View>
      <ActionBar actions={actions} onAction={(dir) => onAction(dir, top)} />
    </View>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Top (interactive) card
// ─────────────────────────────────────────────────────────────────────

interface TopDeckCardProps<T> {
  item: T;
  renderItem: (item: T) => React.ReactNode;
  onAction: (direction: SwipeDirection, item: T) => void;
}

function TopDeckCard<T>({ item, renderItem, onAction }: TopDeckCardProps<T>) {
  const tx = useSharedValue(0);
  const ty = useSharedValue(0);

  const resolve = (direction: SwipeDirection) => {
    onAction(direction, item);
  };

  const gesture = useMemo(
    () =>
      Gesture.Pan()
        .onUpdate((e) => {
          tx.value = e.translationX;
          ty.value = e.translationY;
        })
        .onEnd((e) => {
          if (Math.abs(e.translationX) > SWIPE_THRESHOLD) {
            const dir: SwipeDirection = e.translationX > 0 ? 'right' : 'left';
            tx.value = withTiming(e.translationX > 0 ? 600 : -600, { duration: 180 });
            ty.value = withTiming(e.translationY, { duration: 180 });
            runOnJS(resolve)(dir);
          } else if (e.translationY < -SWIPE_THRESHOLD) {
            ty.value = withTiming(-600, { duration: 180 });
            tx.value = withTiming(e.translationX, { duration: 180 });
            runOnJS(resolve)('up');
          } else {
            tx.value = withSpring(0);
            ty.value = withSpring(0);
          }
        }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [item],
  );

  const animStyle = useAnimatedStyle(() => ({
    transform: [
      { translateX: tx.value },
      { translateY: ty.value },
      { rotate: `${(tx.value / 20).toFixed(2)}deg` },
    ],
  }));

  return (
    <GestureDetector gesture={gesture}>
      <Animated.View
        className="absolute inset-x-0 top-0 bottom-0"
        style={animStyle}
        accessibilityRole="button"
        accessibilityLabel="Swipe to grade — left for bad, right for good, up to skip"
      >
        <DeckCard>{renderItem(item)}</DeckCard>
      </Animated.View>
    </GestureDetector>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Static deck-card surface
// ─────────────────────────────────────────────────────────────────────

function DeckCard({
  children,
  peek = false,
}: {
  children: React.ReactNode;
  peek?: boolean;
}) {
  return (
    <View
      className={cn(
        'absolute inset-x-0 top-0 bottom-0 rounded-xl border p-4',
        'bg-bg-surface dark:bg-bg-surface-dark',
        'border-border-subtle dark:border-border-subtle-dark',
        peek && 'scale-95 opacity-60',
      )}
      style={{
        shadowColor: '#000',
        shadowOffset: { width: 0, height: 4 },
        shadowOpacity: peek ? 0.04 : 0.12,
        shadowRadius: 12,
        elevation: peek ? 1 : 6,
      }}
    >
      {children}
    </View>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Action bar — tap-equivalents of the swipe gestures
// ─────────────────────────────────────────────────────────────────────

function ActionBar({
  actions,
  onAction,
}: {
  actions: SwipeAction[];
  onAction: (direction: SwipeDirection) => void;
}) {
  return (
    <View className="flex-row gap-3">
      {actions.map((a) => (
        <Pressable
          key={a.direction}
          onPress={() => onAction(a.direction)}
          accessibilityRole="button"
          accessibilityLabel={a.accessibilityLabel ?? a.label}
          className={cn(
            'h-12 flex-1 items-center justify-center rounded-md',
            a.tone === 'gain' &&
              'bg-gain-subtle dark:bg-gain-subtle-dark',
            a.tone === 'loss' &&
              'bg-loss-subtle dark:bg-loss-subtle-dark',
            a.tone === 'neutral' &&
              'bg-bg-surface-elevated border border-border-strong dark:bg-bg-surface-elevated-dark dark:border-border-strong-dark',
          )}
        >
          <View className="flex-row items-center gap-2">
            {a.icon ? (
              <Text
                className={cn(
                  'text-[18px]',
                  a.tone === 'gain' && 'text-gain dark:text-gain-dark',
                  a.tone === 'loss' && 'text-loss dark:text-loss-dark',
                  a.tone === 'neutral' &&
                    'text-text-primary dark:text-text-primary-dark',
                )}
              >
                {a.icon}
              </Text>
            ) : null}
            <Text
              className={cn(
                'text-[14px] font-semibold',
                a.tone === 'gain' && 'text-gain dark:text-gain-dark',
                a.tone === 'loss' && 'text-loss dark:text-loss-dark',
                a.tone === 'neutral' &&
                  'text-text-primary dark:text-text-primary-dark',
              )}
            >
              {a.label}
            </Text>
          </View>
        </Pressable>
      ))}
    </View>
  );
}
