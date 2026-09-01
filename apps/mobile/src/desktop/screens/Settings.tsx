/** Settings — broker connections, watchlist, appearance, session. */

import { useRef, useState } from 'react';
import { useColorScheme } from 'nativewind';

import {
  startAlpacaOAuth,
  useBrokerConnections,
  useRevokeBrokerConnection,
} from '@/hooks/useBrokerConnections';
import { useAddWatchlistSymbol, useRemoveWatchlistSymbol, useWatchlist } from '@/hooks/useWatchlist';
import { useHealthFull } from '@/hooks/useHealthFull';
import { useTickerCombobox } from '@/hooks/useTickerCombobox';
import { DEMO_DISABLED_REASON, useIsDemoSession } from '@/lib/demoSession';
import { useAuthStore } from '@/stores/authStore';
import { useThemeStore } from '@/stores/themeStore';
import type { ThemePreference } from '@/stores/themeStore';

import { ago } from '../format';
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

export function SettingsScreen() {
  const connections = useBrokerConnections();
  const revoke = useRevokeBrokerConnection();
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
  const [connectError, setConnectError] = useState<string | null>(null);

  const connect = async () => {
    setConnectError(null);
    try {
      const res = await startAlpacaOAuth(true, 'web');
      // oauthNotConfigured means the upcoming redirect is guaranteed to
      // fail on Alpaca's OWN page with a generic "unknown client" error
      // that gives no hint the problem is server-side config — show
      // devWarning here instead of navigating into that dead end. (Not
      // gated on devWarning being merely truthy — that field can also
      // fire for the unrelated dev-encryption-key case, which doesn't
      // block a working OAuth redirect.)
      if (res.oauthNotConfigured) {
        setConnectError(res.devWarning ?? 'Alpaca OAuth is not configured on this server.');
        return;
      }
      window.location.assign(res.authorizeUrl);
    } catch {
      setConnectError('Could not start the Alpaca connection.');
    }
  };

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
          <Card>
            <CardHead
              label="Broker connections"
              right={
                <Button size="sm" kind="primary" onClick={() => void connect()} ariaLabel="Connect an Alpaca paper account">
                  Connect Alpaca paper
                </Button>
              }
            />
            {connectError ? <span className="pg-body-sm pg-bear">{connectError}</span> : null}
            {connections.isLoading ? (
              <SkelRows rows={2} h={22} />
            ) : (connections.data ?? []).length === 0 ? (
              <div className="pg-inset">
                <Label>No broker linked</Label>
                <p className="pg-body-sm" style={{ marginTop: 6 }}>
                  The council can still deliberate, but nothing can execute until a paper account is connected.
                </p>
              </div>
            ) : (
              // Without its own scroll region, the Revoke button + its helper
              // caption (last column) can demand more width than this 7-span
              // card has at laptop widths, and the excess painted straight
              // over the next grid cell instead of ever scrolling sideways —
              // same fix, same reason, as the two tables in Positions.tsx.
              <div style={{ overflowX: 'auto' }}>
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>Broker</th>
                    <th>Account</th>
                    <th>Status</th>
                    <th className="pg-num-right">Connected</th>
                    <th className="pg-num-right">Last used</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(connections.data ?? []).map((c) => (
                    <tr key={c.id}>
                      <td>
                        {/* Wraps: three side-by-side pills on a 7-of-12 cell
                            is the widest thing in this table, and an
                            unwrapped row is what forced the whole table
                            wider than its card. */}
                        <Row gap={8} style={{ flexWrap: 'wrap' }}>
                          <Numeral size={14} weight={600}>
                            {c.broker.toUpperCase()}
                          </Numeral>
                          <Pill>{c.isPaper ? 'PAPER' : 'LIVE'}</Pill>
                          {c.connectionSource === 'environment' ? (
                            <Pill title="Auto-linked from the server's own Alpaca API keys, not a personal OAuth grant.">
                              Connected via server configuration
                            </Pill>
                          ) : null}
                        </Row>
                      </td>
                      <td className="pg-num">{c.accountNumber ?? '—'}</td>
                      <td>
                        <Pill tone={c.status === 'active' ? 'bull' : 'bear'}>{c.status.toUpperCase()}</Pill>
                      </td>
                      {/* Concrete, un-missable answer to "is this a fresh
                          account?" — a connection this old with history
                          already on it is not new just because someone
                          reconnected or flipped a consent toggle today. */}
                      <td className="pg-num-right pg-dim" title={new Date(c.createdAt).toLocaleString()}>
                        {ago(c.createdAt)}
                      </td>
                      <td className="pg-num-right pg-dim">{ago(c.lastUsedAt)}</td>
                      <td className="pg-num-right">
                        <Stack gap={4} style={{ alignItems: 'flex-end' }}>
                          <Button
                            size="sm"
                            onClick={() => revoke.mutate(c.id)}
                            disabled={revoke.isPending || isDemo}
                            title={isDemo ? DEMO_DISABLED_REASON : undefined}
                            ariaLabel={`Revoke the ${c.broker} connection`}
                          >
                            Revoke
                          </Button>
                          {isDemo ? (
                            <span className="pg-caption" style={{ textAlign: 'right', maxWidth: 168 }}>
                              {DEMO_DISABLED_REASON}.
                            </span>
                          ) : c.connectionSource === 'environment' ? (
                            <span className="pg-caption" style={{ textAlign: 'right', maxWidth: 168 }}>
                              Will relink automatically while the server has Alpaca keys configured.
                            </span>
                          ) : null}
                        </Stack>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
          </Card>
        </Cell>

        <Cell span={5}>
          <Stack gap={20} style={{ flex: 1 }}>
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
                  <ul id="watchlist-symbol-suggestions" role="listbox" className="pg-typeahead">
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
