// Verify screen — exchanges a magic-link token for access + refresh tokens.
//
// Three ways in:
//   1. Deep link: autotrader://auth/verify?email=...&token=...  → auto-submits.
//   2. Dev-mode "Continue with dev token" button on the login screen.
//   3. MANUAL: the user pastes a token (e.g. pulled from the server logs
//      in production, where the API doesn't return it in the response).
//      This is the path that makes testing against a prod Railway deploy
//      painless — no need to hand-craft a deep-link URL.
//
// On success → authStore flips to 'authenticated' → root layout redirects
// out of /auth → into /(tabs).

import { useEffect, useState } from 'react';
import { ActivityIndicator, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, router } from 'expo-router';

import { Button, Card, cn } from '@app/ui';

import { ApiError, request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

interface IssuedTokensResponse {
  userId: string;
  email: string;
  accessToken: string;
  refreshToken: string;
  accessExpiresInSeconds: number;
  refreshExpiresInSeconds: number;
}

export default function VerifyScreen() {
  const params = useLocalSearchParams<{ email?: string; token?: string }>();
  const signIn = useAuthStore((s) => s.signIn);

  // 'auto' = we arrived with a token (deep link / dev button) and auto-submit.
  // 'manual' = no token in params, show the paste form.
  const hasAutoToken = Boolean(params.email && params.token);

  const [status, setStatus] = useState<'verifying' | 'manual' | 'error' | 'done'>(
    hasAutoToken ? 'verifying' : 'manual',
  );
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState(params.email ?? '');
  const [token, setToken] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (hasAutoToken) {
      void verify(params.email as string, params.token as string);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.email, params.token]);

  async function verify(emailArg: string, tokenArg: string) {
    setSubmitting(true);
    setError(null);
    try {
      const issued = await request<IssuedTokensResponse>('/api/v1/auth/verify', {
        method: 'POST',
        body: { email: emailArg.trim(), token: tokenArg.trim() },
        skipAuth: true,
      });
      await signIn(issued);
      setStatus('done');
      // Root layout reacts to status='authenticated' + redirects; we also
      // push directly so there's no flash of /auth. Expo Router dedupes.
      router.replace('/');
    } catch (err) {
      setStatus('manual'); // fall back to the form so the user can retry
      if (err instanceof ApiError) {
        const detail =
          typeof err.body === 'object' && err.body && 'detail' in err.body
            ? String((err.body as { detail: unknown }).detail)
            : err.message;
        setError(detail);
      } else {
        setError("Couldn't reach the agent server. Check the API URL + try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  function onManualSubmit() {
    setError(null);
    if (!email.includes('@')) {
      setError('Enter the email you requested the link for.');
      return;
    }
    if (token.trim().length < 8) {
      setError('Paste the full login token.');
      return;
    }
    void verify(email, token);
  }

  return (
    <SafeAreaView edges={['top', 'bottom']} className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <View className="flex-1 justify-center px-6 pb-8 gap-6">
        {status === 'verifying' ? (
          <Card variant="default" className="items-center gap-4 px-8 py-10">
            <ActivityIndicator size="large" />
            <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
              Signing you in…
            </Text>
            <Text className="text-center text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
              Validating your login token with the agent server.
            </Text>
          </Card>
        ) : null}

        {status === 'manual' ? (
          <>
            <View className="gap-2">
              <Text className="text-[24px] font-bold text-text-primary dark:text-text-primary-dark">
                Enter your token
              </Text>
              <Text className="text-[14px] leading-[20px] text-text-secondary dark:text-text-secondary-dark">
                Paste the login token from your email — or, in a test deploy, from the server logs
                (search for "magic-link issued").
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

              <View className="gap-2">
                <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
                  Login token
                </Text>
                <TextInput
                  value={token}
                  onChangeText={(t) => {
                    setToken(t);
                    setError(null);
                  }}
                  placeholder="paste token here"
                  placeholderTextColor="#9CA3AF"
                  autoCapitalize="none"
                  autoCorrect={false}
                  multiline
                  accessibilityLabel="Login token"
                  editable={!submitting}
                  className={cn(
                    'min-h-11 rounded-md border px-3 py-2.5 text-[14px]',
                    'border-border-strong dark:border-border-strong-dark',
                    'bg-bg-surface-elevated dark:bg-bg-surface-elevated-dark',
                    'text-text-primary dark:text-text-primary-dark',
                  )}
                />
              </View>

              <Button
                label={submitting ? 'Verifying…' : 'Verify + sign in'}
                onPress={onManualSubmit}
                loading={submitting}
                disabled={submitting}
                fullWidth
                accessibilityLabel="Verify and sign in"
                testID="verify-manual-submit"
              />

              {error ? (
                <Text className="text-[13px] leading-[19px] text-danger dark:text-danger-dark">
                  {error}
                </Text>
              ) : null}
            </Card>

            <Button
              label="Back to login"
              variant="tertiary"
              onPress={() => router.replace('/auth/login')}
              testID="verify-back-to-login"
            />
          </>
        ) : null}
      </View>
    </SafeAreaView>
  );
}
