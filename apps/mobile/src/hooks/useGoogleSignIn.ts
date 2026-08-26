/**
 * "Continue with Google" — Authorization Code + PKCE against Google's
 * standard OIDC discovery document (RFC 7636 covers a public/native
 * client with no client secret).
 *
 * Primary path: exchange the authorization code directly with Google,
 * client-side (`AuthSession.exchangeCodeAsync`). If that exchange fails —
 * e.g. the Google OAuth client type that ends up configured in the
 * console is policy-confidential (typically "Web application") and
 * refuses a secret-less exchange — fall back to POSTing
 * `{code, codeVerifier, redirectUri}` to the backend's
 * `/auth/google/exchange`, which holds the one secret this app never can
 * (mirrors `alpaca_oauth.exchange_code_for_tokens`'s server-side leg).
 * Either path ends up with a bare Google ID token, which then goes to
 * `/auth/google` exactly like the primary path would have used directly
 * — there is only one place that ever decides "is this identity real",
 * and it's the backend's `google_oauth.verify_google_id_token`.
 *
 * NEEDS REAL-DEVICE QA: driving a real system-browser round trip against
 * Google isn't meaningfully testable in Jest. Which of the two exchange
 * paths actually fires in practice depends on which Google OAuth client
 * type ends up configured — that's an external prerequisite (a real
 * Google Cloud OAuth client) this hook can't set up on its own.
 */

import { useCallback, useMemo } from 'react';
import { Platform } from 'react-native';
import * as AuthSession from 'expo-auth-session';
import * as WebBrowser from 'expo-web-browser';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

// Lets the in-app browser close itself and hand control back to
// promptAsync() on the redirect. Expo's AuthSession docs call this out as
// required exactly once per app; harmless off the redirect path.
WebBrowser.maybeCompleteAuthSession();

const GOOGLE_DISCOVERY_ISSUER = 'https://accounts.google.com';

interface IssuedTokensResponse {
  userId: string;
  email: string;
  accessToken: string;
  refreshToken: string;
  accessExpiresInSeconds: number;
  refreshExpiresInSeconds: number;
}

/** `process.env[name]` narrowed to a definite `string` (never the literal `'undefined'`). */
function envString(name: string): string {
  const value: unknown = process.env[name];
  return typeof value === 'string' ? value : '';
}

/**
 * Google issues a SEPARATE OAuth client id per platform for one logical
 * app. An empty string (not configured yet for this platform) is handled
 * by the caller — the button stays visible but refuses to ask Google to
 * authorize an empty client id.
 */
function resolveClientId(): string {
  if (Platform.OS === 'ios') return envString('EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID');
  if (Platform.OS === 'android') return envString('EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID');
  return envString('EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID');
}

export interface UseGoogleSignInResult {
  /** False when no client id is configured for this platform yet. */
  isConfigured: boolean;
  /** Runs the full flow and signs in on success. Throws on any failure —
   * the caller (the login screen) is responsible for showing the message.
   * Resolves quietly (no throw, no sign-in) if the user backs out.
   */
  signInAsync: () => Promise<void>;
}

export function useGoogleSignIn(): UseGoogleSignInResult {
  const signIn = useAuthStore((s) => s.signIn);
  const clientId = useMemo(resolveClientId, []);
  const redirectUri = useMemo(
    () => AuthSession.makeRedirectUri({ scheme: 'autotrader', path: 'auth/google/callback' }),
    [],
  );
  const discovery = AuthSession.useAutoDiscovery(GOOGLE_DISCOVERY_ISSUER);

  const [authRequest, , promptAsync] = AuthSession.useAuthRequest(
    {
      // useAuthRequest must be called unconditionally (rules of hooks) even
      // before we know a real client id exists for this platform —
      // signInAsync refuses to actually prompt in that case, below.
      clientId: clientId || 'unconfigured',
      scopes: ['openid', 'profile', 'email'],
      redirectUri,
      responseType: AuthSession.ResponseType.Code,
      usePKCE: true,
    },
    discovery,
  );

  const signInAsync = useCallback(async () => {
    if (!clientId) {
      throw new Error('Google sign-in is not configured yet for this platform.');
    }
    if (!discovery) {
      throw new Error("Couldn't reach Google — check your connection and try again.");
    }

    const result = await promptAsync();

    if (result.type === 'cancel' || result.type === 'dismiss') {
      return; // user backed out — not an error, nothing to report
    }
    if (result.type !== 'success') {
      // Everything else: a real 'error', or the web-only 'opened'/'locked'
      // states (a popup with no way to await it, or a session already in
      // flight) that this native in-app-browser flow doesn't realistically
      // hit but the shared union type still covers. `'params' in result`
      // is the narrowing guard — only the 'error'/'success' branch of the
      // union carries that field.
      const description =
        'params' in result
          ? result.error?.message || result.params.error_description || result.type
          : result.type;
      throw new Error(`Google sign-in failed: ${description}`);
    }
    if (!result.params.code) {
      throw new Error('Google sign-in did not return an authorization code.');
    }

    const code = result.params.code;
    const codeVerifier = authRequest?.codeVerifier;
    if (!codeVerifier) {
      throw new Error('Missing PKCE verifier — restart Google sign-in.');
    }

    let idToken: string | null = null;
    try {
      const exchanged = await AuthSession.exchangeCodeAsync(
        {
          clientId,
          code,
          redirectUri,
          extraParams: { code_verifier: codeVerifier },
        },
        discovery,
      );
      idToken = exchanged.idToken ?? null;
    } catch {
      // Swallow and fall through to the backend fallback leg below. We
      // deliberately don't branch on the specific error: some Google
      // OAuth client types (typically "Web application") are
      // policy-confidential and refuse a secret-less exchange, and we
      // can't know in advance which type ends up configured. Trying the
      // backend fallback is always safe either way.
      idToken = null;
    }

    if (idToken) {
      const issued = await request<IssuedTokensResponse>('/api/v1/auth/google', {
        method: 'POST',
        body: { idToken },
        skipAuth: true,
      });
      await signIn(issued);
      return;
    }

    const issued = await request<IssuedTokensResponse>('/api/v1/auth/google/exchange', {
      method: 'POST',
      body: { code, codeVerifier, redirectUri },
      skipAuth: true,
    });
    await signIn(issued);
  }, [authRequest, clientId, discovery, promptAsync, redirectUri, signIn]);

  return { isConfigured: Boolean(clientId), signInAsync };
}
