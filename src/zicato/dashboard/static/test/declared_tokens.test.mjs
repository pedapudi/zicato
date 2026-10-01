// test/declared_tokens.test.mjs — every theme token the stylesheet reads exists.
//
// A `var(--name)` with no fallback resolves to nothing when no rule declares
// `--name`, and the property falls back to its initial value: a background
// becomes transparent, a colour inherits. The settings drawer's panel once
// read such a token, so the page behind the drawer showed through its header
// and sections. Every custom property the stylesheet reads without a fallback
// must therefore be declared in the stylesheet or set from script.

import { installDom, test, run, assertEqual } from './harness.mjs';

installDom();

const fs = await import('node:fs');
const path = await import('node:path');
const { readCss } = await import('./fixtures.mjs');

const CSS = readCss().replace(/\/\*[\s\S]*?\*\//g, '');

test('every custom property the stylesheet reads without a fallback is declared', () => {
  const declared = new Set([...CSS.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
  // a few properties are set from script (the rail width, the depth indent).
  const jsDir = new URL('../js/', import.meta.url).pathname;
  const walk = (d) => fs.readdirSync(d).flatMap((n) => {
    const p = path.join(d, n);
    return fs.statSync(p).isDirectory() ? walk(p) : (n.endsWith('.js') ? [p] : []);
  });
  for (const f of walk(jsDir)) {
    const src = fs.readFileSync(f, 'utf8');
    for (const m of src.matchAll(/(--[\w-]+)['"]?\s*[:,]/g)) declared.add(m[1]);
  }
  const missing = [...new Set([...CSS.matchAll(/var\(\s*(--[\w-]+)\s*\)/g)].map((m) => m[1]))]
    .filter((v) => !declared.has(v));
  assertEqual(missing.join(', '), '', 'no var() without a fallback names an undeclared property');
});

await run();
