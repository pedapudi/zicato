// test/settings.test.mjs — the Settings surface (B3).
//
// Exercises the slice the structural tests can't reach: the router resolves
// `#/settings[/<section>]` AND the `#/builder` deep-link into the settings
// view (one component, multiple entry points); the section rail renders every
// section; the Contract section reads /api/epoch as a read-only roll-up that
// links into the builder; the Builder-assistant section surfaces the
// builder.json model NAME + api_key_env NAME (never a secret) from
// /builder/config; the builder section RE-HOMES the B2 builder view (its cards
// render inside the settings host). Same harness style as builder.test.mjs.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

// ── env globals the views + builder touch (mirrors builder.test.mjs) ──
function installEnv() {
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.window = globalThis.window || {};
  globalThis.window.addEventListener = globalThis.window.addEventListener || (() => {});
  globalThis.window.removeEventListener = globalThis.window.removeEventListener || (() => {});
  if (!globalThis.window.localStorage) {
    const kv = new Map();
    globalThis.window.localStorage = {
      getItem: (k) => (kv.has(k) ? kv.get(k) : null),
      setItem: (k, v) => kv.set(k, String(v)), removeItem: (k) => kv.delete(k), clear: () => kv.clear(),
    };
  }
}
installEnv();

const EPOCH = {
  epoch_id: 'ep_demo',
  goal: 'be crisper',
  brief: 'line one\nline two\nline three',
  board: [{ entry_id: 'waffles', kind: 'single_turn' }, { entry_id: 'picky', kind: 'multi_turn_emulated' }],
  tournament: { structure: 'swiss', params: { rounds: 3 } },
  scoring: { promote_margin: 0.05, pass_rate_monotonicity: true, overfitting: { enabled: true, holdout_fraction: 0.25 } },
  proposer: { has_custom_agent: false },
};

// the assistant section reads /builder/config — a model NAME + api_key_env NAME.
const BUILDER_CONFIG = {
  chat_enabled: true,
  agent: { model: 'house-model-x', endpoint: null, api_key_env: 'HOUSE_API_KEY', call_llm: null },
  skills: ['zicato-build-tournament', 'zicato-build-board'],
};

function jsonRes(obj) {
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => obj, text: async () => JSON.stringify(obj) };
}

function installFetch() {
  globalThis.fetch = async (path, init) => {
    if (path === '/api/epoch') return jsonRes(EPOCH);
    if (path === '/builder/config') return jsonRes(BUILDER_CONFIG);
    if (path.startsWith('/builder/draft')) {
      return jsonRes({ session: 'dashboard', draft: { scoring: { tournament_structure: { structure: 'gauntlet', params: {} } }, board: [], holdout: { train_ids: [], holdout_ids: [] }, proposer: {} }, cost: { board_runs_per_round: 0, breakdown: [] }, warnings: [], diff: { changed_components: [], rolls_epoch: false } });
    }
    return jsonRes({});
  };
}

const router = await import('../js/variants/T/router.js');
const settings = await import('../js/variants/T/views/settings.js');

function byClass(root, cls) { return root.querySelectorAll('[class]').filter((n) => n.classList.contains(cls)); }
function firstClass(root, cls) { return byClass(root, cls)[0] || null; }
const ctx = { navigate() {}, href: router.href };
function tick() { return new Promise((r) => setTimeout(r, 0)); }

// ── router: one component, multiple entry points ──────────────────────

test('router: #/settings resolves to the settings view (default builder section)', () => {
  const r = router.parseRoute('#/settings');
  assertEqual(r.view, 'settings', 'view is settings');
  assertEqual(r.params.section, null, 'no explicit section ⇒ default');
});

test('router: #/settings/contract deep-links the contract section', () => {
  const r = router.parseRoute('#/settings/contract');
  assertEqual(r.view, 'settings', 'view is settings');
  assertEqual(r.params.section, 'contract', 'the contract section');
});

test('router: #/builder deep-link resolves into the settings builder section', () => {
  const r = router.parseRoute('#/builder');
  assertEqual(r.view, 'settings', 'the builder deep-link re-homes into settings');
  assertEqual(r.params.section, 'builder', 'focused on the builder section');
  // and the canonical href for the builder section is still `#/builder`.
  assertEqual(router.href('settings', { section: 'builder' }) === '#/settings/builder', true, 'settings href encodes the section');
  assertEqual(router.href('builder', {}), '#/builder', 'the builder href stays the canonical deep-link');
});

test('router: settings crumbs + up climb back to the landing then environment', () => {
  const trail = router.crumbTrail({ view: 'settings', params: { section: 'contract' } });
  assert(trail.some((c) => c.label === 'settings'), 'the settings crumb is present');
  assert(trail[trail.length - 1].label === 'contract' && trail[trail.length - 1].current, 'the leaf is the contract section');
  assertEqual(router.up({ view: 'settings', params: { section: 'contract' } }).view, 'settings', 'a section steps up to the landing');
  assertEqual(router.up({ view: 'settings', params: {} }).view, 'home', 'the landing steps up to environment');
});

// ── the settings surface ──────────────────────────────────────────────

test('settings: the section rail renders the four sections (Dashboard retired)', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'contract' });
  const items = byClass(host, 'dn-set-railitem');
  assertEqual(items.length, 4, 'four settings sections in the rail');
  const labels = items.map((i) => i.textContent);
  assert(labels.some((l) => l.includes('Tournament builder')), 'the builder section is in the rail');
  assert(labels.some((l) => l.includes('Contract')), 'the contract section is in the rail');
  assert(labels.some((l) => l.includes('Appearance')), 'the appearance section is in the rail');
  assert(!labels.some((l) => l.includes('Dashboard')), 'the Dashboard section was retired (folded into Appearance)');
});

test('settings: the Appearance section is EDITABLE and shares the top-bar theme store', async () => {
  installFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  // an editable theme <select>, typeface buttons, and page-scale / rail ranges.
  const sel = firstClass(body, 'dn-set-select');
  assert(sel && sel.localName === 'select', 'an editable colour-theme select renders');
  assert(byClass(body, 'dn-set-typebtn').length === 3, 'the three typeface buttons render');
  assertEqual(byClass(body, 'dn-set-range').length, 2, 'page-scale + side-panel-width ranges render');

  // changing the theme select drives the SHARED store (applyTheme persists it
  // to the same localStorage key the top-bar dropdown reads — one source).
  const ui = await import('../js/variants/T/ui.js');
  sel.value = 'dracula';
  sel.dispatchEvent(makeEvent('change'));
  assertEqual(ui.readColor(), 'dracula', 'the appearance select persisted via the shared theme store');

  // changing the page-scale range drives the SHARED scale store likewise.
  const ranges = byClass(body, 'dn-set-range');
  const scaleRange = ranges[0];
  scaleRange.setAttribute('value', '120');
  scaleRange.value = '120';
  scaleRange.dispatchEvent(makeEvent('input'));
  assertEqual(ui.readScale(), 120, 'the page-scale range persisted via the shared scale store');
});

test('settings: editing appearance updates the SAME store the top-bar reads (round-trip)', async () => {
  installFetch();
  globalThis.window.localStorage.clear();
  const ui = await import('../js/variants/T/ui.js');
  // a value set the "top-bar way" (persistType) is reflected by the settings
  // picker's initial selected button — one source of truth, both directions.
  ui.persistType('display');
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  const onBtn = byClass(body, 'dn-set-typebtn').find((b) => b.classList.contains('dn-set-typebtn-on'));
  assert(onBtn && onBtn.getAttribute('data-type') === 'display', 'the picker reflects the shared typeface store');
});

test('settings: the Contract section reads /api/epoch as a read-only roll-up', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'contract' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  assert(body.textContent.includes('swiss'), 'the structure is shown');
  assert(body.textContent.includes('2 entries'), 'the board entry count is shown');
  assert(body.textContent.includes('3 lines'), 'the brief line count is shown');
  assert(body.textContent.includes('0.05'), 'the promote margin is shown');
  // every contract row links INTO the builder (read-only here).
  const rows = byClass(body, 'dn-set-kvrow');
  assert(rows.length > 0 && rows.every((r) => r.getAttribute('href') === '#/builder'), 'contract rows link into the builder');
});

test('settings: the Builder-assistant section surfaces model + api_key_env NAME only (no secret)', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'assistant' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  assert(body.textContent.includes('house-model-x'), 'the model NAME is shown');
  assert(body.textContent.includes('HOUSE_API_KEY'), 'the api_key_env NAME is shown');
  assert(body.textContent.includes('enabled'), 'chat_enabled is surfaced');
  // the env-var NAME is shown, never resolved — there is no secret value path,
  // so nothing that looks like a key value can appear (we only ever pass names).
  assert(!body.textContent.toLowerCase().includes('sk-'), 'no secret-looking value is rendered');
});

test('settings: the builder section RE-HOMES the B2 builder view (its own chrome renders in the host)', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'builder' });
  await tick();
  // the re-homed builder paints its OWN self-contained chrome inside the
  // settings host: its six-section contract rail + its live preview pane. (We
  // assert the always-present chrome, not the active section's body, since the
  // shared builder module carries module-level section state across imports.)
  const railItems = byClass(host, 'dn-bld-railitem');
  assertEqual(railItems.length, 6, 'the builder kept its own six-section rail (self-contained, re-homed not rewritten)');
  assert(firstClass(host, 'dn-builder'), 'the builder root mounted inside the settings host');
  assert(firstClass(host, 'dn-bld-preview'), 'the builder live-preview pane rendered in the re-homed host');
});

await run();
