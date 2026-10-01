// test/layout_fit.test.mjs — two layout rules checked against the stylesheet.
//
//   1. A fleet card on the environment page keeps every stat inside its
//      border: the stat row wraps onto a second row, and each stat keeps its
//      key and its value on one line and never shrinks. Before this rule
//      "cost/promo 14ms" ran past the card at 1440 pixels and
//      "promo rate 2/3 · 67%" broke onto several lines.
//   2. The settings drawer covers the top bar. It stacks above the bar and
//      every popover in it, and its panel paints an opaque background, so no
//      top-bar control shows through the drawer's header.
//
// The harness DOM has no layout engine, so the rules are read from
// css/console.css. The pull request records the same checks measured in a
// browser at 1440 and 390 pixels.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { readCss, allByClass, freshState, installFetch, mountLiveShell } = await import('./fixtures.mjs');

const CSS = readCss().replace(/\/\*[\s\S]*?\*\//g, '');

// The declarations of the rules whose selector list holds `selector` exactly.
function decls(selector) {
  const out = {};
  for (const m of CSS.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sels = m[1].split(';').pop().split(',').map((s) => s.trim());
    if (!sels.includes(selector)) continue;
    for (const d of m[2].split(';')) {
      const i = d.indexOf(':');
      if (i > 0) out[d.slice(0, i).trim()] = d.slice(i + 1).trim().replace(/\s+/g, ' ');
    }
  }
  return out;
}

test('a fleet card\'s stat row wraps, and each stat holds one line and never shrinks', () => {
  const row = decls('.dn-fleet-stats');
  assertEqual(row.display, 'flex', 'the stats sit in a flex row');
  assertEqual(row['flex-wrap'], 'wrap', 'which wraps onto a second row when the card is narrow');
  assertEqual(decls('.dn-mini').flex, 'none', 'a stat keeps its width');
  assertEqual(decls('.dn-mini-k')['white-space'], 'nowrap', 'a stat\'s key holds one line');
  assertEqual(decls('.dn-mini-v')['white-space'], 'nowrap', 'a stat\'s value holds one line');
});

test('the rendered fleet card puts its stats in that row', async () => {
  freshState(); installFetch();
  const root = mountLiveShell('#/');
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  const card = allByClass(root, 'dn-fleet-card')[0];
  assert(card, 'the environment page renders a fleet card');
  const row = allByClass(card, 'dn-fleet-stats')[0];
  const stats = allByClass(row, 'dn-mini');
  assert(stats.length >= 3, 'the card shows its stats');
  for (const s of stats) {
    assert(allByClass(s, 'dn-mini-k')[0] && allByClass(s, 'dn-mini-v')[0], 'each stat has a key and a value');
  }
});

test('the settings drawer stacks above the top bar and every popover in it', () => {
  const z = (sel) => Number(decls(sel)['z-index']);
  const drawer = z('.dt-drawer');
  assert(drawer > z('.dt-topbar'), 'the drawer is above the top bar');
  for (const sel of ['.dt-cd-list', '.dt-tf .dt-tf-pop']) {
    assert(drawer > z(sel), 'the drawer is above ' + sel);
  }
});

test('the settings drawer panel paints an opaque background in every theme', () => {
  const bg = decls('.dt-drawer-panel').background;
  const token = (bg.match(/^var\((--[\w-]+)\)$/) || [])[1];
  assert(token, 'the panel background is one theme token (' + bg + ')');
  const values = [...CSS.matchAll(new RegExp(token + '\\s*:\\s*([^;]+);', 'g'))].map((m) => m[1].trim());
  assert(values.length >= 3, token + ' is declared by the themes');
  for (const v of values) {
    assert(/^#[0-9a-fA-F]{6}$/.test(v), token + ' is an opaque colour in every theme (' + v + ')');
  }
});

await run();
