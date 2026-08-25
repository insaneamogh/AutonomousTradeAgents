/**
 * Jest config for @app/ui.
 *
 * Plain `ts-jest` rather than `jest-expo`: this package has no Expo runtime
 * dependency of its own (expo-haptics etc. are peerDependencies only, and
 * there's no app.json/babel.config.js here for jest-expo's preset to key
 * off of). ts-jest reads this package's own tsconfig.json directly, so
 * `jsx: "react-native"` and strict mode apply to tests the same way they
 * apply to the library itself.
 *
 * Note: this covers plain-function tests (see src/utils.test.ts). A future
 * test that renders an actual component (e.g. via
 * @testing-library/react-native) will additionally need react-native's own
 * jest preset to mock native modules — ts-jest alone can't parse
 * react-native's package internals.
 */
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
};
