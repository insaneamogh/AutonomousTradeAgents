/** Settings — broker connections, watchlist, appearance, session. */

import { useEffect, useRef, useState } from 'react';
import { useColorScheme } from 'nativewind';

import {
  startAlpacaOAuth,
  startZerodhaConnect,
  useBrokerCatalog,
  useBrokerConnections,
  useRevokeBrokerConnection,
  useSetLiveTradingConsent,
  useVerifyBrokerConnection,
} from '@/hooks/useBrokerConnections';
import type {
  BrokerCatalogEntry,
  BrokerConnection,
  VerifyConnectionResult,
} from '@/hooks/useBrokerConnections';
import { LIVE_CONFIRM_PHRASE, useSetTradingMode, useTradingMode } from '@/hooks/useTradingMode';
import { useAddWatchlistSymbol, useRemoveWatchlistSymbol, useWatchlist } from '@/hooks/useWatchlist';
import { useHealthFull } from '@/hooks/useHealthFull';
import { useTickerCombobox } from '@/hooks/useTickerCombobox';
import { DEMO_DISABLED_REASON, useIsDemoSession } from '@/lib/demoSession';
import { useAuthStore } from '@/stores/authStore';
import { useThemeStore } from '@/stores/themeStore';
import type { ThemePreference } from '@/stores/themeStore';

import { ago, money } from '../format';
import { IconCross } from '../icons';
import {
  Button,
  Card,
  CardHead,
  Cell,
  Label,
  Numeral,
  PageHead,
  Pill,
  Row,
  SkelRows,
  Stack,
} from '../primitives';

const THEMES: { id: ThemePreference; label: string }[] = [
  { id: 'system', label: 'System' },
  { id: 'light', label: 'Refined Daylight' },
  { id: 'dark', label: 'Platinum Glass' },
];

/**
 * Surface the server's own explanation for a failed watchlist add.
 *
 * Mirrors `runErrorMessage` in Picks.tsx: a 422/409 here already names the
 * exact problem (untradable ticker, watchlist cap) — showing a generic
 * "something went wrong" would throw that away.
 */
function addSymbolErrorMessage(err: unknown): string {
  const e = err as { status?: number; body?: { detail?: string } } | null;
  const detail = e?.body?.detail;
  if (typeof detail === 'string' && detail) return detail;
  return "Couldn't add the symbol — try again.";
}

/** The server's own explanation for a failed call (`err.body.detail`), else
 * `fallback`. Same shape as `addSymbolErrorMessage` above. */
function apiErrorMessage(err: unknown, fallback: string): string {
  const e = err as { body?: { detail?: unknown } } | null;
  const detail = e?.body?.detail;
  if (typeof detail === 'string' && detail) return detail;
  return fallback;
}

/** Brokers that have a connect flow in this build. */
const CONNECTABLE = new Set(['alpaca', 'zerodha']);

/** Used when `/broker/catalog` is unavailable (older server), so the two
 * real flows are never lost. */
const FALLBACK_CATALOG: BrokerCatalogEntry[] = [
  {
    id: 'alpaca', name: 'Alpaca', market: 'US', currency: 'USD', auth: 'oauth',
    status: 'available', supportsPaper: true, supportsOptions: true, note: null, missingConfig: [],
  },
  {
    id: 'zerodha', name: 'Zerodha', market: 'IN', currency: 'INR', auth: 'kite',
    status: 'available', supportsPaper: false, supportsOptions: false, note: null, missingConfig: [],
  },
];

type VerifyOutcome = { kind: 'ok' | 'fail'; text: string };

function verifyOutcome(r: VerifyConnectionResult): VerifyOutcome {
  if (!r.ok) return { kind: 'fail', text: r.error || 'Verification failed.' };
  return {
    kind: 'ok',
    text: `Verified · equity ${money(r.equity, r.currency, 2)} · buying power ${money(r.buyingPower, r.currency, 2)}`,
  };
}

function BrokerConnectionsCard({ isDemo }: { isDemo: boolean }) {
  const connections = useBrokerConnections();
  const catalogQuery = useBrokerCatalog();
  const revoke = useRevokeBrokerConnection();
  const verify = useVerifyBrokerConnection();
  const consent = useSetLiveTradingConsent();

  const [pending, setPending] = useState<{ broker: string; url: string } | null>(null);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [starting, setStarting] = useState<string | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);
  const [verifying, setVerifying] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<Record<string, VerifyOutcome>>({});
  const [consentError, setConsentError] = useState<string | null>(null);

  const all = connections.data ?? [];
  const revokedRows = all.filter((c) => c.status === 'revoked');
  const rows = all.filter((c) => c.status !== 'revoked');
  const catalog = catalogQuery.data && catalogQuery.data.length > 0 ? catalogQuery.data : FALLBACK_CATALOG;

  const isHealthy = (c: BrokerConnection) => c.status === 'active' && !c.needsReconnect;
  const healthyBrokers = new Set(all.filter(isHealthy).map((c) => c.broker));

  const refetch = connections.refetch;

  // The login happens in another tab: refetch when the user comes back.
  useEffect(() => {
    if (!pending) return;
    const onBack = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      void refetch();
    };
    window.addEventListener('focus', onBack);
    document.addEventListener('visibilitychange', onBack);
    return () => {
      window.removeEventListener('focus', onBack);
      document.removeEventListener('visibilitychange', onBack);
    };
  }, [pending, refetch]);

  // ...and stop waiting once that broker has a healthy connection.
  const pendingConnected = pending ? healthyBrokers.has(pending.broker) : false;
  useEffect(() => {
    if (pendingConnected) setPending(null);
  }, [pendingConnected]);

  const startConnect = async (broker: string, isPaper = true) => {
    setConnectError(null);
    setStarting(broker);
    try {
      let url: string;
      if (broker === 'zerodha') {
        const res = await startZerodhaConnect();
        url = res.loginUrl;
      } else {
        const res = await startAlpacaOAuth(isPaper, 'web');
        // oauthNotConfigured: Alpaca's own page would reject with a generic
        // "unknown client" — show the server's warning instead.
        if (res.oauthNotConfigured) {
          setConnectError(res.devWarning ?? 'Alpaca OAuth is not configured on this server.');
          return;
        }
        url = res.authorizeUrl;
      }
      // New tab: navigating this one away strands the user on a bare API page.
      window.open(url, '_blank', 'noopener');
      setPending({ broker, url });
    } catch (err) {
      setConnectError(apiErrorMessage(err, `Could not start the ${broker} connection.`));
    } finally {
      setStarting(null);
    }
  };

  const runVerify = async (id: string) => {
    setVerifying(id);
    try {
      const r = await verify.mutateAsync(id);
      setOutcomes((o) => ({ ...o, [id]: verifyOutcome(r) }));
    } catch (err) {
      setOutcomes((o) => ({ ...o, [id]: { kind: 'fail', text: apiErrorMessage(err, 'Could not reach the server to verify.') } }));
    } finally {
      setVerifying(null);
    }
  };

  const toggleConsent = (c: BrokerConnection) => {
    const enabled = !c.liveTradingConsent;
    if (
      enabled &&
      !window.confirm(
        `Allow real-money trading on this ${c.broker} account? Orders placed through it will spend real money.`,
      )
    ) {
      return;
    }
    setConsentError(null);
    consent.mutate(
      { connectionId: c.id, enabled },
      { onError: (err) => setConsentError(apiErrorMessage(err, "Couldn't update real-money consent.")) },
    );
  };

  const demoTitle = isDemo ? DEMO_DISABLED_REASON : undefined;
  const pendingName = pending ? (catalog.find((b) => b.id === pending.broker)?.name ?? pending.broker) : '';

  const renderRow = (c: BrokerConnection) => {
    const revoked = c.status === 'revoked';
    const outcome = outcomes[c.id];
    return (
      <div key={c.id} className="pg-inset" style={{ minWidth: 0 }}>
        <Stack gap={8}>
          <Row gap={8} style={{ flexWrap: 'wrap' }}>
            <Numeral size={14} weight={600}>
              {c.broker.toUpperCase()}
            </Numeral>
            <Pill>{c.isPaper ? 'PAPER' : 'LIVE'}</Pill>
            <Pill>{c.currency}</Pill>
            {c.needsReconnect && !revoked ? (
              <Pill tone="warn" title={c.tokenExpiresAt ? `Expired ${new Date(c.tokenExpiresAt).toLocaleString()}` : undefined}>
                SESSION EXPIRED
              </Pill>
            ) : (
              <Pill tone={c.status === 'active' ? 'bull' : 'bear'}>{c.status.toUpperCase()}</Pill>
            )}
            {c.connectionSource === 'environment' ? (
              <Pill title="Auto-linked from the server's own Alpaca API keys, not a personal OAuth grant.">
                Connected via server configuration
              </Pill>
            ) : null}
          </Row>
          <span className="pg-caption" style={{ overflowWrap: 'anywhere' }}>
            {c.accountNumber ?? '—'} · connected{' '}
            <span title={new Date(c.createdAt).toLocaleString()}>{ago(c.createdAt)}</span> · last used {ago(c.lastUsedAt)}
          </span>
          {!revoked ? (
            <Row gap={8} style={{ flexWrap: 'wrap' }}>
              {c.needsReconnect ? (
                <Button
                  size="sm"
                  kind="primary"
                  onClick={() => void startConnect(c.broker, c.isPaper)}
                  disabled={starting === c.broker || isDemo}
                  title={demoTitle}
                  ariaLabel={`Reconnect ${c.broker}`}
                >
                  Reconnect
                </Button>
              ) : c.status === 'active' ? (
                <Button
                  size="sm"
                  onClick={() => void runVerify(c.id)}
                  disabled={verifying === c.id || isDemo}
                  title={demoTitle}
                  ariaLabel={`Verify the ${c.broker} connection`}
                >
                  {verifying === c.id ? 'Verifying…' : 'Verify'}
                </Button>
              ) : null}
              {!c.isPaper && c.status === 'active' ? (
                <button
                  type="button"
                  className={`pg-btn pg-btn-${c.liveTradingConsent ? 'primary' : 'secondary'} pg-btn-sm`}
                  aria-pressed={c.liveTradingConsent}
                  aria-label={`Real-money consent for ${c.broker}: ${c.liveTradingConsent ? 'on' : 'off'}`}
                  disabled={consent.isPending || isDemo}
                  title={demoTitle}
                  onClick={() => toggleConsent(c)}
                >
                  Real-money consent: {c.liveTradingConsent ? 'ON' : 'OFF'}
                </button>
              ) : null}
              <Button
                size="sm"
                onClick={() => revoke.mutate(c.id)}
                disabled={revoke.isPending || isDemo}
                title={demoTitle}
                ariaLabel={`Revoke the ${c.broker} connection`}
              >
                Revoke
              </Button>
            </Row>
          ) : null}
          {outcome ? (
            <span
              role="status"
              className={`pg-body-sm ${outcome.kind === 'ok' ? 'pg-bull' : 'pg-bear'}`}
              style={{ overflowWrap: 'anywhere' }}
            >
              {outcome.text}
            </span>
          ) : null}
          {isDemo ? (
            <span className="pg-caption">{DEMO_DISABLED_REASON}.</span>
          ) : c.connectionSource === 'environment' && !revoked ? (
            <span className="pg-caption">Will relink automatically while the server has Alpaca keys configured.</span>
          ) : null}
        </Stack>
      </div>
    );
  };

  return (
    <Card>
      <CardHead
        label="Broker connections"
        right={
          <Button size="sm" kind="ghost" onClick={() => void refetch()} ariaLabel="Refresh broker connections">
            Refresh
          </Button>
        }
      />

      {pending ? (
        <div className="pg-inset" role="status">
          <Stack gap={8}>
            <Label>Finish in the other tab</Label>
            <p className="pg-body-sm">
              Log in to {pendingName} in the tab that just opened. This page updates when you come back. If no tab
              opened,{' '}
              <a href={pending.url} target="_blank" rel="noopener noreferrer">
                open the login page
              </a>
              .
            </p>
            <Row gap={8} style={{ flexWrap: 'wrap' }}>
              <Button size="sm" kind="primary" onClick={() => void refetch()} ariaLabel="Refresh broker connections after logging in">
                Refresh
              </Button>
              <Button size="sm" kind="ghost" onClick={() => setPending(null)} ariaLabel="Stop waiting for the broker login">
                Cancel
              </Button>
            </Row>
          </Stack>
        </div>
      ) : null}
      {connectError ? (
        <span className="pg-body-sm pg-bear" role="alert" style={{ overflowWrap: 'anywhere' }}>
          {connectError}
        </span>
      ) : null}
      {consentError ? (
        <span className="pg-body-sm pg-bear" role="alert">
          {consentError}
        </span>
      ) : null}

      {connections.isLoading ? (
        <SkelRows rows={2} h={22} />
      ) : rows.length === 0 ? (
        <div className="pg-inset">
          <Label>No broker linked</Label>
          <p className="pg-body-sm" style={{ marginTop: 6 }}>
            The council can still deliberate, but nothing can execute until a broker account is connected.
          </p>
        </div>
      ) : (
        <Stack gap={10}>{rows.map(renderRow)}</Stack>
      )}

      {revokedRows.length > 0 ? (
        <>
          <Row>
            <Button
              size="sm"
              kind="ghost"
              onClick={() => setShowRevoked((v) => !v)}
              ariaLabel={showRevoked ? 'Hide revoked connections' : `Show ${revokedRows.length} revoked connections`}
            >
              {showRevoked ? 'Hide revoked' : `Show revoked (${revokedRows.length})`}
            </Button>
          </Row>
          {showRevoked ? <Stack gap={10}>{revokedRows.map(renderRow)}</Stack> : null}
        </>
      ) : null}

      <Label>Brokers</Label>
      <Stack gap={0}>
        {catalog.map((b, i) => {
          const hasFlow = CONNECTABLE.has(b.id);
          const connected = healthyBrokers.has(b.id);
          let action: React.ReactNode = null;
          let caption: string | null = null;
          if (b.status === 'planned') {
            caption = b.note ?? 'Planned.';
            action = (
              <Button size="sm" disabled ariaLabel={`Connect ${b.name} (planned)`}>
                Planned
              </Button>
            );
          } else if (b.status === 'needs_server_config') {
            caption = `Server is missing: ${b.missingConfig.join(', ')}`;
            action = (
              <Button size="sm" disabled ariaLabel={`Connect ${b.name} (server not configured)`}>
                Connect
              </Button>
            );
          } else if (connected) {
            action = <Pill tone="bull">CONNECTED</Pill>;
          } else if (hasFlow) {
            action = (
              <Button
                size="sm"
                kind="primary"
                onClick={() => void startConnect(b.id, true)}
                disabled={starting === b.id || isDemo}
                title={demoTitle}
                ariaLabel={`Connect ${b.name}${b.supportsPaper ? ' paper account' : ''}`}
              >
                {starting === b.id ? 'Opening…' : b.supportsPaper ? `Connect ${b.name} paper` : `Connect ${b.name}`}
              </Button>
            );
          } else {
            caption = b.note ?? 'No connect flow in this build yet.';
            action = (
              <Button size="sm" disabled ariaLabel={`Connect ${b.name} (not available yet)`}>
                Connect
              </Button>
            );
          }
          return (
            <Row
              key={b.id}
              gap={10}
              style={{
                padding: '10px 0',
                borderTop: i === 0 ? undefined : '1px solid var(--pg-card-border)',
                flexWrap: 'wrap',
                justifyContent: 'space-between',
              }}
            >
              <Stack gap={2} style={{ flex: '1 1 160px', minWidth: 0 }}>
                <Row gap={8} style={{ flexWrap: 'wrap' }}>
                  <Numeral size={14} weight={600}>
                    {b.name}
                  </Numeral>
                  <Pill>{b.market}</Pill>
                  <Pill>{b.currency}</Pill>
                </Row>
                {caption ? (
                  <span className="pg-caption" style={{ overflowWrap: 'anywhere' }}>
                    {caption}
                  </span>
                ) : null}
              </Stack>
              {action}
            </Row>
          );
        })}
      </Stack>
    </Card>
  );
}

function TradingModeCard({ isDemo }: { isDemo: boolean }) {
  const query = useTradingMode();
  const setMode = useSetTradingMode();
  const [confirming, setConfirming] = useState(false);
  const [phrase, setPhrase] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Endpoint missing (404) or failed: render nothing rather than something broken.
  if (!query.data) return null;
  const tm = query.data;
  const live = tm.mode === 'live';
  const canChange = tm.isAdmin && !tm.locked && !isDemo;

  const submit = (mode: 'paper' | 'live') => {
    setError(null);
    setMode.mutate(
      mode === 'live' ? { mode, confirm: phrase } : { mode },
      {
        onSuccess: () => {
          setConfirming(false);
          setPhrase('');
        },
        onError: (err) => {
          const status = (err as { status?: number } | null)?.status;
          const fallback =
            status === 403
              ? 'Only an administrator can change the trading mode.'
              : status === 409
                ? 'The server environment locks the trading mode.'
                : status === 400
                  ? 'The confirmation phrase was not accepted.'
                  : "Couldn't change the trading mode.";
          setError(apiErrorMessage(err, fallback));
        },
      },
    );
  };

  return (
    <Card>
      <CardHead label="Trading mode" right={<Pill tone={live ? 'bear' : 'bull'}>{live ? 'LIVE' : 'PAPER'}</Pill>} />
      <Row gap={12} style={{ flexWrap: 'wrap' }}>
        <Numeral size={32} weight={600} tone={live ? 'bear' : 'bull'}>
          {live ? 'LIVE' : 'PAPER'}
        </Numeral>
        <span className="pg-caption">
          {tm.updatedBy ? `Set by ${tm.updatedBy}${tm.updatedAt ? ` ${ago(tm.updatedAt)}` : ''}` : `Source: ${tm.source}`}
        </span>
      </Row>

      {tm.locked ? (
        <span className="pg-body-sm">The server environment locks the trading mode; it cannot be changed here.</span>
      ) : !tm.isAdmin ? (
        <span className="pg-body-sm">
          {live
            ? 'The desk is in live mode. Only an administrator can change this.'
            : 'The desk trades on paper accounts only. Only an administrator can change this.'}
        </span>
      ) : null}

      {tm.isAdmin ? (
        live ? (
          <Row>
            <Button
              onClick={() => submit('paper')}
              disabled={!canChange || setMode.isPending}
              title={isDemo ? DEMO_DISABLED_REASON : undefined}
              ariaLabel="Switch the trading mode back to paper"
            >
              Return to paper
            </Button>
          </Row>
        ) : confirming ? (
          <div className="pg-inset" role="group" aria-label="Confirm live trading">
            <Stack gap={10}>
              <span className="pg-body-sm">
                Live mode sends real orders with real money. Each broker connection still needs its own real-money
                consent. Auto-approve and the options agents stay paper-only.
              </span>
              <label className="pg-caption" htmlFor="live-confirm-phrase">
                Type {LIVE_CONFIRM_PHRASE} to confirm
              </label>
              <input
                id="live-confirm-phrase"
                className="pg-input"
                style={{ width: '100%', minWidth: 0 }}
                value={phrase}
                onChange={(e) => setPhrase(e.target.value)}
                autoComplete="off"
                aria-label={`Type ${LIVE_CONFIRM_PHRASE} to confirm`}
              />
              <Row gap={8} style={{ flexWrap: 'wrap' }}>
                <Button
                  kind="primary"
                  onClick={() => submit('live')}
                  disabled={phrase !== LIVE_CONFIRM_PHRASE || setMode.isPending || !canChange}
                  ariaLabel="Enable live trading"
                >
                  {setMode.isPending ? 'Enabling…' : 'Enable live trading'}
                </Button>
                <Button
                  kind="ghost"
                  onClick={() => {
                    setConfirming(false);
                    setPhrase('');
                    setError(null);
                  }}
                  ariaLabel="Cancel enabling live trading"
                >
                  Cancel
                </Button>
              </Row>
            </Stack>
          </div>
        ) : (
          <Row>
            <Button
              onClick={() => setConfirming(true)}
              disabled={!canChange}
              title={isDemo ? DEMO_DISABLED_REASON : undefined}
              ariaLabel="Switch the trading mode to live"
            >
              Switch to live…
            </Button>
          </Row>
        )
      ) : null}

      {error ? (
        <span className="pg-body-sm pg-bear" role="alert">
          {error}
        </span>
      ) : null}
    </Card>
  );
}

export function SettingsScreen() {
  const health = useHealthFull();
  const watchlist = useWatchlist();
  const addSymbol = useAddWatchlistSymbol();
  const removeSymbol = useRemoveWatchlistSymbol();
  const preference = useThemeStore((s) => s.preference);
  const setPreference = useThemeStore((s) => s.setPreference);
  const { colorScheme } = useColorScheme();
  const email = useAuthStore((s) => s.user?.email ?? '');
  const signOut = useAuthStore((s) => s.signOut);
  const isDemo = useIsDemoSession();

  const combobox = useTickerCombobox(8);
  const { query, setQuery, open, close, setOpen, activeIndex, moveActive, hits, selectIndex, selectActive, reset } =
    combobox;
  const blurTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /**
   * Commit an add attempt — either the raw typed text or a picked
   * suggestion. The query is updated to exactly what's being attempted
   * (so a failure shows the right text next to the error) and the list
   * closes immediately, matching CouncilLauncher; only a SUCCESSFUL add
   * clears the field, via `reset()` in the mutation's `onSuccess`.
   */
  const commitSymbol = (raw: string) => {
    const value = raw.trim().toUpperCase();
    if (!value) return;
    setQuery(value);
    close();
    addSymbol.mutate(value, { onSuccess: () => reset() });
  };

  /** Arrow/Enter/Escape over the suggestion list — lifted from CouncilLauncher. */
  const onTickerKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open || hits.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveActive(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveActive(-1);
    } else if (e.key === 'Enter') {
      // Enter commits the highlighted suggestion rather than the raw text.
      e.preventDefault();
      const hit = selectActive();
      if (hit) commitSymbol(hit.symbol);
    } else if (e.key === 'Escape') {
      close();
    }
  };

  return (
    <>
      <PageHead title="Settings" sub={email} right={<Pill>{colorScheme === 'light' ? 'LIGHT' : 'DARK'}</Pill>} />

      <div className="pg-grid pg-fade-up">
        <Cell span={7}>
          <BrokerConnectionsCard isDemo={isDemo} />
        </Cell>

        <Cell span={5}>
          <Stack gap={20} style={{ flex: 1 }}>
            <TradingModeCard isDemo={isDemo} />

            <Card>
              <CardHead label="Appearance" />
              <Row gap={8} style={{ flexWrap: 'wrap' }}>
                {THEMES.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    className={`pg-btn pg-btn-${preference === t.id ? 'primary' : 'secondary'} pg-btn-sm`}
                    onClick={() => setPreference(t.id)}
                    aria-pressed={preference === t.id}
                  >
                    {t.label}
                  </button>
                ))}
              </Row>
              <span className="pg-caption">
                Desktop renders the Platinum Glass system; the phone build keeps its own calmer palette.
              </span>
            </Card>

            <Card>
              <CardHead label="Session" />
              <div className="pg-inset">
                <Label>Signed in as</Label>
                <div className="pg-body-sm" style={{ marginTop: 6 }}>
                  {email}
                </div>
              </div>
              <Row>
                <Button onClick={() => void signOut()} ariaLabel="Sign out of this session">
                  Sign out
                </Button>
              </Row>
            </Card>
          </Stack>
        </Cell>

        <Cell span={7}>
          <Card>
            <CardHead label="Watchlist" right={<Pill>{(watchlist.data ?? []).length} SYMBOLS</Pill>} />
            <form
              onSubmit={(e) => {
                e.preventDefault();
                commitSymbol(query);
              }}
              style={{ display: 'flex', gap: 8 }}
            >
              <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
                <input
                  className="pg-input"
                  style={{ width: '100%' }}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onFocus={() => setOpen(true)}
                  onBlur={() => {
                    // Delay so a mousedown on a suggestion lands before the
                    // list unmounts.
                    blurTimer.current = setTimeout(() => close(), 120);
                  }}
                  onKeyDown={onTickerKeyDown}
                  placeholder="Search ticker or company"
                  aria-label="Search for a ticker or company to add to the watchlist"
                  role="combobox"
                  aria-expanded={open && hits.length > 0}
                  aria-autocomplete="list"
                  aria-controls="watchlist-symbol-suggestions"
                  autoComplete="off"
                  maxLength={40}
                />
                {open && hits.length > 0 ? (
                  <ul id="watchlist-symbol-suggestions" role="listbox" className="pg-typeahead pg-scroll">
                    {hits.map((h, i) => (
                      <li key={h.symbol} role="option" aria-selected={i === activeIndex}>
                        <button
                          type="button"
                          className={`pg-typeahead-row${i === activeIndex ? ' is-active' : ''}`}
                          onMouseDown={(e) => {
                            // mousedown, not click: fires before blur.
                            e.preventDefault();
                            if (blurTimer.current) clearTimeout(blurTimer.current);
                            const hit = selectIndex(i);
                            if (hit) commitSymbol(hit.symbol);
                          }}
                        >
                          <span className="pg-typeahead-sym">{h.symbol}</span>
                          <span className="pg-typeahead-name">{h.name}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
              <Button
                kind="primary"
                type="submit"
                disabled={addSymbol.isPending || query.trim().length === 0}
                ariaLabel="Add ticker"
              >
                {addSymbol.isPending ? 'Adding…' : 'Add'}
              </Button>
            </form>
            {addSymbol.isError ? (
              <span className="pg-body-sm pg-bear">{addSymbolErrorMessage(addSymbol.error)}</span>
            ) : null}
            {watchlist.isLoading ? (
              <SkelRows rows={2} h={30} />
            ) : (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {(watchlist.data ?? []).map((item) => (
                  <span key={item.id} className="pg-pill" style={{ padding: '6px 6px 6px 12px', gap: 8 }}>
                    {item.symbol}
                    <button
                      type="button"
                      className="pg-icon-btn"
                      style={{ width: 22, height: 22, minHeight: 22, border: 'none' }}
                      onClick={() => removeSymbol.mutate(item.symbol)}
                      aria-label={`Remove ${item.symbol} from the watchlist`}
                    >
                      <IconCross size={13} />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </Card>
        </Cell>

        <Cell span={5}>
          <Card>
            <CardHead label="System health" />
            {health.isLoading || !health.data ? (
              <SkelRows rows={5} h={18} />
            ) : (
              <Stack gap={0}>
                {(
                  [
                    ['Council', health.data.council],
                    ['Approvals', health.data.approvals],
                    ['Broker', health.data.broker],
                    ['Reconciler', health.data.reconciler],
                    ['LLM cost', health.data.llmCost],
                  ] as const
                ).map(([name, comp], i) => (
                  <Row
                    key={name}
                    gap={10}
                    style={{ padding: '10px 0', borderTop: i === 0 ? undefined : '1px solid var(--pg-card-border)' }}
                  >
                    <span
                      aria-hidden
                      style={{
                        width: 7,
                        height: 7,
                        borderRadius: 9999,
                        flex: 'none',
                        backgroundColor:
                          comp.status === 'ok'
                            ? 'var(--pg-bull)'
                            : comp.status === 'danger'
                              ? 'var(--pg-error)'
                              : comp.status === 'warning'
                                ? 'var(--pg-bear)'
                                : 'var(--pg-outline)',
                      }}
                    />
                    <Stack gap={2} style={{ flex: 1 }}>
                      <span className="label-caps" style={{ fontSize: 10, color: 'var(--pg-on-surface-variant)' }}>
                        {name}
                      </span>
                      <span className="pg-caption">{comp.label}</span>
                    </Stack>
                  </Row>
                ))}
              </Stack>
            )}
          </Card>
        </Cell>
      </div>
    </>
  );
}
