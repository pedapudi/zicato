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
  tournament: { structure: 'swiss', params: { field_size: 4, replicates: 1, rounds_n: 3 } },
  scoring: { promote_margin: 0.05, pass_rate_monotonicity: true, overfitting: { enabled: true, holdout_fraction: 0.25 } },
  // /api/epoch computes the train/holdout split SERVER-SIDE; the contract
  // preview reads its counts (so it never re-derives the sha256 hash split).
  board_split: { configured: true, enabled: true, holdout_fraction: 0.25, train_count: 2, holdout_count: 0, total: 2 },
  proposer: { },
};

// the models section reads /settings/models — the secret-safe per-role view:
// each role carries a call_llm path OR a {model, endpoint, api_key_env} spec
// with an api_key_env_set boolean. NO secret value is ever present.
const MODELS_VIEW = {
  models: {
    engines: {
      target: { call_llm: 'pkg.harness:call_llm' },
      evaluation: { model: 'house-model-x', endpoint: null, api_key_env: 'HOUSE_API_KEY', api_key_env_set: true },
      builder: { model: 'builder-model', endpoint: 'https://endpoint.example', api_key_env: 'BUILDER_KEY', api_key_env_set: false },
    },
    roles: { builder: 'builder' },
  },
  roles: ['target', 'evaluation', 'builder', 'judge', 'adjudicator', 'user_emulator', 'proposer', 'proposer_generate', 'proposer_review'],
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
        const posted = _lastModelsPost.models || { engines: {}, roles: {} };
        const engines = {};
        for (const [name, s] of Object.entries(posted.engines || {})) {
          engines[name] = s.call_llm ? { call_llm: s.call_llm }
            : { ...s, api_key_env_set: false };
        }
        return jsonRes({ models: { engines, roles: posted.roles || {} }, roles: MODELS_VIEW.roles, rolls_epoch: false });
      }
      return jsonRes(MODELS_VIEW);
    }
    if (path.startsWith('/builder/draft')) {
      // the SERVER cost envelope for the current (swiss) contract — the same
      // number the server serves rather than the client re-estimating it.
      return jsonRes({ session: 'dashboard', draft: { scoring: { tournament_structure: { structure: 'gauntlet', params: {} } }, board: [], holdout: { train_ids: [], holdout_ids: [] }, proposer: {} }, cost: { board_runs_per_round: 12, breakdown: [{ label: 'swiss', runs: 12, detail: '3 rounds' }] }, warnings: [], diff: { changed_components: [], rolls_epoch: false } });
    }
    return jsonRes({});
  };
}

const router = await import('../js/router.js');
const settings = await import('../js/views/settings.js');

function byClass(root, cls) { return root.querySelectorAll('[class]').filter((n) => n.classList.contains(cls)); }
function firstClass(root, cls) { return byClass(root, cls)[0] || null; }
const ctx = { navigate() {}, href: router.href };
function tick() { return new Promise((r) => setTimeout(r, 0)); }

// ── router: one component, multiple entry points ──────────────────────

test('router: #/settings resolves to the settings view (default contract section)', () => {
  const r = router.parseRoute('#/settings');
  assertEqual(r.view, 'settings', 'view is settings');
  assertEqual(r.params.section, null, 'no explicit section ⇒ default');
  // the builder is not the default Settings section — the router exports
  // the new default so the view + the back/up target agree on it.
  assertEqual(router.DEFAULT_SETTINGS_SECTION, 'contract', 'the default Settings section is now the contract roll-up');
});

test('router: #/settings/models deep-links the models section', () => {
  const r = router.parseRoute('#/settings/models');
  assertEqual(r.view, 'settings', 'view is settings');
  assertEqual(r.params.section, 'models', 'the models section');
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

test('settings: the rail renders contract, model, and appearance sections', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'contract' });
  const items = byClass(host, 'dn-set-railitem');
  // three in-host sections (contract / models / appearance) + the launcher.
  assertEqual(items.length, 3, 'three settings sections');
  const labels = items.map((i) => i.textContent);
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
  const ui = await import('../js/ui.js');
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
  const ui = await import('../js/ui.js');
  // a value set the "top-bar way" (persistType) is reflected by the settings
  // picker's initial selected option — one source of truth, both directions.
  ui.persistType('literata');
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  const tf = firstClass(body, 'dt-tf');
  assert(tf, 'the typeface grouped popover renders in Appearance');
  const onOpt = byClass(tf, 'dt-tf-option').find((o) => o.getAttribute('aria-selected') === 'true');
  assert(onOpt && onOpt.getAttribute('data-type') === 'literata', 'the popover reflects the shared typeface store');
  ui.persistType('display');
  assertEqual(ui.readType(), ui.DEFAULT_TYPE, 'a retired typeface alias uses the existing default');
});

// The typeface picker now lives ONLY in Settings (removed from the top bar).
// Choosing a face there must still APPLY live (stamp the app root via the shared
// applyTypeface store) AND PERSIST — so this is the sole live picker now.
test('settings: the Settings typeface picker still APPLIES + PERSISTS (the sole live picker)', async () => {
  installFetch();
  globalThis.window.localStorage.clear();
  const ui = await import('../js/ui.js');
  // a stable app root the picker's applyTypeface can stamp (applyTypeface falls
  // back to the shell's `_root` when no root is passed — mountShell sets it, but
  // here we drive the picker directly and assert via the persisted store).
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'appearance' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  const tf = firstClass(body, 'dt-tf');
  assert(tf, 'the typeface grouped popover renders in Settings → Appearance');
  // pick the Inconsolata option — it must apply via the shared store and persist.
  const inconsolata = byClass(tf, 'dt-tf-option').find((o) => o.getAttribute('data-type') === 'inconsolata');
  assert(inconsolata, 'the popover has the Inconsolata option');
  inconsolata.dispatchEvent(makeEvent('click'));
  assertEqual(ui.readType(), 'inconsolata', 'choosing Inconsolata in Settings persisted via the shared typeface store');
  // and the closed trigger now reflects the chosen face (the live picker synced).
  const trigger = firstClass(tf, 'dt-cd-trigger');
  assert(trigger, 'the typeface popover has a closed trigger');
  assert((trigger.textContent || '').length > 0, 'the trigger shows the chosen face label');
});

// The old accent-tinted, pulsing "light-up rail card" research-preview banner is
// GONE from the Settings surface. The product-status mark is now a QUIET pill
// pinned NEXT TO the wordmark in the top bar (asserted in shell.test.mjs),
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
  assert(rows.length > 0 && rows.every((r) => !r.getAttribute('href')), 'contract rows are read-only');
});

// CHANGE 2: the Contract section LEADS with the builder's live-preview RENDERER
// (reused read-only) — bound to /api/epoch's frozen contract. It shows the
// per-structure schematic + the cost meter + the train/holdout strip + any
// validation diagnostics, computed CLIENT-SIDE — and carries NO apply controls.

test('settings: the Contract section names the holdout bounds ONCE they are pinned', async () => {
  installFetch();
  // /api/epoch is read through the shared cache, so the earlier tests' epoch
  // would otherwise stand in for this one's pinned contract.
  (await import('../js/data.js')).invalidate('/api/epoch');
  const base = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (path === '/api/epoch') {
      return jsonRes(Object.assign({}, EPOCH, {
        scoring: Object.assign({}, EPOCH.scoring, {
          holdout_margin: 0.09, holdout_entry_regression_budget: 1,
        }),
      }));
    }
    return base(path, init);
  };
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'contract' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  // Unshown, this summary's lone "Promote margin" row implies one bound
  // governs both slices — the single-knob reading the separate holdout
  // bounds exist to end.
  assert(body.textContent.includes('Holdout margin'), 'the pinned holdout margin is named');
  assert(body.textContent.includes('0.09'), 'it shows the pinned value, not the promote margin');
  assert(body.textContent.includes('Holdout regression budget'), 'the pinned budget is named');
  assert(body.textContent.includes('1 entry'), 'the budget is singular at one entry');
});



test('settings: the Models section shows the api_key_env NAME + set/unset flag, never a secret', async () => {
  installFetch();
  const host = globalThis.document.createElement('div');
  await settings.render(host, ctx, { section: 'models' });
  await tick();
  const body = firstClass(host, 'dn-set-body');
  assert(body.textContent.includes('HOUSE_API_KEY'), 'shows the credential variable name');
  assert(body.textContent.includes('api_key_env_set'), 'shows credential availability');
  assert(body.textContent.includes('true') && body.textContent.includes('false'), 'shows present and absent credentials');
  // there is NO secret-value input or text anywhere — only env-var NAMES.
  assert(!body.textContent.toLowerCase().includes('sk-'), 'no secret-looking value is rendered');
  const inputs = byClass(body, 'dn-set-input');
  assert(!inputs.some((i) => (i.getAttribute('type') || '') === 'password'), 'no password / secret input exists');
});




// ── S/M/L FONT-SIZE control (text-only multiplier in the typeface picker) ──
//
// DISTINCT from the page-scale pill (whole-page `zoom`): this scales the HTML
// text via `--dt-font-scale` WITHOUT touching the SVG figures. The ladder
// starts above the raw literal-px baseline (too small for low-x-height faces):
// small 1.15 (the default floor), medium 1.3, large 1.45.

test('ui: font-size model — normalise + read/persist round-trip + scale values', async () => {
  const ui = await import('../js/ui.js');
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
  globalThis.window.localStorage.setItem('zicato.console.fontsize', 'huge');
  assertEqual(ui.readFontSize(), 'small', 'a bogus stored size reads small');
});

// The console's preference keys were renamed from `zicato.T.<name>` to
// `zicato.console.<name>`. A browser used before the rename still holds the
// viewer's choices under the old spelling, so every preference must read back
// from it — and a write must land on the current key alone, so the value moves
// across the first time it is set.
//
// Every value below is one the preference accepts today, so each assertion
// turns on the key spelling alone. What a stored VALUE may say is a separate
// question each preference answers for itself: the typeface's retired option
// ids are pinned in candidate_surfaces.test.mjs.

test('shell: applyFontSize stamps --dt-font-scale + data-t-fontsize per size + persists', async () => {
  const ui = await import('../js/ui.js');
  const shell = await import('../js/shell.js');
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
  const ui = await import('../js/ui.js');
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

for (const failure of ['http', 'network']) {
  test(`settings: Models keeps navigation usable after ${failure} failure and recovers`, async () => {
    globalThis.fetch = async () => {
      if (failure === 'network') throw new Error('connection failed');
      return { ok: false, status: 404, json: async () => ({}) };
    };
    const host = document.createElement('div');
    await settings.render(host, ctx, { section: 'models' });
    const rail = firstClass(host, 'dn-set-rail');
    const body = firstClass(host, 'dn-set-body');
    assertEqual(byClass(rail, 'dn-set-railitem').length, 3, 'all settings sections remain available');
    assert(body.textContent.includes('Could not load the models settings.'), 'failed reads have an explicit unavailable state');
    assertEqual(body.querySelectorAll('pre').length, 0, 'unavailable settings do not render an empty configuration');
    const unavailable = body.firstChild;
    await settings.render(host, ctx, { section: 'models' });
    assert(body.firstChild === unavailable, 'an unchanged unavailable state preserves its nodes');

    installFetch();
    await settings.render(host, ctx, { section: 'models' });
    assert(body.textContent.includes('HOUSE_API_KEY'), 'a later successful read displays the configured values');
    assert(!body.textContent.includes('Could not load'), 'successful read clears the unavailable state');
    const available = body.firstChild;
    await settings.render(host, ctx, { section: 'models' });
    assert(body.firstChild === available, 'an unchanged successful read preserves its nodes');
    await settings.render(host, ctx, { section: 'appearance' });
    assert(firstClass(host, 'dn-set-rail') === rail, 'navigation keeps the section rail');
    assert(firstClass(body, 'dn-set-appgrid'), 'appearance remains usable after a failed model read');
  });
}

await run();
