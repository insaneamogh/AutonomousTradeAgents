/**
 * Desktop pre-auth screen — Platinum Glass.
 *
 * Mounted by `DesktopShell` on the wide-web surface whenever there is no
 * session yet — replacing the mobile login/verify screens that used to be
 * framed in a phone-width column here. See `DesktopShell`'s docstring:
 * the whole point of that switch point is that the mobile tree is never
 * mounted on this surface, and until this file existed that guarantee
 * silently didn't cover the pre-auth state — a judge on a desktop browser
 * saw the phone UI (in a narrow column) for the entire login flow, not
 * just a flash.
 *
 * One combined email + access-token form, not the mobile flow's two
 * separate screens (request → "check your email" → verify) — a desktop
 * visitor is not on a phone waiting on a push notification; one page
 * reads as a normal sign-in. Still backs onto the exact same two
 * endpoints the mobile flow uses (`/api/v1/auth/request-login`,
 * `/api/v1/auth/verify`) and the same `authStore.signIn()` — no new auth
 * mechanism, no password ever stored anywhere. "Get a token" calls
 * request-login and auto-fills the field when the API returns a
 * `devToken` (non-production only — see the mobile login screen's own
 * comment on that field); in production it just confirms an email went
 * out and the visitor pastes the token in below.
 *
 * A real magic-link click (the URL already carries `?email=&token=`)
 * still auto-verifies on mount, exactly like the mobile verify screen —
 * requesting the link from a phone and opening it on a desktop browser
 * should not require retyping anything.
 */

import { useEffect, useRef, useState } from 'react';
import { useColorScheme } from 'nativewind';
import { router, useGlobalSearchParams } from 'expo-router';

import { authErrorMessage, request } from '@/lib/api';
import { useGoogleSignIn } from '@/hooks/useGoogleSignIn';
import { useAuthStore } from '@/stores/authStore';

import { installPlatinumGlass } from './runtime';
import { Button, Card, Label, Row, Stack } from './primitives';

interface RequestLoginResponse {
  expiresAt: string;
  devToken: string | null;
}

interface IssuedTokensResponse {
  userId: string;
  email: string;
  accessToken: string;
  refreshToken: string;
  accessExpiresInSeconds: number;
  refreshExpiresInSeconds: number;
}

/**
 * Same one-shot-redemption guard as the mobile verify screen, and for the
 * same reason: a token is invalidated the instant it's redeemed, and this
 * component can remount with the same URL params still in place (Fast
 * Refresh in dev, or a re-render before navigation away completes).
 * Module-level so it survives a remount — a `useRef` would not.
 */
const redeemedPairs = new Set<string>();

export default function DesktopAuth() {
  // Idempotent + synchronous, same call site pattern as DesktopApp — the
  // pre-auth surface needs the stylesheet/fonts too, and DesktopApp isn't
  // mounted yet to have done it for us.
  installPlatinumGlass();
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme !== 'light';

  const params = useGlobalSearchParams<{ email?: string; token?: string }>();
  const hasAutoToken = Boolean(params.email && params.token);

  const signIn = useAuthStore((s) => s.signIn);
  const { signInAsync: googleSignInAsync } = useGoogleSignIn();

  const [mode, setMode] = useState<'form' | 'verifying'>(hasAutoToken ? 'verifying' : 'form');
  const [email, setEmail] = useState(params.email ?? '');
  const [token, setToken] = useState(params.token ?? '');
  const [requesting, setRequesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [googleSubmitting, setGoogleSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const autoTried = useRef(false);

  useEffect(() => {
    if (autoTried.current || !hasAutoToken) return;
    const pair = `${params.email}:${params.token}`;
    if (redeemedPairs.has(pair)) return;
    redeemedPairs.add(pair);
    autoTried.current = true;
    void verify(params.email as string, params.token as string);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.email, params.token]);

  async function verify(emailArg: string, tokenArg: string) {
    setError(null);
    setSubmitting(true);
    try {
      const issued = await request<IssuedTokensResponse>('/api/v1/auth/verify', {
        method: 'POST',
        body: { email: emailArg.trim(), token: tokenArg.trim() },
        skipAuth: true,
      });
      await signIn(issued);
      router.replace('/');
    } catch (err) {
      setMode('form');
      setError(authErrorMessage(err, "Couldn't verify that token. Check it and try again."));
    } finally {
      setSubmitting(false);
    }
  }

  async function onGetToken() {
    setError(null);
    setHint(null);
    if (!email.includes('@') || email.length < 3) {
      setError('Enter a valid email first.');
      return;
    }
    setRequesting(true);
    try {
      const res = await request<RequestLoginResponse>('/api/v1/auth/request-login', {
        method: 'POST',
        body: { email: email.trim() },
        skipAuth: true,
      });
      if (res.devToken) {
        setToken(res.devToken);
        setHint('Dev mode — token filled in below. Click Sign in.');
      } else {
        setHint(`Sent to ${email.trim()}. Paste the token below once it arrives.`);
      }
    } catch (err) {
      setError(authErrorMessage(err, "Couldn't reach the agent server. Make sure the API is running."));
    } finally {
      setRequesting(false);
    }
  }

  function onSubmit() {
    setError(null);
    if (!email.includes('@') || email.length < 3) {
      setError('Enter a valid email.');
      return;
    }
    if (token.trim().length < 8) {
      setError('Enter your access token — use "Get a token" below if you don’t have one yet.');
      return;
    }
    void verify(email, token);
  }

  async function onGooglePress() {
    setError(null);
    setGoogleSubmitting(true);
    try {
      await googleSignInAsync();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Google sign-in failed.');
    } finally {
      setGoogleSubmitting(false);
    }
  }

  return (
    <div className="pg-root" data-pg-theme={isDark ? 'dark' : 'light'}>
      <div
        style={{
          position: 'relative',
          zIndex: 1,
          height: '100%',
          overflowY: 'auto',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 24,
        }}
      >
        <div style={{ width: '100%', maxWidth: 420, display: 'flex', flexDirection: 'column', gap: 20 }}>
          <Stack gap={6}>
            <span className="label-caps">Autonomous Trader</span>
            <h1 className="pg-h2">Sign in</h1>
            <p className="pg-body-sm">
              Your email as the username, an access token as the password. No token yet? Get one below.
            </p>
          </Stack>

          {mode === 'verifying' ? (
            <Card variant="hero" style={{ alignItems: 'center', textAlign: 'center', gap: 14 }}>
              <Spinner />
              <span className="pg-body-sm">Signing you in…</span>
            </Card>
          ) : (
            <Card variant="hero" style={{ gap: 16 }}>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  onSubmit();
                }}
                style={{ display: 'flex', flexDirection: 'column', gap: 16 }}
              >
                <Stack gap={6}>
                  <Label>Email</Label>
                  <input
                    className="pg-input"
                    type="email"
                    value={email}
                    onChange={(e) => {
                      setEmail(e.target.value);
                      setError(null);
                    }}
                    placeholder="you@example.com"
                    autoComplete="username"
                    disabled={submitting}
                  />
                </Stack>

                <Stack gap={6}>
                  <Row style={{ justifyContent: 'space-between' }}>
                    <Label>Access token</Label>
                    <Button
                      kind="ghost"
                      size="sm"
                      type="button"
                      onClick={onGetToken}
                      disabled={requesting || submitting}
                    >
                      {requesting ? 'Sending…' : 'Get a token'}
                    </Button>
                  </Row>
                  <input
                    className="pg-input"
                    type="text"
                    value={token}
                    onChange={(e) => {
                      setToken(e.target.value);
                      setError(null);
                    }}
                    placeholder="paste your token here"
                    autoComplete="current-password"
                    disabled={submitting}
                    style={{ fontFamily: 'var(--pg-font-num)' }}
                  />
                </Stack>

                {hint ? <p className="pg-caption">{hint}</p> : null}
                {error ? <p className="pg-caption pg-bear">{error}</p> : null}

                <Button kind="primary" type="submit" disabled={submitting} style={{ width: '100%' }}>
                  {submitting ? 'Signing in…' : 'Sign in'}
                </Button>
              </form>

              <Row gap={10}>
                <div style={{ height: 1, flex: 1, backgroundColor: 'var(--pg-card-border)' }} />
                <span className="pg-caption">or</span>
                <div style={{ height: 1, flex: 1, backgroundColor: 'var(--pg-card-border)' }} />
              </Row>

              <Button
                kind="secondary"
                type="button"
                onClick={onGooglePress}
                disabled={googleSubmitting}
                ariaLabel="Continue with Google"
                style={{ width: '100%' }}
              >
                <GoogleGlyph />
                {googleSubmitting ? 'Signing in…' : 'Continue with Google'}
              </Button>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

function Spinner() {
  return (
    <div
      aria-hidden
      className="pg-spin"
      style={{
        width: 28,
        height: 28,
        borderRadius: 9999,
        border: '3px solid var(--pg-outline-variant)',
        borderTopColor: 'var(--pg-primary)',
      }}
    />
  );
}

/**
 * Google's own official brand-guideline colors for the "G" glyph — a
 * trademark, not a Platinum Glass token, so exempt from the tokens-only
 * rule (same exemption `GoogleSignInButton.tsx` documents on mobile).
 */
function GoogleGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden focusable="false">
      <path
        fill="#4285F4"
        d="M17.64 9.2045c0-.6381-.0573-1.2518-.1636-1.8409H9v3.4814h4.8436c-.2086 1.125-.8427 2.0782-1.7959 2.7164v2.2581h2.9087c1.7018-1.5668 2.6836-3.8741 2.6836-6.615z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.4673-.8064 5.9564-2.1805l-2.9087-2.2581c-.8064.54-1.8368.8591-3.0477.8591-2.3427 0-4.3282-1.5818-5.0359-3.7104H.9573v2.3318C2.4382 15.9832 5.4818 18 9 18z"
      />
      <path
        fill="#FBBC05"
        d="M3.9641 10.71c-.18-.54-.2822-1.1168-.2822-1.71s.1023-1.17.2822-1.71V4.9582H.9573C.3477 6.1732 0 7.5477 0 9s.3477 2.8268.9573 4.0418L3.9641 10.71z"
      />
      <path
        fill="#EA4335"
        d="M9 3.5795c1.3214 0 2.5077.4541 3.4405 1.346l2.5813-2.5814C13.4632.8918 11.426 0 9 0 5.4818 0 2.4382 2.0168.9573 4.9582L3.9641 7.29C4.6718 5.1614 6.6573 3.5795 9 3.5795z"
      />
    </svg>
  );
}
