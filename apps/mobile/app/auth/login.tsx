// Login screen — email input → POST /api/v1/auth/request-login → "check your email".
//
// In dev (when the API returns ``devToken`` on the response), we surface a
// "Continue with dev token" CTA that deep-links the user straight to the
// verify screen with the token prefilled. In prod this branch never fires
// because the API drops ``devToken`` from the response.
//
// "Continue with Google" is a second, independent login method below the
// magic-link card — it doesn't replace or gate on the email flow above.

import { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { router } from 'expo-router';

import { Button, Card, cn } from '@app/ui';

import { GoogleSignInButton } from '@/components/GoogleSignInButton';
import { useGoogleSignIn } from '@/hooks/useGoogleSignIn';
import { authErrorMessage, request } from '@/lib/api';

interface RequestLoginResponse {
  expiresAt: string;
  devToken: string | null;
}

/**
 * Hides the browser's scrollbar chrome while keeping the ScrollView fully
 * scrollable — `scrollbarWidth` is a react-native-web extension (not in
 * core RN's style types, hence the cast) that RN-Web's own ScrollView
 * implementation special-cases: setting it to `'none'` emits BOTH the
 * Firefox-standard `scrollbar-width: none` AND an auto-generated
 * `::-webkit-scrollbar { display: none }` rule, so no separate global
 * CSS is needed. Web-only — native has no browser scrollbar to hide.
 */
const webHideScrollbarStyle =
  Platform.OS === 'web' ? ({ scrollbarWidth: 'none' } as Record<string, string>) : undefined;

export default function LoginScreen() {
  const [email, setEmail] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [challenge, setChallenge] = useState<RequestLoginResponse | null>(null);

  const { signInAsync: googleSignInAsync } = useGoogleSignIn();
  const [googleSubmitting, setGoogleSubmitting] = useState(false);
  const [googleError, setGoogleError] = useState<string | null>(null);

  async function onGooglePress() {
    setGoogleError(null);
    setGoogleSubmitting(true);
    try {
      await googleSignInAsync();
      // On success, signInAsync() has already called authStore.signIn();
      // the root layout's AuthRouteGuard reacts to status='authenticated'
      // and redirects out of /auth on its own — no explicit push needed.
    } catch (err) {
      setGoogleError(err instanceof Error ? err.message : 'Google sign-in failed.');
    } finally {
      setGoogleSubmitting(false);
    }
  }

  async function onSubmit() {
    setError(null);
    setChallenge(null);
    if (!email.includes('@') || email.length < 3) {
      setError('Enter a valid email.');
      return;
    }
    setSubmitting(true);
    try {
      const res = await request<RequestLoginResponse>('/api/v1/auth/request-login', {
        method: 'POST',
        body: { email: email.trim() },
        skipAuth: true,
      });
      setChallenge(res);
    } catch (err) {
      setError(authErrorMessage(err, "Couldn't reach the agent server. Make sure the API is running."));
    } finally {
      setSubmitting(false);
    }
  }

  function continueWithDevToken() {
    if (!challenge?.devToken) return;
    router.push({
      pathname: '/auth/verify',
      params: { email: email.trim(), token: challenge.devToken },
    });
  }

  function enterTokenManually() {
    // Navigate to the verify screen WITHOUT a token → it shows the paste
    // form, prefilled with the email. The path for prod deploys where the
    // token comes via email / server logs rather than the response body.
    router.push({
      pathname: '/auth/verify',
      params: { email: email.trim() },
    });
  }

  return (
    <SafeAreaView edges={['top', 'bottom']} className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        className="flex-1"
      >
        <ScrollView
          contentContainerClassName="px-6 pt-16 pb-8 gap-6"
          keyboardShouldPersistTaps="handled"
          style={webHideScrollbarStyle}
        >
          <View className="gap-2">
            <Text className="text-[28px] font-bold text-text-primary dark:text-text-primary-dark">
              Sign in
            </Text>
            <Text className="text-[15px] leading-[22px] text-text-secondary dark:text-text-secondary-dark">
              We&apos;ll email you a one-tap login link. No password.
            </Text>
          </View>

          <Card variant="default" className="gap-4">
            <View className="gap-2">
              <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
                Email
              </Text>
              <TextInput
                value={email}
                onChangeText={(t) => {
                  setEmail(t);
                  setError(null);
                }}
                placeholder="you@example.com"
                placeholderTextColor="#9CA3AF"
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="email-address"
                textContentType="emailAddress"
                accessibilityLabel="Email"
                editable={!submitting}
                className={cn(
                  'h-11 rounded-md border px-3 text-[15px]',
                  'border-border-strong dark:border-border-strong-dark',
                  'bg-bg-surface-elevated dark:bg-bg-surface-elevated-dark',
                  'text-text-primary dark:text-text-primary-dark',
                )}
              />
            </View>

            <Button
              label={submitting ? 'Sending…' : 'Send login link'}
              onPress={onSubmit}
              loading={submitting}
              disabled={submitting || email.length === 0}
              fullWidth
              accessibilityLabel="Send login link"
              testID="login-submit"
            />

            {error ? (
              <Text className="text-[13px] leading-[19px] text-danger dark:text-danger-dark">
                {error}
              </Text>
            ) : null}
          </Card>

          <View className="flex-row items-center gap-3">
            <View className="h-px flex-1 bg-border-strong dark:bg-border-strong-dark" />
            <Text className="text-[12px] font-medium uppercase tracking-[1.2px] text-text-tertiary dark:text-text-tertiary-dark">
              or
            </Text>
            <View className="h-px flex-1 bg-border-strong dark:bg-border-strong-dark" />
          </View>

          <View className="gap-2">
            <GoogleSignInButton
              onPress={onGooglePress}
              loading={googleSubmitting}
              testID="login-google"
            />
            {googleError ? (
              <Text className="text-[13px] leading-[19px] text-danger dark:text-danger-dark">
                {googleError}
              </Text>
            ) : null}
          </View>

          {challenge ? (
            <Card variant="inset" className="gap-3">
              <Text className="text-[13px] font-semibold text-text-primary dark:text-text-primary-dark">
                Check your email
              </Text>
              <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
                A magic link is on the way to {email}. Tap it to sign in. It expires in 15 minutes.
              </Text>

              {challenge.devToken ? (
                <View className="gap-2 pt-2">
                  <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-warning dark:text-warning-dark">
                    Dev mode
                  </Text>
                  <Text className="text-[12px] leading-[17px] text-text-tertiary dark:text-text-tertiary-dark">
                    The API returned the token in the response. Continue directly:
                  </Text>
                  <Button
                    label="Continue with dev token"
                    variant="secondary"
                    onPress={continueWithDevToken}
                    fullWidth
                    accessibilityLabel="Continue with dev token"
                    testID="login-dev-continue"
                  />
                </View>
              ) : (
                <View className="gap-2 pt-2">
                  <Text className="text-[12px] leading-[17px] text-text-tertiary dark:text-text-tertiary-dark">
                    Got the token from your email or the server logs? Enter it manually:
                  </Text>
                  <Button
                    label="Enter token manually"
                    variant="secondary"
                    onPress={enterTokenManually}
                    fullWidth
                    accessibilityLabel="Enter login token manually"
                    testID="login-manual-token"
                  />
                </View>
              )}
            </Card>
          ) : null}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}
