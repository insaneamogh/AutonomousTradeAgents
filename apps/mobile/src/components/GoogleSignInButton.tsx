/**
 * "Continue with Google" button.
 *
 * Secondary-variant styling to match the existing dev-token button on the
 * login screen (same 44pt height, border, corner radius, disabled/pressed
 * treatment) — everything AROUND the icon uses this app's design tokens.
 * The G glyph's four fills are Google's own official brand colors, fixed
 * by their Identity branding guidelines (path data sourced from there,
 * not a freehand redraw) — those colors are Google's trademark, not this
 * app's palette, so they're deliberately exempt from the design-tokens
 * rule the rest of this component follows.
 */

import { ActivityIndicator, Pressable, Text } from 'react-native';
import Svg, { Path } from 'react-native-svg';

import { cn } from '@app/ui';

interface GoogleSignInButtonProps {
  onPress: () => void;
  loading?: boolean;
  disabled?: boolean;
  label?: string;
  testID?: string;
}

function GoogleGlyph() {
  return (
    <Svg width={18} height={18} viewBox="0 0 18 18">
      <Path
        fill="#4285F4"
        d="M17.64 9.2045c0-.6381-.0573-1.2518-.1636-1.8409H9v3.4814h4.8436c-.2086 1.125-.8427 2.0782-1.7959 2.7164v2.2581h2.9087c1.7018-1.5668 2.6836-3.8741 2.6836-6.615z"
      />
      <Path
        fill="#34A853"
        d="M9 18c2.43 0 4.4673-.8064 5.9564-2.1805l-2.9087-2.2581c-.8064.54-1.8368.8591-3.0477.8591-2.3427 0-4.3282-1.5818-5.0359-3.7104H.9573v2.3318C2.4382 15.9832 5.4818 18 9 18z"
      />
      <Path
        fill="#FBBC05"
        d="M3.9641 10.71c-.18-.54-.2822-1.1168-.2822-1.71s.1023-1.17.2822-1.71V4.9582H.9573C.3477 6.1732 0 7.5477 0 9s.3477 2.8268.9573 4.0418L3.9641 10.71z"
      />
      <Path
        fill="#EA4335"
        d="M9 3.5795c1.3214 0 2.5077.4541 3.4405 1.346l2.5813-2.5814C13.4632.8918 11.426 0 9 0 5.4818 0 2.4382 2.0168.9573 4.9582L3.9641 7.29C4.6718 5.1614 6.6573 3.5795 9 3.5795z"
      />
    </Svg>
  );
}

export function GoogleSignInButton({
  onPress,
  loading = false,
  disabled = false,
  label = 'Continue with Google',
  testID,
}: GoogleSignInButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <Pressable
      onPress={onPress}
      disabled={isDisabled}
      accessibilityRole="button"
      accessibilityLabel="Continue with Google"
      accessibilityState={{ disabled: isDisabled, busy: loading }}
      testID={testID}
      className={cn(
        'h-11 flex-row items-center justify-center gap-2 self-stretch rounded-md border px-4',
        'border-border-strong dark:border-border-strong-dark',
        'bg-bg-surface-elevated dark:bg-bg-surface-elevated-dark',
        isDisabled && 'opacity-50',
      )}
    >
      {loading ? (
        <ActivityIndicator size="small" />
      ) : (
        <>
          <GoogleGlyph />
          <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
            {label}
          </Text>
        </>
      )}
    </Pressable>
  );
}
