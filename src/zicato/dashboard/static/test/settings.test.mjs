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

// the models section reads /settings/models — the secret-safe per-role view:
// each role carries a call_llm path OR a {model, endpoint, api_key_env} spec
// with an api_key_env_set boolean. NO secret value is ever present.
const MODELS_VIEW = {
  models: {
    harness: { call_llm: 'pkg.harness:call_llm' },
    auxiliary: { model: 'house-model-x', endpoint: null, api_key_env: 'HOUSE_API_KEY', api_key_env_set: true },
    builder: { model: 'builder-model', endpoint: 'https://endpoint.example', api_key_env: 'BUILDER_KEY', api_key_env_set: false },
    judge: {},
  },
  roles: ['harness', 'auxiliary', 'builder', 'judge'],
  rolls_epoch: false,
};

let _lastModelsPost = null; // captures the POST /settings/models body for assertions

function jsonRes(obj) {
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => obj, text: async () => JSON.stringify(obj) };
}

function installFetch() {
  _lastModelsPost = null;
  globalThis.fetch = async (path, init) => {
    if (path === '/api/epoch') return jsonRes(EPOCH);
    if (path === '/settings/models') {
      if (init && init.method === 'POST') {
        _lastModelsPost = JSON.parse(init.body);
        // echo a refreshed secret-safe view derived from the posted block.
        const posted = _lastModelsPost.models || {};
        const echo = {};
        for (const id of ['harness', 'auxiliary', 'builder', 'judge']) {
          const s = posted[id] || {};
          echo[id] = s.call_llm ? { call_llm: s.call_llm }
            : (s.model ? { model: s.model, endpoint: s.endpoint || null, api_key_env: s.api_key_env || null, api_key_env_set: false } : {});
        }
        return jsonRes({ models: echo, roles: MODELS_VIEW.roles, rolls_epoch: false });
      }
      return jsonRes(MODELS_VIEW);
    }
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
  assert(labels.some((l) => l.includes('Models')), 'the models / LLM-endpoints section is in the rail');
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
  // CHANGE 1: the theme picker is the SHARED swatch DROPDOWN (the very same
  // component the top bar renders — dt-cd / dt-cd-option / dt-swatch-strip), NOT
  // a plain <select>. Each option carries a colour swatch strip + name.
  const dd = firstClass(body, 'dt-cd');
  assert(dd, 'the colour theme picker is the shared swatch dropdown (dt-cd)');
  assert(!firstClass(body, 'dn-set-select'), 'the old plain <select> theme picker is gone');
  const options = byClass(body, 'dt-cd-option');
  assert(options.length >= 3, 'one option per theme in the swatch dropdown');
  for (const opt of options) {
    const strip = byClass(opt, 'dt-swatch-strip')[0];
    assert(strip, 'option ' + opt.getAttribute('data-theme') + ' shows a colour swatch strip');
    assert(byClass(strip, 'dt-swatch').length >= 4, 'the strip renders representative colour swatches');
  }
  // the TYPEFACE picker is now the SHARED grouped popover (dt-tf, reusing the
  // dt-cd idiom): a trigger + 3 mode-group headers + 12 option rows. The old
  // 3-button group is gone.
  const tf = firstClass(body, 'dt-tf');
  assert(tf, 'the typeface picker is the shared grouped popover (dt-tf)');
  assert(byClass(body, 'dn-set-typebtn').length === 0, 'the old 3-button typeface group is gone');
  assertEqual(byClass(tf, 'dt-cd-group').length, 3, 'three mode-group headers in the typeface popover');
  assertEqual(byClass(tf, 'dt-tf-option').length, 12, 'twelve typeface option rows');
  assertEqual(byClass(body, 'dn-set-range').length, 2, 'page-scale + side-panel-width ranges render');

  // choosing a swatch option drives the SHARED store (applyTheme persists it to
  // the same localStorage key the top-bar dropdown reads — one source).
  const ui = await import('../js/variants/T/ui.js');
  const dracula = byClass(body, 'dt-cd-option').find((o) => o.getAttribute('data-theme') === 'dracula');
  assert(dracula, 'the dropdown has a dracula option');
  dracula.dispatchEvent(makeEvent('click'));
  assertEqual(ui.readColor(), 'dracula', 'the swatch dropdown persisted via the shared theme store');
  // the closed trigger now reflects the chosen theme name.
  assert(firstClass(body, 'dt-cd-trigger').textContent.toLowerCase().includes('dracula'),
    'the closed trigger shows the chosen theme');

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
  // picker's initial selected option — one source of truth, both directions.
  // (persistType normalises a finalized id; here we use an explicit one.)
  ui.persistType('E8');
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  const tf = firstClass(body, 'dt-tf');
  assert(tf, 'the typeface grouped popover renders in Appearance');
  const onOpt = byClass(tf, 'dt-tf-option').find((o) => o.getAttribute('aria-selected') === 'true');
  assert(onOpt && onOpt.getAttribute('data-type') === 'E8', 'the popover reflects the shared typeface store (E8 selected)');
  // a legacy stored mode id MIGRATES to its finalized default on read.
  ui.persistType('display');
  assertEqual(ui.readType(), 'D2', 'a stored legacy "display" migrates to D2 on read');
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

test('settings: the Models section renders all four roles, each editable (toggle + fields)', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'models' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  // one card per role — harness · auxiliary · builder · judge.
  const cards = byClass(body, 'dn-set-modelcard');
  assertEqual(cards.length, 4, 'all four roles render a card');
  const roleIds = cards.map((c) => c.getAttribute('data-role'));
  for (const id of ['harness', 'auxiliary', 'builder', 'judge']) {
    assert(roleIds.includes(id), 'the ' + id + ' role card renders');
  }
  // each card carries the call_llm ⟷ model-spec toggle.
  for (const c of cards) {
    assert(byClass(c, 'dn-set-typebtn').length === 2, 'each role has the form toggle');
  }
  // harness arrived as a call_llm spec ⇒ its call_llm path input is shown.
  const harness = cards.find((c) => c.getAttribute('data-role') === 'harness');
  const harnessInput = byClass(harness, 'dn-set-input').find((i) => i.getAttribute('name') === 'harness-call_llm');
  assert(harnessInput && harnessInput.getAttribute('value') === 'pkg.harness:call_llm', 'the harness call_llm path is editable and seeded');
});

test('settings: the Models section shows the api_key_env NAME + set/unset flag, never a secret', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'models' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  // auxiliary arrived as a model spec with a SET env var; builder as UNSET.
  // The NAME is surfaced in the (editable) api_key_env input value.
  const keyInputs = byClass(body, 'dn-set-input').filter((i) => (i.getAttribute('name') || '').endsWith('-api_key_env'));
  const keyVals = keyInputs.map((i) => i.getAttribute('value'));
  assert(keyVals.includes('HOUSE_API_KEY'), 'the auxiliary api_key_env NAME is shown');
  assert(keyVals.includes('BUILDER_KEY'), 'the builder api_key_env NAME is shown');
  const flags = byClass(body, 'dn-set-keyflag');
  const flagText = flags.map((f) => f.textContent);
  assert(flagText.includes('set'), 'a set indicator renders for a present env var');
  assert(flagText.includes('unset'), 'an unset indicator renders for an absent env var');
  // there is NO secret-value input or text anywhere — only env-var NAMES.
  assert(!body.textContent.toLowerCase().includes('sk-'), 'no secret-looking value is rendered');
  const inputs = byClass(body, 'dn-set-input');
  assert(!inputs.some((i) => (i.getAttribute('type') || '') === 'password'), 'no password / secret input exists');
});

test('settings: editing a Models role + saving round-trips through POST /settings/models (NAMES only)', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'models' });
  await tick();
  let body = firstClass(host, 'dn-set-body');
  // edit the judge role's model spec — type a model id + an env-var NAME.
  const judge = byClass(body, 'dn-set-modelcard').find((c) => c.getAttribute('data-role') === 'judge');
  const modelInput = byClass(judge, 'dn-set-input').find((i) => i.getAttribute('name') === 'judge-model');
  assert(modelInput, 'the judge model-spec input is present (model-spec is the default form)');
  modelInput.setAttribute('value', 'judge-model-y');
  modelInput.value = 'judge-model-y';
  modelInput.dispatchEvent(makeEvent('input'));
  await tick();
  // Save is enabled once dirty; click it.
  body = firstClass(host, 'dn-set-body');
  const save = byClass(body, 'dn-linkbtn').find((b) => b.textContent.includes('Save'));
  assert(save && !save.getAttribute('disabled'), 'the save button is enabled after an edit');
  save.dispatchEvent(makeEvent('click'));
  await tick();
  // the POST carried the edited model spec, an api_key_env NAME only, and never
  // any resolved secret value.
  assert(_lastModelsPost && _lastModelsPost.models, 'the POST carried a models block');
  assertEqual(_lastModelsPost.models.judge.model, 'judge-model-y', 'the edited judge model id round-tripped');
  assert(!('api_key_env_set' in _lastModelsPost.models.judge), 'the set/unset flag is a VIEW-only field, never posted');
  const flat = JSON.stringify(_lastModelsPost).toLowerCase();
  assert(!flat.includes('sk-'), 'no secret value crossed the POST boundary');
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
