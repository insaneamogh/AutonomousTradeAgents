/**
 * `packages/ui/src/tokens.ts` is the source of truth; `tailwind.config.js`
 * mirrors it by hand so NativeWind classes resolve. A token changed in one
 * and not the other renders the wrong colour with no error, so this fails
 * the build instead.
 */
import { palette } from '@app/ui/tokens';

// eslint-disable-next-line @typescript-eslint/no-require-imports
const tailwind = require('../../tailwind.config.js') as {
  theme: { extend: { colors: Record<string, string> } };
};
const colors = tailwind.theme.extend.colors;

/** bgSurfaceElevated -> bg-surface-elevated */
const kebab = (s: string) => s.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);

describe('tokens.ts <-> tailwind.config.js', () => {
  for (const mode of ['light', 'dark'] as const) {
    it(`mirrors every ${mode} palette token`, () => {
      const wrong: string[] = [];
      for (const [name, hex] of Object.entries(palette[mode])) {
        const key = mode === 'dark' ? `${kebab(name)}-dark` : kebab(name);
        const got = colors[key];
        if (got == null) wrong.push(`${key}: missing (expected ${hex})`);
        else if (got.toLowerCase() !== hex.toLowerCase()) wrong.push(`${key}: ${got} != ${hex}`);
      }
      expect(wrong).toEqual([]);
    });
  }

  it('keeps the accent a fill and gives accent TEXT its own readable token', () => {
    // Clay as a fill is fine; as small text on ivory it would be ~3:1.
    expect(palette.light.accentText).not.toBe(palette.light.accentPrimary);
    expect(palette.light.onAccent).toBe('#1A1613');
  });
});
