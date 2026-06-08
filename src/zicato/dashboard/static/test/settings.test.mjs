// test/settings.test.mjs — the Settings surface.
//
// Exercises the slice the structural tests can't reach: the router resolves
// `#/settings[/<section>]` to the settings view, and `#/builder` to the
// STANDALONE tournament-builder view (the builder was promoted out of
// Settings); the section rail renders the in-host sections PLUS a launcher link
// out to `#/builder`; the Contract section reads /api/epoch as a read-only
// roll-up that links into the builder; the Models section surfaces the
// per-role model NAME + api_key_env NAME (never a secret); the builder is NO
// LONGER embedded inside the settings host (only the launcher remains). Same
// harness style as builder.test.mjs.

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

test('router: #/settings resolves to the settings view (default contract section)', () => {
  const r = router.parseRoute('#/settings');
  assertEqual(r.view, 'settings', 'view is settings');
  assertEqual(r.params.section, null, 'no explicit section ⇒ default');
  // the builder is no longer the default Settings section — the router exports
  // the new default so the view + the back/up target agree on it.
  assertEqual(router.DEFAULT_SETTINGS_SECTION, 'contract', 'the default Settings section is now the contract roll-up');
});

test('router: #/settings/models deep-links the models section', () => {
  const r = router.parseRoute('#/settings/models');
  assertEqual(r.view, 'settings', 'view is settings');
  assertEqual(r.params.section, 'models', 'the models section');
});

test('router: #/builder resolves to the STANDALONE builder view (no longer into settings)', () => {
  const r = router.parseRoute('#/builder');
  assertEqual(r.view, 'builder', 'the builder is its own first-class view');
  assert(!r.params.section, 'it is NOT a settings section any more (no section param)');
  // the canonical href for the builder view is `#/builder`.
  assertEqual(router.href('builder', {}), '#/builder', 'the builder href is the canonical standalone link');
  // its breadcrumb reads environment › tournament builder, and it steps up to
  // environment (the un-nesting: it is a top-level view, not a settings child).
  const trail = router.crumbTrail({ view: 'builder', params: {} });
  assertEqual(trail[0].label, 'environment', 'crumb root is environment');
  assert(trail[trail.length - 1].label === 'tournament builder' && trail[trail.length - 1].current, 'the leaf is the tournament builder');
  assert(!trail.some((c) => c.label === 'settings'), 'the builder crumb does NOT pass through settings');
  assertEqual(router.up({ view: 'builder', params: {} }).view, 'home', 'the builder view steps up to environment');
});

test('router: settings crumbs + up climb back to the landing then environment', () => {
  const trail = router.crumbTrail({ view: 'settings', params: { section: 'models' } });
  assert(trail.some((c) => c.label === 'settings'), 'the settings crumb is present');
  assert(trail[trail.length - 1].label === 'models / llm endpoints' && trail[trail.length - 1].current, 'the leaf is the models section');
  assert(!trail.some((c) => c.label === 'tournament builder'), 'no builder crumb inside settings (it is its own view)');
  assertEqual(router.up({ view: 'settings', params: { section: 'models' } }).view, 'settings', 'a non-default section steps up to the landing');
  // the DEFAULT section (contract) steps straight up to environment, like the bare landing.
  assertEqual(router.up({ view: 'settings', params: { section: 'contract' } }).view, 'home', 'the default section steps up to environment');
  assertEqual(router.up({ view: 'settings', params: {} }).view, 'home', 'the landing steps up to environment');
});

// ── the settings surface ──────────────────────────────────────────────

test('settings: the rail renders three in-host sections + a builder LAUNCHER (Dashboard retired)', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'contract' });
  const items = byClass(host, 'dn-set-railitem');
  // three in-host sections (contract / models / appearance) + the launcher.
  assertEqual(items.length, 4, 'three in-host sections + one launcher entry in the rail');
  const labels = items.map((i) => i.textContent);
  assert(labels.some((l) => l.includes('Contract')), 'the contract section is in the rail');
  assert(labels.some((l) => l.includes('Models')), 'the models / LLM-endpoints section is in the rail');
  assert(labels.some((l) => l.includes('Appearance')), 'the appearance section is in the rail');
  assert(!labels.some((l) => l.includes('Dashboard')), 'the Dashboard section was retired (folded into Appearance)');
  // the builder is a LAUNCHER (a link OUT to the standalone view), NOT an
  // in-host section: its rail entry carries the launcher class + the `#/builder`
  // href, and there is exactly one of it.
  const launchers = byClass(host, 'dn-set-raillauncher');
  assertEqual(launchers.length, 1, 'exactly one builder launcher entry');
  assert(launchers[0].textContent.includes('Tournament builder'), 'the launcher is labelled Tournament builder');
  assertEqual(launchers[0].getAttribute('href'), '#/builder', 'the launcher navigates to the standalone builder view');
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

// The typeface picker now lives ONLY in Settings (removed from the top bar).
// Choosing a face there must still APPLY live (stamp the app root via the shared
// applyTypeface store) AND PERSIST — so this is the sole live picker now.
test('settings: the Settings typeface picker still APPLIES + PERSISTS (the sole live picker)', async () => {
  installFetch();
  globalThis.window.localStorage.clear();
  const ui = await import('../js/variants/T/ui.js');
  // a stable app root the picker's applyTypeface can stamp (applyTypeface falls
  // back to the shell's `_root` when no root is passed — mountShell sets it, but
  // here we drive the picker directly and assert via the persisted store).
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  const tf = firstClass(body, 'dt-tf');
  assert(tf, 'the typeface grouped popover renders in Settings → Appearance');
  // pick the T12 option — it must apply via the shared store and persist.
  const t12 = byClass(tf, 'dt-tf-option').find((o) => o.getAttribute('data-type') === 'T12');
  assert(t12, 'the popover has the T12 option');
  t12.dispatchEvent(makeEvent('click'));
  assertEqual(ui.readType(), 'T12', 'choosing T12 in Settings persisted via the shared typeface store');
  // and the closed trigger now reflects the chosen face (the live picker synced).
  const trigger = firstClass(tf, 'dt-cd-trigger');
  assert(trigger, 'the typeface popover has a closed trigger');
  assert((trigger.textContent || '').length > 0, 'the trigger shows the chosen face label');
});

// The old accent-tinted, pulsing "light-up rail card" research-preview banner is
// GONE from the Settings surface. The product-status mark is now a QUIET pill
// pinned NEXT TO the wordmark in the top bar (asserted in variant_t.test.mjs),
// NOT a card that leads Settings — so the Settings host must carry no `dn-respreview`
// banner and must lead directly with the section grid.
test('settings: the old research-preview light-up card is gone from Settings', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  assert(!firstClass(host, 'dn-respreview'), 'no Settings light-up research-preview banner');
  // the Settings surface now LEADS with the section grid (no card above it).
  assert(host.firstChild && host.firstChild.classList.contains('dn-settings'),
    'the Settings surface leads directly with the section grid');
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

test('settings: the builder is NO LONGER embedded in the host — it is a LAUNCHER to #/builder', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  // even if a stale `#/builder` ever resolved into a settings section, the
  // surface must not render the builder's chrome inside its host (that nesting
  // was the clutter we removed). The launcher links out instead.
  await settings.render(host, ctx, { section: 'builder' });
  await tick();
  // the builder's self-contained chrome must NOT appear inside the settings host.
  assert(!firstClass(host, 'dn-builder'), 'the builder root is NOT mounted inside the settings host');
  assert(!firstClass(host, 'dn-bld-preview'), 'no embedded builder preview pane in the settings host');
  assertEqual(byClass(host, 'dn-bld-railitem').length, 0, 'no embedded builder rail inside settings');
  // a non-section `section: 'builder'` param falls back to the default section;
  // the launcher to the standalone view is still present.
  const launchers = byClass(host, 'dn-set-raillauncher');
  assertEqual(launchers.length, 1, 'the builder launcher is present');
  assertEqual(launchers[0].getAttribute('href'), '#/builder', 'the launcher targets the standalone builder view');
});

// ── S/M/L FONT-SIZE control (text-only multiplier in the typeface picker) ──
//
// DISTINCT from the page-scale pill (whole-page `zoom`): this scales the HTML
// text via `--dt-font-scale` WITHOUT touching the SVG figures. The ladder
// starts above the raw literal-px baseline (too small for low-x-height faces):
// small 1.15 (the default floor), medium 1.3, large 1.45.

test('ui: font-size model — normalise + read/persist round-trip + scale values', async () => {
  const ui = await import('../js/variants/T/ui.js');
  globalThis.window.localStorage.clear();
  // default is small; unknown / nullish values normalise to small.
  assertEqual(ui.DEFAULT_FONTSIZE, 'small', 'the default font size is small');
  assertEqual(ui.normaliseFontSize('small'), 'small', 'small is a known size');
  assertEqual(ui.normaliseFontSize('medium'), 'medium', 'medium is a known size');
  assertEqual(ui.normaliseFontSize('large'), 'large', 'large is a known size');
  assertEqual(ui.normaliseFontSize('xl'), 'small', 'an unknown size falls back to small');
  assertEqual(ui.normaliseFontSize(null), 'small', 'null falls back to small');
  // scale numbers: small 1.15 (default floor), medium 1.3, large 1.45.
  assertEqual(ui.fontSizeScale('small'), 1.15, 'small ⇒ scale 1.15');
  assertEqual(ui.fontSizeScale('medium'), 1.3, 'medium ⇒ scale 1.3');
  assertEqual(ui.fontSizeScale('large'), 1.45, 'large ⇒ scale 1.45');
  assertEqual(ui.fontSizeScale('nope'), 1.15, 'an unknown size scales at the small floor');
  // read default with an empty store; persist + read round-trips each size.
  assertEqual(ui.readFontSize(), 'small', 'an empty store reads small');
  for (const size of ['medium', 'large', 'small']) {
    assertEqual(ui.persistFontSize(size), size, 'persistFontSize returns the normalised size');
    assertEqual(ui.readFontSize(), size, 'readFontSize round-trips ' + size);
  }
  // a bogus persisted value normalises back to small on read.
  globalThis.window.localStorage.setItem('zicato.T.fontsize', 'huge');
  assertEqual(ui.readFontSize(), 'small', 'a bogus stored size reads small');
});

test('shell: applyFontSize stamps --dt-font-scale + data-t-fontsize per size + persists', async () => {
  const ui = await import('../js/variants/T/ui.js');
  const shell = await import('../js/variants/T/shell.js');
  globalThis.window.localStorage.clear();
  const root = globalThis.document.createElement('div');
  const expect = { small: 1.15, medium: 1.3, large: 1.45 };
  for (const size of ['medium', 'large', 'small']) {
    const applied = shell.applyFontSize(size, root);
    assertEqual(applied, size, 'applyFontSize returns the applied size');
    assertEqual(root.style._props['--dt-font-scale'], String(expect[size]),
      '--dt-font-scale is set to ' + expect[size] + ' for ' + size);
    assertEqual(root.getAttribute('data-t-fontsize'), size, 'data-t-fontsize stamped ' + size);
    assertEqual(ui.readFontSize(), size, 'applyFontSize persisted ' + size + ' to the shared store');
  }
  // small ⇒ the default floor, scale 1.15.
  shell.applyFontSize('small', root);
  assertEqual(root.style._props['--dt-font-scale'], '1.15', 'small stamps scale 1.15 (default floor)');
});

test('settings: the typeface popover carries the S/M/L text-size control + applies via the shared store', async () => {
  installFetch();
  globalThis.window.localStorage.clear();
  const ui = await import('../js/variants/T/ui.js');
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  const tf = firstClass(body, 'dt-tf');
  assert(tf, 'the typeface grouped popover renders in Appearance');
  // the S/M/L segmented control rides in the popover footer — three segments.
  const segs = byClass(tf, 'dt-tf-sizeseg');
  assertEqual(segs.length, 3, 'three text-size segments (S · M · L) in the popover footer');
  const ids = segs.map((b) => b.getAttribute('data-fontsize'));
  assert(ids.includes('small') && ids.includes('medium') && ids.includes('large'),
    'the segments are small / medium / large');
  // small is checked by default (the current look).
  const small = segs.find((b) => b.getAttribute('data-fontsize') === 'small');
  assertEqual(small.getAttribute('aria-checked'), 'true', 'small is checked by default');
  // choosing LARGE applies via the shared store (persists to the same key).
  const large = segs.find((b) => b.getAttribute('data-fontsize') === 'large');
  large.dispatchEvent(makeEvent('click'));
  assertEqual(ui.readFontSize(), 'large', 'choosing large persisted via the shared font-size store');
  // the segment sync flipped aria-checked: large on, small off (cross-instance).
  assertEqual(large.getAttribute('aria-checked'), 'true', 'the large segment is now checked');
  assertEqual(small.getAttribute('aria-checked'), 'false', 'the small segment is no longer checked');
});

await run();
