/**
 * Platinum Glass app shell (§7.1).
 *
 *   sticky 64px header  ·  fixed 280px sidebar  ·  main max-w 1600 centred
 *
 * The shell owns the theme attribute (`data-pg-theme`) and the regime
 * attribute (`data-regime`) that the ambient halos key off, so every
 * screen inherits both without touching them.
 */

import type { ReactNode } from 'react';
import { useColorScheme } from 'nativewind';

import { usePendingApprovals } from '@/hooks/useApprovals';
import { useAccount } from '@/hooks/useAccount';
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
import { sectionOf, useNav } from './nav';
import type { SectionId } from './nav';

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

  return (
    <div className="pg-root" data-pg-theme={isDark ? 'dark' : 'light'} data-regime={regime}>
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '100%',
          position: 'relative',
          zIndex: 1,
        }}
      >
        <Header />
        <div className="pg-body">
          <Sidebar />
          <main className="pg-main">
            <div className="pg-main-inner">{children}</div>
          </main>
        </div>
      </div>
    </div>
  );
}

function Header() {
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme !== 'light';
  const setPreference = useThemeStore((s) => s.setPreference);
  const account = useAccount();
  const { regime, label, loading } = useRegime();

  const pnl = account.data?.todayPnlPct ?? null;
  const pnlTone = pnl == null || pnl === 0 ? 'neutral' : pnl > 0 ? 'bull' : 'bear';

  return (
    <header className="pg-header">
      <Row gap={10}>
        <Mark />
        <Stack gap={0}>
          <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: '-0.01em' }}>
            Autonomous Trader
          </span>
          <span className="label-caps" style={{ fontSize: 10 }}>
            Trading desk
          </span>
        </Stack>
      </Row>

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

function Mark() {
  return (
    <div
      aria-hidden
      style={{
        width: 32,
        height: 32,
        borderRadius: 10,
        border: '1px solid var(--pg-card-border)',
        background: 'var(--pg-inset-bg)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="var(--pg-bull)"
        strokeWidth="2"
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
      {NAV.map((item) => {
        const Icon = item.icon;
        const isActive = active === item.id;
        return (
          <button
            key={item.id}
            type="button"
            className="pg-sidebar-link"
            aria-current={isActive ? 'page' : undefined}
            onClick={() => go({ name: item.id })}
          >
            <Icon size={18} />
            {item.label}
            {item.id === 'picks' && pendingCount > 0 ? (
              <span className="pg-sidebar-badge" aria-label={`${pendingCount} pending`}>
                {pendingCount}
              </span>
            ) : null}
          </button>
        );
      })}

      <div style={{ flex: 1, minHeight: 24 }} />

      <div className="pg-inset">
        <span className="label-caps" style={{ fontSize: 10 }}>
          Signed in
        </span>
        <div className="pg-caption pg-truncate" style={{ marginTop: 4 }} title={email}>
          {email}
        </div>
      </div>
      <button
        type="button"
        className="pg-sidebar-link"
        onClick={() => void signOut()}
        aria-label="Sign out"
      >
        <IconLogout size={18} />
        Sign out
      </button>
    </nav>
  );
}
