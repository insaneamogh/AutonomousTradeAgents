/**
 * Platinum Glass app shell (§7.1).
 *
 *   sticky 64px header  ·  fixed 280px sidebar  ·  main max-w 1600 centred
 *
 * The shell owns the theme attribute (`data-pg-theme`) and the regime
 * attribute (`data-regime`) that the ambient halos key off, so every
 * screen inherits both without touching them.
 */

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useColorScheme } from 'nativewind';

import { usePendingApprovals } from '@/hooks/useApprovals';
import { useAccount } from '@/hooks/useAccount';
import { useTradingMode } from '@/hooks/useTradingMode';
import { useThemeStore } from '@/stores/themeStore';
import { useAuthStore } from '@/stores/authStore';

import {
  IconDashboard,
  IconInsights,
  IconLogout,
  IconMoon,
  IconPicks,
  IconPositions,
  IconReview,
  IconSettings,
  IconStrategies,
  IconSun,
} from './icons';
import { Pill, Row, Stack } from './primitives';
import { signedPct, usd } from './format';
import { useRegime } from './regime';
import { SECTION_LABEL, sectionOf, useNav } from './nav';
import { useIsCompact } from './viewport';
import type { SectionId } from './nav';

/** The five phone tabs. Everything else is behind the "More" sheet. */
export const TAB_IDS: SectionId[] = ['dashboard', 'picks', 'positions', 'insights', 'settings'];
export const MORE_IDS: SectionId[] = ['strategies', 'review'];

const NAV: { id: SectionId; label: string; icon: (p: { size?: number }) => ReactNode }[] = [
  { id: 'dashboard', label: 'Dashboard', icon: IconDashboard },
  { id: 'picks', label: 'Picks', icon: IconPicks },
  { id: 'positions', label: 'Positions', icon: IconPositions },
  { id: 'strategies', label: 'Strategies', icon: IconStrategies },
  { id: 'review', label: 'Review', icon: IconReview },
  { id: 'insights', label: 'Insights', icon: IconInsights },
  { id: 'settings', label: 'Settings', icon: IconSettings },
];

export function Shell({ children }: { children: ReactNode }) {
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme !== 'light';
  const { regime } = useRegime();
  const compact = useIsCompact();

  return (
    <div
      className="pg-root"
      data-pg-theme={isDark ? 'dark' : 'light'}
      data-regime={regime}
      data-compact={compact ? 'true' : undefined}
    >
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '100%',
          position: 'relative',
          zIndex: 1,
        }}
      >
        {compact ? <CompactHeader /> : <Header />}
        <div className="pg-body">
          {compact ? null : <Sidebar />}
          <main className="pg-main">
            <div className="pg-main-inner">{children}</div>
          </main>
        </div>
        {compact ? <BottomNav /> : null}
      </div>
    </div>
  );
}

function Header() {
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme !== 'light';
  const setPreference = useThemeStore((s) => s.setPreference);
  const account = useAccount();
  const tradingMode = useTradingMode();
  const { regime, label, loading } = useRegime();

  const pnl = account.data?.todayPnlPct ?? null;
  const pnlTone = pnl == null || pnl === 0 ? 'neutral' : pnl > 0 ? 'bull' : 'bear';

  return (
    <header className="pg-header">
      <Stack gap={0}>
        <span className="pg-serif" style={{ fontSize: 22, lineHeight: '26px' }}>
          Autonomous Trader
        </span>
        <span className="label-caps">Trading desk</span>
      </Stack>

      <div style={{ flex: 1 }} />

      {loading ? null : (
        <Pill tone={regime}>
          <span aria-hidden style={{ fontSize: 9 }}>
            ◆
          </span>
          {label}
        </Pill>
      )}

      {account.data ? (
        <Row gap={10}>
          <span className="pg-num" style={{ fontSize: 15, fontWeight: 500 }}>
            {usd(account.data.equity)}
          </span>
          <Pill tone={pnlTone}>{signedPct(account.data.todayPnlPct)}</Pill>
          <Pill>{account.data.isPaper ? 'PAPER' : 'LIVE'}</Pill>
          {/* The pill above is the ACCOUNT (paper or live keys). This one is
              the platform switch: when an admin has turned real trading on,
              every screen says so, not just Settings. */}
          {tradingMode.data?.mode === 'live' ? (
            <Pill tone="bear" title="An admin has switched real-money trading on.">
              LIVE TRADING ON
            </Pill>
          ) : null}
        </Row>
      ) : null}

      <button
        type="button"
        className="pg-icon-btn"
        aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
        title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
        onClick={() => setPreference(isDark ? 'light' : 'dark')}
      >
        <span aria-hidden>{isDark ? <IconSun /> : <IconMoon />}</span>
      </button>
    </header>
  );
}

/** The brand mark: a clay rounded square with a rising line. */
function Mark() {
  return (
    <div
      aria-hidden
      style={{
        width: 32,
        height: 32,
        borderRadius: 10,
        background: 'var(--pg-accent)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flex: 'none',
      }}
    >
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="var(--pg-accent-ink)"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M3 17l5-6 4 4 8-9" />
      </svg>
    </div>
  );
}

function Sidebar() {
  const { route, go } = useNav();
  const active = sectionOf(route);
  const pending = usePendingApprovals();
  const signOut = useAuthStore((s) => s.signOut);
  const email = useAuthStore((s) => s.user?.email ?? '');
  const pendingCount = pending.data?.length ?? 0;

  return (
    <nav className="pg-sidebar" aria-label="Primary">
      <div className="pg-rail-mark" aria-hidden />
      {NAV.map((item) => {
        const Icon = item.icon;
        const isActive = active === item.id;
        return (
          <button
            key={item.id}
            type="button"
            className="pg-sidebar-link"
            aria-current={isActive ? 'page' : undefined}
            aria-label={item.label}
            title={item.label}
            onClick={() => go({ name: item.id })}
          >
            <Icon size={21} />
            <span className="pg-nav-label">{item.label}</span>
            {item.id === 'picks' && pendingCount > 0 ? (
              <span className="pg-sidebar-badge" aria-label={`${pendingCount} pending`}>
                {pendingCount}
              </span>
            ) : null}
          </button>
        );
      })}

      <div style={{ flex: 1, minHeight: 24 }} />

      <button
        type="button"
        className="pg-sidebar-link"
        onClick={() => void signOut()}
        aria-label="Sign out"
        title={email ? `Sign out ${email}` : 'Sign out'}
      >
        <IconLogout size={21} />
        <span className="pg-nav-label">Sign out</span>
      </button>
    </nav>
  );
}

/* ── Phone shell (< 768px) ─────────────────────────────────────────── */

/** Title + mode pills + a More button. No equity, no regime: the Home tiles
 * carry those. */
function CompactHeader() {
  const { route } = useNav();
  const account = useAccount();
  const tradingMode = useTradingMode();
  const [moreOpen, setMoreOpen] = useState(false);

  return (
    <header className="pg-header pg-header--compact">
      <Mark />
      <h1 className="pg-compact-title pg-truncate">{SECTION_LABEL[sectionOf(route)]}</h1>
      <div style={{ flex: 1 }} />
      {account.data ? <Pill>{account.data.isPaper ? 'PAPER' : 'LIVE'}</Pill> : null}
      {tradingMode.data?.mode === 'live' ? <Pill tone="bear">LIVE ON</Pill> : null}
      <button
        type="button"
        className="pg-icon-btn"
        aria-label="More"
        aria-haspopup="dialog"
        aria-expanded={moreOpen}
        onClick={() => setMoreOpen(true)}
      >
        <span aria-hidden style={{ fontSize: 18, lineHeight: 1 }}>
          ⋯
        </span>
      </button>
      {moreOpen ? <MoreSheet onClose={() => setMoreOpen(false)} /> : null}
    </header>
  );
}

function BottomNav() {
  const { route, go } = useNav();
  const active = sectionOf(route);
  const pending = usePendingApprovals();
  const pendingCount = pending.data?.length ?? 0;

  return (
    <nav className="pg-bottom-nav" aria-label="Primary">
      {TAB_IDS.map((id) => {
        const item = NAV.find((n) => n.id === id);
        if (!item) return null;
        const Icon = item.icon;
        return (
          <button
            key={id}
            type="button"
            className="pg-bottom-tab"
            aria-current={active === id ? 'page' : undefined}
            onClick={() => go({ name: id })}
          >
            <span className="pg-bottom-tab-icon">
              <Icon size={22} />
              {id === 'picks' && pendingCount > 0 ? (
                <span className="pg-bottom-tab-badge" aria-label={`${pendingCount} pending`}>
                  {pendingCount}
                </span>
              ) : null}
            </span>
            <span>{SECTION_LABEL[id]}</span>
          </button>
        );
      })}
    </nav>
  );
}

/** Bottom sheet for everything that is not one of the five tabs. */
function MoreSheet({ onClose }: { onClose: () => void }) {
  const { go } = useNav();
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme !== 'light';
  const setPreference = useThemeStore((s) => s.setPreference);
  const signOut = useAuthStore((s) => s.signOut);
  const email = useAuthStore((s) => s.user?.email ?? '');

  useEffect(() => {
    if (typeof document === 'undefined') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="pg-sheet-wrap">
      <button type="button" className="pg-sheet-backdrop" aria-label="Close menu" onClick={onClose} />
      <div className="pg-sheet" role="dialog" aria-modal="true" aria-label="More">
        <span className="label-caps">More</span>
        {MORE_IDS.map((id) => {
          const item = NAV.find((n) => n.id === id);
          if (!item) return null;
          const Icon = item.icon;
          return (
            <button
              key={id}
              type="button"
              className="pg-sidebar-link"
              onClick={() => {
                go({ name: id });
                onClose();
              }}
            >
              <Icon size={18} />
              {item.label}
            </button>
          );
        })}
        <button
          type="button"
          className="pg-sidebar-link"
          onClick={() => setPreference(isDark ? 'light' : 'dark')}
        >
          {isDark ? <IconSun size={18} /> : <IconMoon size={18} />}
          {isDark ? 'Light theme' : 'Dark theme'}
        </button>
        <button type="button" className="pg-sidebar-link" onClick={() => void signOut()}>
          <IconLogout size={18} />
          Sign out
        </button>
        <div className="pg-caption pg-truncate" title={email} style={{ padding: '0 16px' }}>
          {email}
        </div>
      </div>
    </div>
  );
}
