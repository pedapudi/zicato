// test/builder_suggestion_cards.test.mjs — the inbox CARDS (TRAJECTORY-UI.md §2.2a).
//
// The suggestions inbox rows upgraded to cards carrying the ADMISSION VISUALS
// (flip-rate whisker + discrimination pips + evidence tier, honest unmeasured
// states) and — for a FOREIGN-source suggestion — the PROVENANCE MINI-STRIP via
// the shared trajectory-strip figure's compact mode behind a GUARDED dynamic
// import (absent figure → the textual fallback; present → the figure renders).
//
// COMPOSITION CHECK (§4.1): the foreign card's mini-strip renders from the REAL
// reader payload — `test/fixtures/trace_view/provenance.json`, captured from the
// real pipeline by tools/gen_trace_view_fixtures.py — never a hand-authored
// strip. XSS: rationales/ids are textContent (probe markup stays inert).

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const here = dirname(fileURLToPath(import.meta.url));
const PROVENANCE = JSON.parse(readFileSync(join(here, 'fixtures/trace_view/provenance.json'), 'utf8'));

const VOCAB = {
  kinds: ['single_turn', 'multi_turn_scripted', 'multi_turn_emulated', 'synthetic_adversarial', 'synthetic_clean'],
  expectation_kinds: ['expected_text', 'regex', 'json_schema', 'predicate', 'rubric'],
  reads: ['final_output', 'conversation_end'], judge_modes: ['inline', 'python'],
  severities: ['info', 'warning', 'critical'], drift_kinds: ['off_topic'],
};
const CONFIG = { chat_enabled: false, agent: { model: '' }, skills: [], vocab: VOCAB };

function freshDraft() {
  return {
    scoring: {
      tournament: { structure: 'gauntlet', params: { field_size: 2, replicates: 1 } },
      overfitting: { enabled: true, holdout_fraction: 0.2, min_board_size_for_split: 8, ladder: { enabled: true, budget: 16, noise_scale: 0 } },
      promote_margin: 0, pass_weight: 1,
      namespace_weights: {}, proposer_quality: { best_of_n: 3, critique_enabled: true }, experiment_memory: {},
    },
    board: [{ id: 'entryA', kind: 'single_turn', budget_s: 60, input: 'hi' }],
    board_meta: { disable_drift: [], judge_only: false },
    brief: 'steer', proposer_path: null,
    proposer: { agent_id: 'default', tools: [], skills: [], has_custom_agent: false },
    holdout: { train_ids: ['entryA'], holdout_ids: [] },
  };
}

// The feed: a MEASURED judge suggestion (probed tier), a NOISY measured one
// (flip over the 0.25 ceiling + a dead channel), an UNMEASURED entry (planned
// tier), and a FOREIGN bootstrap suggestion whose id matches the real
// provenance fixture (refl-traceviz / sug-14ffa7e6).
function feed() {
  return {
    epoch_id: '2020-01-01_boot', reflection_id: 'refl-traceviz',
    suggestions: [
      {
        suggestion_id: 'sug-judge01', suggestion_type: 'judge_suggestion', artifact_kind: 'judge',
        subject: 'citations', summary: 'draft a citations judge', rationale: 'g0 missed a citation', target_slice: 'incoming_rotation',
        proposed_op: { op: 'add_judge', args: { entry_id: 'entryA', judge: { name: 'citations', mode: 'inline', body: 'x', severity: 'warning' } } },
        provenance: { source_episodes: ['ep-1'], source_lineage_ids: ['g0'], target_slice: 'incoming_rotation' },
        admission: { noise: { flip_rate: 0.1, runs: 5, measured: true, base: 6000 }, discrimination: { separated: 3, pairs: 5, measured: true }, leakage: { target_slice_ok: true, self_preference_flag: false } },
      },
      {
        suggestion_id: 'sug-noisy01', suggestion_type: 'judge_suggestion', artifact_kind: 'judge',
        subject: 'tone', summary: 'a noisy tone judge', rationale: 'noisy', target_slice: 'incoming_rotation',
        proposed_op: { op: 'add_judge', args: { entry_id: 'entryA', judge: { name: 'tone', mode: 'inline', body: 'x', severity: 'warning' } } },
        provenance: { source_episodes: ['ep-2'] },
        admission: { noise: { flip_rate: 0.4, runs: 5, measured: true, base: 6000 }, discrimination: { separated: 0, pairs: 5, measured: true }, leakage: { target_slice_ok: true, self_preference_flag: false } },
      },
      {
        suggestion_id: 'sug-entry01', suggestion_type: 'regression_entry', artifact_kind: 'board_entry',
        subject: 'entryA', summary: 'pin the g0 miss', rationale: 'plan mode — no probe', target_slice: 'train',
        proposed_op: { op: 'add_board_entry', args: { entry: { id: 'entryA_reg', kind: 'single_turn', wall_clock_budget_seconds: 30, input: 'pinned' } } },
        provenance: { source_episodes: ['ep-3'], source_lineage_ids: ['g0'], target_slice: 'train' },
        admission: null,
      },
      {
        suggestion_id: 'sug-14ffa7e6', suggestion_type: 'regression_entry', artifact_kind: 'board_entry',
        subject: 'trace-a0be332d', summary: 'pin the recorded error cascade',
        rationale: 'keep this in train unless the trace is genuinely foreign',
        target_slice: 'train',
        proposed_op: { op: 'add_board_entry', args: { entry: { id: 'bootstrap_cascade', kind: 'single_turn', wall_clock_budget_seconds: 30, input: 'recorded opener' } } },
        provenance: { source_episodes: ['ep-acaca016'], source_lineage_ids: [], target_slice: 'train',
          foreign_source: { kind: 'trajectory_bootstrap', dialect: 'adk_events', trace_id: 'trace-a0be332d', source_file: 'adk_run.jsonl' } },
        admission: null,
      },
    ],
  };
}

let SUGGESTIONS = feed();
const OP_CALLS = [];
const PROV_PATH = '/api/reflection/refl-traceviz/suggestion/sug-14ffa7e6/provenance';

function jsonRes(obj) {
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => obj, text: async () => JSON.stringify(obj) };
}
function installFetch() {
  OP_CALLS.length = 0;
  const DRAFT = freshDraft();
  globalThis.fetch = async (path, init) => {
    const body = init && init.body ? JSON.parse(init.body) : {};
    if (path === '/builder/config') return jsonRes(CONFIG);
    if (path.startsWith('/builder/draft')) return jsonRes({ session: 'dashboard', draft: DRAFT, cost: { board_runs_per_round: 2, breakdown: [] }, warnings: [], diff: { components: [], changed_components: [] }, drafts: [], proposer_dirs: [] });
    if (path === '/builder/suggestions') return jsonRes(SUGGESTIONS);
    if (path === PROV_PATH) return jsonRes(PROVENANCE);
    if (path === '/builder/op') { OP_CALLS.push(body); return jsonRes({ draft: DRAFT, patch: { op: body.op, changed: {} }, cost: { board_runs_per_round: 2, breakdown: [] }, warnings: [], diff: { components: [], changed_components: [] } }); }
    return jsonRes({});
  };
}
function installEnv() {
  globalThis.window = globalThis.window || {};
  globalThis.window.addEventListener = globalThis.window.addEventListener || (() => {});
  if (!globalThis.window.localStorage) {
    const kv = new Map();
    globalThis.window.localStorage = { getItem: (k) => (kv.has(k) ? kv.get(k) : null), setItem: (k, v) => kv.set(k, String(v)), removeItem: (k) => kv.delete(k), clear: () => kv.clear() };
  }
  globalThis.HashChangeEvent = globalThis.HashChangeEvent || function HashChangeEvent() {};
}
installEnv();

const view = await import('../js/views/builder.js');
const data = await import('../js/data.js');

function byClass(root, cls) { return root.querySelectorAll('[class]').filter((n) => n.classList.contains(cls)); }
function firstClass(root, cls) { return byClass(root, cls)[0] || null; }
function tick() { return new Promise((r) => setTimeout(r, 0)); }

async function mountBoard() {
  view._resetBuilderForTest();
  data.invalidate();
  installFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();     // provenance fetch for the foreign card resolves + re-renders
  await tick();
  return host;
}

test('cards: every suggestion renders as a card with admission visuals', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  assertEqual(byClass(host, 'dn-bld-sugcard').length, 4, 'one card per suggestion');
  assert(byClass(host, 'dn-adm').length >= 4, 'each card carries an admission-visuals block');
  // the whisker figure + the pips idiom render.
  assert(byClass(host, 'dn-adm-whisker').length >= 4, 'the flip-rate whisker renders per card');
});

test('cards: a MEASURED admission shows a firm tier, a flip dot, and filled pips', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const cards = byClass(host, 'dn-bld-sugcard');
  const judge = cards.find((c) => c.textContent.includes('citations'));
  assert(judge, 'the measured judge card exists');
  const tier = byClass(judge, 'dn-adm-tier')[0];
  assert(tier && /probed/.test(tier.textContent), 'a measured probe reads the firm "probed" tier');
  assert(byClass(judge, 'dn-adm-flipdot').length === 1, 'a measured flip renders a whisker point (not the unmeasured rail)');
  assert(byClass(judge, 'dt-rungstep-done').length === 3, 'three of five discrimination pips are filled (sep 3/5)');
  assert(byClass(judge, 'dn-adm-pip').length === 5, 'five pips total (pairs 5)');
});

test('cards: an over-ceiling flip rides caution; a dead channel shows no filled pips', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const noisy = byClass(host, 'dn-bld-sugcard').find((c) => c.textContent.includes('a noisy tone judge'));
  assert(noisy, 'the noisy card exists');
  assertEqual(byClass(noisy, 'dn-caution').length, 1, 'the over-ceiling flip dot rides the caution tone');
  assert(/over ceiling/.test(noisy.textContent), 'the readout names the over-ceiling state');
  assertEqual(byClass(noisy, 'dt-rungstep-done').length, 0, 'a dead channel (sep 0) fills no pips');
});

test('cards: an UNMEASURED admission is honest — planned tier, "unmeasured" (never a fabricated 0)', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const entry = byClass(host, 'dn-bld-sugcard').find((c) => c.textContent.includes('pin the g0 miss'));
  assert(entry, 'the unmeasured entry card exists');
  const tier = byClass(entry, 'dn-adm-tier')[0];
  assert(tier && /planned/.test(tier.textContent), 'an unspent probe reads the faint "planned" tier');
  assertEqual(byClass(entry, 'dn-adm-flipdot').length, 0, 'no flip point on an unmeasured whisker');
  assert(/unmeasured/.test(byClass(entry, 'dn-adm')[0].textContent), 'the admission block reads "unmeasured"');
  assert(!/flip 0\.0\b/.test(entry.textContent) && !/sep 0\/0/.test(entry.textContent), 'no fabricated 0.0 / 0/0');
});

test('mini-strip: absent figure → the textual fallback renders from the REAL provenance payload', async () => {
  SUGGESTIONS = feed();
  view._resetBuilderForTest();
  data.invalidate();
  installFetch();
  // FORCE the absent branch: on the merged tree the real svg.trajectoryStrip
  // exists, so `undefined` (not the null default) pins the fallback path. Must
  // be injected AFTER the reset (which restores the null default).
  view._setStripFigureForTest(undefined);
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  await tick();
  const boot = byClass(host, 'dn-bld-sugcard').find((c) => c.textContent.includes('trace-a0be332d'));
  assert(boot, 'the foreign bootstrap card exists');
  const strip = firstClass(boot, 'dn-bld-sugstrip');
  assert(strip, 'the foreign card carries a mini-strip host');
  const fallback = firstClass(boot, 'dn-bld-sugstrip-fallback');
  assert(fallback, 'the textual fallback renders when the figure is absent');
  // the fallback is derived from the REAL segment_strip_model (2 turns, signals, episodes).
  assert(/turn\(s\).*signal\(s\).*episode\(s\)/.test(fallback.textContent), 'the fallback summarises the real strip-model');
  // the Traces detail link resolves to the REGISTERED route (the review caught a
  // substring-only assert passing on a misrouted `/trace/` URL) — pin the full
  // #/e/<epoch>/traces/<reflection>/<trace> shape rather than a fragment.
  const link = firstClass(boot, 'dn-bld-sugtracelink');
  assert(link, 'the card carries a Traces detail link');
  const href = link.getAttribute('href') || '';
  assert(/^#\/e\/[^/]+\/traces\/[^/]+\/trace-a0be332d$/.test(href),
    'the link is the Traces DETAIL route (epoch/traces/reflection/trace), got: ' + href);
});

test('mini-strip: present figure → it renders, replacing the fallback', async () => {
  SUGGESTIONS = feed();
  view._resetBuilderForTest();
  data.invalidate();
  installFetch();
  // inject a synchronous figure factory (the guarded-import seam).
  let seenModel = null;
  view._setStripFigureForTest((model, opts) => {
    seenModel = { model, opts };
    return globalThis.document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  });
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  await tick();
  const boot = byClass(host, 'dn-bld-sugcard').find((c) => c.textContent.includes('trace-a0be332d'));
  const strip = firstClass(boot, 'dn-bld-sugstrip');
  assert(strip && strip.children.length === 1 && strip.children[0].tagName === 'SVG', 'the figure Node replaced the fallback');
  assert(byClass(boot, 'dn-bld-sugstrip-fallback').length === 0, 'the textual fallback is gone once the figure mounts');
  assert(seenModel && seenModel.opts && seenModel.opts.compact === true, 'the figure is called in COMPACT mode');
  assertEqual(seenModel.model.focus_episode_id, 'ep-acaca016', 'the real segment strip-model (focus set) is passed');
});

test('cards: the roll-honesty note renders for a foreign train draft', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const boot = byClass(host, 'dn-bld-sugcard').find((c) => c.textContent.includes('trace-a0be332d'));
  assert(firstClass(boot, 'dn-bld-sugroll'), 'the roll-honesty caption renders');
  assert(/default to train/.test(boot.textContent), 'it states the train default');
});

test('cards: rationale + subject are textContent — probe markup stays inert (XSS)', async () => {
  const f = feed();
  f.suggestions[0].rationale = '<img src=x onerror=alert(1)>';
  f.suggestions[0].subject = '<script>bad()</script>';
  SUGGESTIONS = f;
  const host = await mountBoard();
  const judge = byClass(host, 'dn-bld-sugcard')[0];
  // the markup survives verbatim in textContent (escaped/inert) — never parsed
  // into an <img>/<script> element.
  assert(judge.textContent.includes('<img src=x onerror=alert(1)>'), 'the rationale markup is inert text');
  assert(judge.textContent.includes('<script>bad()</script>'), 'the subject markup is inert text');
  assertEqual(host.innerHTMLWriteCount(), 0, 'the cards never write innerHTML');
});

await run();
