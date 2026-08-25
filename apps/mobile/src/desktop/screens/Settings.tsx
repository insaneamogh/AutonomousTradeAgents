/** Settings — broker connections, watchlist, appearance, session. */

import { useState } from 'react';
import { useColorScheme } from 'nativewind';

import {
  startAlpacaOAuth,
  useBrokerConnections,
  useRevokeBrokerConnection,
} from '@/hooks/useBrokerConnections';
import { useAddWatchlistSymbol, useRemoveWatchlistSymbol, useWatchlist } from '@/hooks/useWatchlist';
import { useHealthFull } from '@/hooks/useHealthFull';
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

  const [ticker, setTicker] = useState('');
  const [connectError, setConnectError] = useState<string | null>(null);

  const connect = async () => {
    setConnectError(null);
    try {
      const res = await startAlpacaOAuth(true);
      window.location.assign(res.authorizeUrl);
    } catch {
      setConnectError('Could not start the Alpaca connection.');
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
              <table className="pg-table">
                <thead>
                  <tr>
                    <th>Broker</th>
                    <th>Account</th>
                    <th>Status</th>
                    <th className="pg-num-right">Last used</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(connections.data ?? []).map((c) => (
                    <tr key={c.id}>
                      <td>
                        <Row gap={8}>
                          <Numeral size={14} weight={600}>
                            {c.broker.toUpperCase()}
                          </Numeral>
                          <Pill>{c.isPaper ? 'PAPER' : 'LIVE'}</Pill>
                        </Row>
                      </td>
                      <td className="pg-num">{c.accountNumber ?? '—'}</td>
                      <td>
                        <Pill tone={c.status === 'active' ? 'bull' : 'bear'}>{c.status.toUpperCase()}</Pill>
                      </td>
                      <td className="pg-num-right pg-dim">{ago(c.lastUsedAt)}</td>
                      <td className="pg-num-right">
                        <Button
                          size="sm"
                          onClick={() => revoke.mutate(c.id)}
                          disabled={revoke.isPending}
                          ariaLabel={`Revoke the ${c.broker} connection`}
                        >
                          Revoke
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
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
                const value = ticker.trim().toUpperCase();
                if (!value) return;
                addSymbol.mutate(value);
                setTicker('');
              }}
              style={{ display: 'flex', gap: 8 }}
            >
              <input
                className="pg-input"
                style={{ flex: 1, minWidth: 0 }}
                value={ticker}
                onChange={(e) => setTicker(e.target.value)}
                placeholder="Add a ticker"
                aria-label="Add a ticker to the watchlist"
                maxLength={8}
              />
              <Button kind="primary" type="submit" disabled={addSymbol.isPending} ariaLabel="Add ticker">
                Add
              </Button>
            </form>
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
