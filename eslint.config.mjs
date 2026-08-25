// Flat ESLint config (ESLint 9+) for the JS/TS side of the monorepo:
// apps/mobile, packages/ui, packages/shared-types.
//
// Why not airbnb-typescript: `eslint-config-airbnb-typescript` (last
// published 2024-03, on top of `eslint-config-airbnb` which hasn't shipped
// since 2021-12) has never gained flat-config support and predates ESLint 9's
// flat-config-only requirement entirely. We use `typescript-eslint`'s own
// type-checked "recommended" config as the backbone instead, layered with
// React / React Hooks / React Native / jsx-a11y flat presets. See
// fable5findings.md build log for the full writeup.
//
// The Python side (apps/api, apps/agents, packages/engine, packages/broker)
// is intentionally out of scope — see the ignores below.

import path from 'node:path';
import { fileURLToPath } from 'node:url';

import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactNative from 'eslint-plugin-react-native';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import globals from 'globals';
import prettierConfig from 'eslint-config-prettier';

// Avoid `import.meta.dirname` (Node 20.11+ only) — the repo's declared
// engines.node is ">=20.0.0", so resolve the old-fashioned way instead.
const __dirname = path.dirname(fileURLToPath(import.meta.url));

// TypeScript files that live inside a package with its own tsconfig.json.
// Kept in sync with pnpm-workspace.yaml's package list.
const TS_PROJECT_FILES = [
  'apps/mobile/**/*.{ts,tsx}',
  'packages/ui/**/*.{ts,tsx}',
  'packages/shared-types/**/*.ts',
];

// The subset of the above that is React / JSX UI code (as opposed to
// shared-types, which is plain type declarations with no JSX at all).
const UI_FILES = ['apps/mobile/**/*.{ts,tsx}', 'packages/ui/**/*.{ts,tsx}'];

// `src/desktop/**` is a deliberately separate, web-only DOM tree (plain
// <div>/<span>/<button>, styled via CSSProperties — see the file header in
// primitives.tsx) that DesktopShell.tsx swaps in for wide-web sessions
// instead of the React Native tree. The `react-native/*` rules assume RN's
// View/Text/StyleSheet model, which this subtree deliberately doesn't use,
// so it gets ordinary React/JSX linting but not the RN-specific plugin.
const DESKTOP_WEB_DIR = 'apps/mobile/src/desktop/**';

// Custom components that wrap RN's <Text> internally (see
// src/components/bento.tsx) and are used with text children at call
// sites, e.g. `<TileLabel>Open positions</TileLabel>`. `no-raw-text` only
// recognizes literal Text/TSpan/StyledText/Animated.Text tag names by
// default — this list extends that recognition to our own wrappers so the
// rule keeps catching genuinely unwrapped text instead of flagging every
// use of a typography primitive.
const RAW_TEXT_SKIP_COMPONENTS = [
  'TileLabel',
  'TileValue',
  'HeroHeadline',
  'HeroSub',
  'SectionLabel', // local to app/(tabs)/settings.tsx
];

export default tseslint.config(
  // ── Global ignores ────────────────────────────────────────────────────
  {
    ignores: [
      '**/node_modules/**',
      '**/dist/**',
      '**/.expo/**',
      '**/.turbo/**',
      '**/coverage/**',
      '**/*.d.ts',
      // Python side — out of scope for v1, other agents may be editing it.
      'apps/api/**',
      'apps/agents/**',
      'packages/engine/**',
      'packages/broker/**',
      'infra/**',
      'scripts/**',
    ],
  },

  // ── Baseline JS correctness rules for every lintable file ─────────────
  js.configs.recommended,

  // ── Plain Node config scripts (babel/metro/tailwind/jest/this file) ──
  {
    files: ['**/*.config.{js,cjs,mjs}'],
    languageOptions: {
      globals: { ...globals.node },
    },
  },

  // ── TypeScript, type-checked, across all three packages ──────────────
  {
    files: TS_PROJECT_FILES,
    extends: [tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parserOptions: {
        // Auto-discovers the nearest tsconfig.json per file — the
        // monorepo-friendly replacement for a manual `project: [...]` array.
        projectService: true,
        tsconfigRootDir: __dirname,
      },
      globals: { ...globals.es2022 },
    },
    rules: {
      // Hand-picked, in the spirit of airbnb-typescript rather than a
      // rule-for-rule port of it (see header comment).
      eqeqeq: ['error', 'always', { null: 'ignore' }],
      'no-var': 'error',
      'prefer-const': 'error',
      'no-console': 'warn',
      '@typescript-eslint/consistent-type-imports': ['warn', { prefer: 'type-imports' }],
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },

  // ── React / Hooks / a11y — every UI file, RN and desktop-web alike ────
  {
    files: UI_FILES,
    plugins: {
      react,
      'react-hooks': reactHooks,
      'jsx-a11y': jsxA11y,
    },
    languageOptions: {
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: {
      react: { version: 'detect' },
    },
    rules: {
      ...react.configs.flat.recommended.rules,
      ...react.configs.flat['jsx-runtime'].rules, // new JSX transform — no `import React`
      ...reactHooks.configs.flat.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,

      // TS types are the source of truth here, not React.PropTypes.
      'react/prop-types': 'off',
      'react/require-default-props': 'off',

      // eslint-plugin-react-hooks v7 folded the full "React Compiler
      // readiness" rule set into `recommended` (immutability, purity, ref
      // access timing, compiler config/gating, etc). This repo does not use
      // the React Compiler — it's not on CLAUDE.md's locked stack — and in
      // a real run several of these fired on ordinary, correct React
      // Native code (e.g. `useRef(new Animated.Value(x)).current` for a
      // lazily-created stable value is idiomatic RN, not a bug; flagged by
      // `refs`). Keep the rules that catch real runtime mistakes regardless
      // of the compiler; drop the compiler-adoption-only ones so real
      // findings don't drown in noise.
      'react-hooks/immutability': 'off',
      'react-hooks/purity': 'off',
      'react-hooks/refs': 'off',
      'react-hooks/static-components': 'off',
      'react-hooks/use-memo': 'off',
      'react-hooks/preserve-manual-memoization': 'off',
      'react-hooks/incompatible-library': 'off',
      'react-hooks/globals': 'off',
      'react-hooks/config': 'off',
      'react-hooks/gating': 'off',

      // `onPress`/`onClick`={async () => ...} is the idiomatic pattern for
      // an event handler typed to return void — don't flag it.
      '@typescript-eslint/no-misused-promises': [
        'error',
        { checksVoidReturn: { attributes: false } },
      ],
    },
  },

  // ── React Native plugin — RN view model only, not desktop-web DOM ────
  {
    files: UI_FILES,
    ignores: [DESKTOP_WEB_DIR],
    plugins: {
      'react-native': reactNative,
    },
    languageOptions: {
      globals: { ...globals['react-native'] },
    },
    rules: {
      // eslint-plugin-react-native ships no flat preset yet (still
      // eslintrc-shaped as of 5.0.0), so the rules we want are hand-picked
      // rather than spread from `configs.all`.
      'react-native/no-raw-text': ['error', { skip: RAW_TEXT_SKIP_COMPONENTS }],
      'react-native/no-unused-styles': 'error',
      'react-native/no-single-element-style-arrays': 'error',
      'react-native/split-platform-components': 'error',
      // Direct enforcement of CLAUDE.md/DESIGN.md: components use design
      // tokens only, never a raw hex literal.
      'react-native/no-color-literals': 'error',
      'react-native/no-inline-styles': 'warn',
    },
  },

  // ── Test files ──────────────────────────────────────────────────────
  {
    files: ['**/*.test.{ts,tsx}', '**/__tests__/**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.jest },
    },
  },

  // ── Prettier last: turn off every rule that fights the formatter ──────
  prettierConfig,
);
