/**
 * Jest config for the Expo app.
 *
 * `jest-expo` is the standard preset for Expo-managed RN projects, and its
 * major version tracks the Expo SDK number — pin it to the SDK 54 line to
 * match this app's `expo: "~54.0.0"`.
 *
 * It also auto-derives `moduleNameMapper` from this package's
 * tsconfig.json `compilerOptions.paths` (see
 * node_modules/jest-expo/src/preset/withTypescriptMapping.js), so the
 * `@/*` and `@app/*` aliases used throughout src/ and app/ resolve at test
 * time with no extra wiring here.
 */
module.exports = {
  preset: 'jest-expo',
};
