// Login screen — email input → POST /api/v1/auth/request-login → "check your email".
//
// In dev (when the API returns ``devToken`` on the response), we surface a
// "Continue with dev token" CTA that deep-links the user straight to the
// verify screen with the token prefilled. In prod this branch never fires
// because the API drops ``devToken`` from the response.

import { useState } from 'react';
import { Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { router } from 'expo-router';

import { Button, Card, cn } from '@app/ui';

import { ApiError, request } from '@/lib/api';

interface RequestLoginResponse {
  expiresAt: string;
  devToken: string | null;
}

export default function LoginScreen() {
  const [email, setEmail] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [challenge, setChallenge] = useState<RequestLoginResponse | null>(null);

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
      if (err instanceof ApiError) {
        const detail = typeof err.body === 'object' && err.body && 'detail' in err.body
          ? String((err.body as { detail: unknown }).detail)
          : err.message;
        setError(detail);
      } else {
        setError("Couldn't reach the agent server. Make sure the API is running.");
      }
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
      <View className="flex-1 px-6 pt-16 pb-8 gap-6">
        <View className="gap-2">
          <Text className="text-[28px] font-bold text-text-primary dark:text-text-primary-dark">
            Sign in
          </Text>
          <Text className="text-[15px] leading-[22px] text-text-secondary dark:text-text-secondary-dark">
            We'll email you a one-tap login link. No password.
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
      </View>
    </SafeAreaView>
  );
}
