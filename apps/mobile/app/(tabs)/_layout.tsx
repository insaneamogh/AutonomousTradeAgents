// Tab navigator. Phase 3 adds Settings for broker OAuth + biometric toggle.
// Phase 3.5 will add: Strategies, Journal.
import { Tabs } from 'expo-router';
import {
  BarChart3,
  CheckCircle2,
  ClipboardCheck,
  House,
  Settings as SettingsIcon,
} from 'lucide-react-native';
import { useColorScheme } from 'react-native';

import { palette } from '@app/ui';

export default function TabsLayout() {
  const scheme = useColorScheme() ?? 'dark';
  const pal = palette[scheme];

  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: pal.accentPrimary,
        tabBarInactiveTintColor: pal.textTertiary,
        tabBarStyle: {
          backgroundColor: pal.bgSurface,
          borderTopColor: pal.borderSubtle,
          borderTopWidth: 1,
          elevation: 0,
        },
        tabBarLabelStyle: {
          fontSize: 11,
          fontWeight: '600',
          letterSpacing: 1.1,
          textTransform: 'uppercase',
        },
        headerStyle: { backgroundColor: pal.bgBase },
        headerTitleStyle: { color: pal.textPrimary, fontWeight: '600' },
        headerShadowVisible: false,
        sceneStyle: { backgroundColor: pal.bgBase },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: 'Home',
          tabBarIcon: ({ color, size }) => <House color={color} size={size} />,
        }}
      />
      <Tabs.Screen
        name="approvals"
        options={{
          title: 'Approve',
          tabBarIcon: ({ color, size }) => <CheckCircle2 color={color} size={size} />,
        }}
      />
      <Tabs.Screen
        name="strategies"
        options={{
          title: 'Strategies',
          tabBarIcon: ({ color, size }) => <BarChart3 color={color} size={size} />,
        }}
      />
      <Tabs.Screen
        name="review"
        options={{
          title: 'Review',
          tabBarIcon: ({ color, size }) => <ClipboardCheck color={color} size={size} />,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: 'Settings',
          tabBarIcon: ({ color, size }) => <SettingsIcon color={color} size={size} />,
        }}
      />
    </Tabs>
  );
}
