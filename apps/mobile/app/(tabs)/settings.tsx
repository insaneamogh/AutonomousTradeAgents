// Settings — Phase 3.3.
//
// Sections (top → bottom):
//   1. Notifications  Permission + token state, toggle + "open system settings" CTA when denied.
//   2. Brokers        Alpaca OAuth + Zerodha (Kite) connect/disconnect.
//   3. Security       Biometric require-on-launch toggle.
//   4. Account        Signed-in identity + sign out.
//
// Each section is its own self-contained sub-component so the screen
// re-renders are scoped to the slice that changed.

import { useState } from 'react';
import { Linking, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Card, ErrorState, Skeleton, Toggle, cn } from '@app/ui';

import { ApiError } from '@/lib/api';
import {
  BrokerConnection,
  startAlpacaOAuth,
  startZerodhaConnect,
  useBrokerConnections,
  useRevokeBrokerConnection,
} from '@/hooks/useBrokerConnections';
import { revokeRegisteredDevice } from '@/hooks/usePushRegistration';
import { useAuthStore } from '@/stores/authStore';
import { useBiometricStore } from '@/stores/biometricStore';
import { useNotificationsStore } from '@/stores/notificationsStore';

export default function SettingsScreen() {
  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-bg-base dark:bg-bg-base-dark">
      <ScrollView contentContainerClassName="px-4 pb-32 pt-4 gap-4">
        <SectionLabel>Notifications</SectionLabel>
        <NotificationsCard />

        <SectionLabel>Brokers</SectionLabel>
        <BrokersCard />

        <SectionLabel>Security</SectionLabel>
        <SecurityCard />

        <SectionLabel>Account</SectionLabel>
        <AccountCard />
      </ScrollView>
    </SafeAreaView>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <Text className="mt-2 text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
      {children}
    </Text>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Notifications
// ─────────────────────────────────────────────────────────────────────

function NotificationsCard() {
  const permission = useNotificationsStore((s) => s.permission);
  const enabled = useNotificationsStore((s) => s.enabled);
  const setEnabled = useNotificationsStore((s) => s.setEnabled);
  const registeredDeviceId = useNotificationsStore((s) => s.registeredDeviceId);
  const lastError = useNotificationsStore((s) => s.lastError);

  const statusTone: 'gain' | 'warning' | 'neutral' = (() => {
    if (permission === 'granted' && registeredDeviceId) return 'gain';
    if (permission === 'denied' || permission === 'unsupported') return 'warning';
    return 'neutral';
  })();
  const statusLabel: string = (() => {
    if (!enabled) return 'OFF';
    if (permission === 'unsupported') return 'NOT SUPPORTED';
    if (permission === 'denied') return 'BLOCKED';
    if (permission === 'granted' && registeredDeviceId) return 'ACTIVE';
    if (permission === 'granted') return 'REGISTERING…';
    return 'AWAITING PERMISSION';
  })();

  async function onToggle(next: boolean) {
    setEnabled(next);
    if (!next) {
      await revokeRegisteredDevice();
    }
    // When next=true, the usePushRegistration hook in the root layout
    // will re-run on the state change and re-register.
  }

  return (
    <Card variant="default" className="gap-3">
      <View className="flex-row items-center justify-between">
        <View className="flex-1 gap-1 pr-3">
          <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
            Proposal alerts
          </Text>
          <Text className="text-[12px] leading-[17px] text-text-tertiary dark:text-text-tertiary-dark">
            Get a push when the agent council has a new trade for you to approve.
          </Text>
        </View>
        <Toggle
          value={enabled}
          onValueChange={onToggle}
          accessibilityLabel="Toggle proposal-pending notifications"
          testID="toggle-notifications"
        />
      </View>

      <View className="flex-row items-center gap-2">
        <View
          className={cn(
            'h-2 w-2 rounded-full',
            statusTone === 'gain' && 'bg-gain dark:bg-gain-dark',
            statusTone === 'warning' && 'bg-warning dark:bg-warning-dark',
            statusTone === 'neutral' && 'bg-text-tertiary dark:bg-text-tertiary-dark',
          )}
        />
        <Text className="text-[11px] font-semibold uppercase tracking-[1.1px] text-text-secondary dark:text-text-secondary-dark">
          {statusLabel}
        </Text>
      </View>

      {permission === 'denied' && enabled ? (
        <View className="gap-2">
          <Text className="text-[12px] leading-[17px] text-text-secondary dark:text-text-secondary-dark">
            Notifications are blocked in iOS / Android settings. Open the system settings to grant permission.
          </Text>
          <Button
            label="Open system settings"
            variant="secondary"
            onPress={() => Linking.openSettings()}
            accessibilityLabel="Open system settings to grant notification permission"
            testID="open-system-settings"
          />
        </View>
      ) : null}

      {lastError ? (
        <Text className="text-[12px] leading-[17px] text-danger dark:text-danger-dark">
          {lastError}
        </Text>
      ) : null}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Brokers — Alpaca (OAuth deep-link) + Zerodha (browser-completed).
// ─────────────────────────────────────────────────────────────────────

function detailFromError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return typeof err.body === 'object' && err.body && 'detail' in err.body
      ? String((err.body as { detail: unknown }).detail)
      : err.message;
  }
  return fallback;
}

function BrokersCard() {
  const { data, isLoading, isError, refetch } = useBrokerConnections();
  const [connectError, setConnectError] = useState<string | null>(null);
  const [pendingState, setPendingState] = useState<string | null>(null);
  const [zerodhaPending, setZerodhaPending] = useState(false);

  async function onConnect(isPaper: boolean) {
    setConnectError(null);
    try {
      const started = await startAlpacaOAuth(isPaper);
      setPendingState(started.state);
      const canOpen = await Linking.canOpenURL(started.authorizeUrl);
      if (!canOpen) {
        setConnectError("Couldn't open the broker authorization page.");
        setPendingState(null);
        return;
      }
      await Linking.openURL(started.authorizeUrl);
    } catch (err) {
      setConnectError(detailFromError(err, "Couldn't start the OAuth flow."));
      setPendingState(null);
    }
  }

  async function onConnectZerodha() {
    setConnectError(null);
    try {
      const started = await startZerodhaConnect();
      setZerodhaPending(true);
      // Kite completes the connection on the API's redirect page — the app
      // just opens the login URL and refreshes the list when the user is back.
      await Linking.openURL(started.loginUrl);
    } catch (err) {
      setConnectError(detailFromError(err, "Couldn't start the Zerodha connect flow."));
      setZerodhaPending(false);
    }
  }

  if (isLoading) {
    return (
      <Card variant="default" className="gap-3">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-11 w-full" />
      </Card>
    );
  }

  if (isError) {
    return (
      <Card variant="default">
        <ErrorState
          title="Couldn't load brokers"
          description="The agent server isn't reachable. Try again in a moment."
          onRetry={() => refetch()}
        />
      </Card>
    );
  }

  const activeAlpaca = (data ?? []).find(
    (c) => c.broker === 'alpaca' && c.status === 'active',
  );
  const activeZerodha = (data ?? []).find(
    (c) => c.broker === 'zerodha' && c.status === 'active',
  );

  return (
    <View className="gap-3">
      {activeAlpaca ? (
        <ConnectedBrokerCard connection={activeAlpaca} />
      ) : (
        <Card variant="default" className="gap-3">
          <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
            Connect Alpaca
          </Text>
          <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
            Link your Alpaca account so the agent can read positions and (with your approval per
            trade) place orders. Paper-only in Phase 3 — live is gated on the paper-validation phase.
          </Text>
          <Button
            label={pendingState ? 'Waiting for browser…' : 'Connect Alpaca paper'}
            onPress={() => onConnect(true)}
            disabled={Boolean(pendingState)}
            fullWidth
            accessibilityLabel="Connect Alpaca paper account"
            testID="connect-alpaca-paper"
          />
        </Card>
      )}

      {activeZerodha ? (
        <ConnectedBrokerCard connection={activeZerodha} />
      ) : (
        <Card variant="default" className="gap-3">
          <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
            Connect Zerodha
          </Text>
          <Text className="text-[13px] leading-[19px] text-text-secondary dark:text-text-secondary-dark">
            Log in at Kite in the browser; the connection completes there. Kite tokens expire
            daily around 6:00 IST, so you reconnect each trading day. Live account — orders stay
            blocked until live trading is enabled on the server.
          </Text>
          <Button
            label={zerodhaPending ? 'Finish login in browser, then refresh' : 'Connect Zerodha'}
            onPress={onConnectZerodha}
            disabled={zerodhaPending}
            fullWidth
            accessibilityLabel="Connect Zerodha Kite account"
            testID="connect-zerodha"
          />
          {zerodhaPending ? (
            <Button
              label="I've logged in — refresh"
              variant="secondary"
              onPress={() => {
                setZerodhaPending(false);
                refetch();
              }}
              fullWidth
              accessibilityLabel="Refresh broker connections after Zerodha login"
              testID="refresh-after-zerodha"
            />
          ) : null}
        </Card>
      )}

      {connectError ? (
        <Text className="text-[13px] leading-[19px] text-danger dark:text-danger-dark">
          {connectError}
        </Text>
      ) : null}

      {(data ?? []).filter((c) => c.status === 'revoked').map((c) => (
        <RevokedBrokerCard key={c.id} connection={c} />
      ))}
    </View>
  );
}

function brokerLabel(connection: BrokerConnection): string {
  const name = connection.broker === 'zerodha' ? 'Zerodha' : 'Alpaca';
  return `${name} ${connection.isPaper ? 'paper' : 'live'}`;
}

function ConnectedBrokerCard({ connection }: { connection: BrokerConnection }) {
  const revoke = useRevokeBrokerConnection();

  return (
    <Card variant="default" className="gap-3">
      <View className="flex-row items-center justify-between">
        <View className="flex-1 gap-1">
          <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
            {brokerLabel(connection)}
          </Text>
          {connection.accountNumber ? (
            <Text
              className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark"
              style={{ fontVariant: ['tabular-nums'] }}
            >
              {connection.accountNumber}
            </Text>
          ) : null}
        </View>
        <View className="h-2 w-2 rounded-full bg-gain dark:bg-gain-dark" />
      </View>
      <Button
        label={revoke.isPending ? 'Disconnecting…' : 'Disconnect'}
        onPress={() => revoke.mutate(connection.id)}
        variant="secondary"
        disabled={revoke.isPending}
        accessibilityLabel={`Disconnect ${brokerLabel(connection)}`}
        testID={`disconnect-${connection.broker}`}
      />
    </Card>
  );
}

function RevokedBrokerCard({ connection }: { connection: BrokerConnection }) {
  return (
    <Card variant="inset" className="gap-1">
      <Text className="text-[13px] font-semibold text-text-secondary dark:text-text-secondary-dark">
        {brokerLabel(connection)} — revoked
      </Text>
      <Text className="text-[12px] text-text-tertiary dark:text-text-tertiary-dark">
        Reconnect anytime above.
      </Text>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Security — biometric toggle
// ─────────────────────────────────────────────────────────────────────

function SecurityCard() {
  const requireOnLaunch = useBiometricStore((s) => s.requireOnLaunch);
  const setRequireOnLaunch = useBiometricStore((s) => s.setRequireOnLaunch);
  const [confirmingOff, setConfirmingOff] = useState(false);

  function onToggle(next: boolean) {
    if (!next) {
      // Disabling biometric is sensitive — PLAN.md §3 requires an
      // explicit confirmation. We expand the card to ask first.
      setConfirmingOff(true);
      return;
    }
    setRequireOnLaunch(true);
    setConfirmingOff(false);
  }

  function confirmDisable() {
    setRequireOnLaunch(false);
    setConfirmingOff(false);
  }

  return (
    <Card variant="default" className="gap-3">
      <View className="flex-row items-center justify-between">
        <View className="flex-1 gap-1 pr-3">
          <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
            Require Face ID on launch
          </Text>
          <Text className="text-[12px] leading-[17px] text-text-tertiary dark:text-text-tertiary-dark">
            Re-locks when you background the app — recommended for any device that holds broker access.
          </Text>
        </View>
        <Toggle
          value={requireOnLaunch}
          onValueChange={onToggle}
          accessibilityLabel="Require biometric unlock on launch"
          testID="toggle-biometric"
        />
      </View>

      {confirmingOff ? (
        <View className="gap-2 rounded-md bg-warning/10 px-3 py-3 dark:bg-warning-dark/10">
          <Text className="text-[12px] font-semibold uppercase tracking-[1.1px] text-warning dark:text-warning-dark">
            Disable biometric?
          </Text>
          <Text className="text-[12px] leading-[17px] text-text-secondary dark:text-text-secondary-dark">
            Anyone who can unlock your phone will be able to see open positions and approve trades. The
            session refresh token is still encrypted at rest, but the in-app gate is gone.
          </Text>
          <View className="flex-row gap-2">
            <View className="flex-1">
              <Button
                label="Keep enabled"
                variant="secondary"
                onPress={() => setConfirmingOff(false)}
                fullWidth
                accessibilityLabel="Keep biometric enabled"
              />
            </View>
            <View className="flex-1">
              <Button
                label="Disable"
                variant="destructive"
                onPress={confirmDisable}
                fullWidth
                accessibilityLabel="Confirm disabling biometric"
                testID="confirm-biometric-off"
              />
            </View>
          </View>
        </View>
      ) : null}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Account
// ─────────────────────────────────────────────────────────────────────

function AccountCard() {
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [signingOut, setSigningOut] = useState(false);

  if (!user) return null;

  return (
    <Card variant="default" className="gap-3">
      <View className="gap-1">
        <Text className="text-[11px] font-semibold uppercase tracking-[1.2px] text-text-secondary dark:text-text-secondary-dark">
          Signed in as
        </Text>
        <Text className="text-[15px] font-semibold text-text-primary dark:text-text-primary-dark">
          {user.email}
        </Text>
      </View>
      <Button
        label={signingOut ? 'Signing out…' : 'Sign out'}
        onPress={async () => {
          setSigningOut(true);
          try {
            await signOut();
          } finally {
            setSigningOut(false);
          }
        }}
        variant="destructive"
        disabled={signingOut}
        fullWidth
        accessibilityLabel="Sign out"
        testID="sign-out"
      />
    </Card>
  );
}
