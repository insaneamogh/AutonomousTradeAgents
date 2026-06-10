// Auth-route layout — no tab bar; minimal chrome.
//
// Mounted under /auth/login + /auth/verify. The root layout redirects
// here when ``useAuthStore.status`` is 'unauthenticated' and back to
// /(tabs) on successful sign-in.

import { Stack } from 'expo-router';

export default function AuthLayout() {
  return (
    <Stack
      screenOptions={{
        headerShown: false,
        animation: 'fade',
      }}
    >
      <Stack.Screen name="login" />
      <Stack.Screen name="verify" />
    </Stack>
  );
}
