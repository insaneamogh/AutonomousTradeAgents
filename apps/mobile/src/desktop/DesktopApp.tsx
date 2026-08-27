/**
 * Desktop app root — Platinum Glass.
 *
 * Mounted ONLY from `DesktopShell` on `Platform.OS === 'web'` at
 * ≥ 1024px with an authenticated session. It replaces expo-router's
 * `<Slot />` rather than wrapping it, so no mobile screen is ever
 * mounted, styled or measured on this path — that is what keeps the
 * phone UI byte-identical.
 */

import { installPlatinumGlass } from './runtime';
import { NavProvider, useNav } from './nav';
import { Shell } from './Shell';
import { CouncilScreen } from './screens/Council';
import { DashboardScreen } from './screens/Dashboard';
import { DecisionsScreen } from './screens/Decisions';
import { InsightsScreen } from './screens/Insights';
import { PickDetailScreen } from './screens/PickDetail';
import { PicksScreen } from './screens/Picks';
import { PositionsScreen } from './screens/Positions';
import { ReviewScreen } from './screens/Review';
import { SettingsScreen } from './screens/Settings';
import { StrategiesScreen } from './screens/Strategies';

export default function DesktopApp() {
  // Idempotent and synchronous, so the first paint is already themed
  // (no flash of unstyled canvas). Safe across Fast Refresh.
  installPlatinumGlass();

  return (
    <NavProvider>
      <Shell>
        <Route />
      </Shell>
    </NavProvider>
  );
}

function Route() {
  const { route } = useNav();
  switch (route.name) {
    case 'dashboard':
      return <DashboardScreen />;
    case 'picks':
      return <PicksScreen />;
    case 'pick':
      return <PickDetailScreen id={route.id} />;
    case 'council':
      return <CouncilScreen runId={route.runId} symbol={route.symbol} />;
    case 'positions':
      return <PositionsScreen />;
    case 'strategies':
      return <StrategiesScreen />;
    case 'decisions':
      return <DecisionsScreen />;
    case 'review':
      return <ReviewScreen />;
    case 'insights':
      return <InsightsScreen />;
    case 'settings':
      return <SettingsScreen />;
    default:
      return <DashboardScreen />;
  }
}
