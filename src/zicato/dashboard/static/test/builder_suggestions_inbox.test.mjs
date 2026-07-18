// test/builder_suggestions_inbox.test.mjs — the B2 suggestions inbox (WS-SURFACE).
//
// The eval-suggestions inbox (EVAL-SYNTHESIS.md §6): the Board section renders
// the persisted `reflect suggest` output as verdict-led rows — rationale,
// provenance, admission stats rendered HONESTLY (measured with n / unmeasured),
// an Instrument-lens link, and a "stage to draft" affordance driving the
// add_board_entry / add_judge ops. Three layers: RENDER (rows + honest
// admission + link), DEGRADE (honest empty), and STAGE (the op dispatch). Plus
// a digest no-op guard: a re-render over the same feed churns no DOM.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const VOCAB = {
  kinds: ['single_turn', 'multi_turn_scripted', 'multi_turn_emulated', 'synthetic_adversarial', 'synthetic_clean'],
  expectation_kinds: ['expected_text', 'regex', 'json_schema', 'predicate', 'rubric'],
  reads: ['final_output', 'conversation_end'],
  judge_modes: ['inline', 'python'],
  severities: ['info', 'warning', 'critical'],
  drift_kinds: ['off_topic'],
};
const CONFIG = { chat_enabled: false, agent: { model: '' }, skills: [], vocab: VOCAB };

function freshDraft() {
  return {
    scoring: {
      tournament: { structure: 'gauntlet', params: { field_size: 2, replicates: 1 } },
      overfitting: { enabled: true, holdout_fraction: 0.2, min_board_size_for_split: 8, ladder: { enabled: true, budget: 16, noise_scale: 0 } },
      promote_margin: 0, drift_weight: 1, pass_weight: 1,
      namespace_weights: {}, proposer_quality: { best_of_n: 3, critique_enabled: true }, experiment_memory: {},
    },
    board: [{ id: 'entryA', kind: 'single_turn', budget_s: 60, input: 'hi' }],
    board_meta: { disable_drift: [], judge_only: false },
    brief: 'steer', proposer_path: null,
    proposer: { agent_id: 'default', tools: [], skills: [], has_custom_agent: false },
    holdout: { train_ids: ['entryA'], holdout_ids: [] },
  };
}

// The suggestions feed: an entry suggestion (unmeasured), a judge suggestion
// (measured admission), and a rubric revision (recommendation only, no op).
function feed() {
  return {
    epoch_id: 'epoch-1', reflection_id: 'refl-xyz',
    suggestions: [
      {
        suggestion_id: 'sug-judge01', suggestion_type: 'judge_suggestion', artifact_kind: 'judge',
        subject: 'citations', summary: 'draft a citations judge', target_slice: 'incoming_rotation',
        proposed_op: { op: 'add_judge', args: { entry_id: 'entryA', judge: { name: 'citations', mode: 'inline', body: 'x', severity: 'warning' } } },
        provenance: { source_episodes: ['ep-1'], source_lineage_ids: ['g0'], target_slice: 'incoming_rotation' },
        admission: { noise: { flip_rate: 0.1, runs: 5, measured: true, base: 6000 }, discrimination: { separated: 3, pairs: 5, measured: true }, leakage: { target_slice_ok: true, self_preference_flag: false } },
      },
      {
        suggestion_id: 'sug-entry01', suggestion_type: 'regression_entry', artifact_kind: 'board_entry',
        subject: 'entryA', summary: 'pin the g0 miss', target_slice: 'train',
        proposed_op: { op: 'add_board_entry', args: { entry: { id: 'entryA_reg', kind: 'single_turn', wall_clock_budget_seconds: 30, input: 'pinned' } } },
        provenance: { source_episodes: ['ep-2'], source_lineage_ids: ['g0'], target_slice: 'train' },
        admission: null,
      },
      {
        suggestion_id: 'sug-rub01', suggestion_type: 'rubric_revision', artifact_kind: 'rubric_revision',
        subject: 'tone', summary: 'tighten the tone rubric', target_slice: 'existing_judge',
        proposed_op: null, provenance: { source_episodes: ['ep-3'] }, admission: null,
      },
    ],
  };
}

let SUGGESTIONS = feed();
const OP_CALLS = [];

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
    if (path === '/builder/op') {
      OP_CALLS.push(body);
      return jsonRes({ draft: DRAFT, patch: { op: body.op, changed: {} }, cost: { board_runs_per_round: 2, breakdown: [] }, warnings: [], diff: { components: [], changed_components: [] } });
    }
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

function byClass(root, cls) { return root.querySelectorAll('[class]').filter((n) => n.classList.contains(cls)); }
function firstClass(root, cls) { return byClass(root, cls)[0] || null; }
function tick() { return new Promise((r) => setTimeout(r, 0)); }

async function mountBoard() {
  view._resetBuilderForTest();
  installFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  return host;
}

test('suggestions inbox: renders one row per persisted suggestion', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const rows = byClass(host, 'dn-bld-sugrow');
  assertEqual(rows.length, 3, 'one row per suggestion');
  const text = firstClass(host, 'dn-bld-suggestions').textContent;
  assert(text.includes('regression_entry'), 'the entry suggestion type shows');
  assert(text.includes('judge_suggestion'), 'the judge suggestion type shows');
});

test('suggestions inbox: admission renders HONESTLY — measured with n, else unmeasured', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const stats = byClass(host, 'dn-bld-sugadmission').map((n) => n.textContent);
  const joined = stats.join(' | ');
  assert(/flip 0\.1 \(n=5 @base 6000\)/.test(joined), 'the measured judge shows flip WITH n + base');
  assert(/sep 3\/5/.test(joined), 'the measured judge shows discrimination');
  assert(/unmeasured/.test(joined), 'the unmeasured entry reads unmeasured (never a fabricated 0)');
});

test('suggestions inbox: the advisory band renders when a measured stat crosses a recommended band', async () => {
  const f = feed();
  // A noisy measured suggestion: flip 0.4 > the 0.25 advisory ceiling.
  f.suggestions[0].admission = {
    noise: { flip_rate: 0.4, runs: 5, measured: true, base: 6000 },
    discrimination: { separated: 0, pairs: 5, measured: true },
    leakage: { target_slice_ok: true, self_preference_flag: false },
  };
  SUGGESTIONS = f;
  const host = await mountBoard();
  const joined = byClass(host, 'dn-bld-sugadmission').map((n) => n.textContent).join(' | ');
  assert(/advisory:/.test(joined), 'the advisory band renders (mirrors format_admission)');
  assert(/advisory ceiling/.test(joined), 'names the flip ceiling advice');
  assert(/dead channel/.test(joined), 'names the dead-channel advice');
});

test('suggestions inbox: the Instrument-lens link points at the motivating reflection', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const link = firstClass(host, 'dn-instr-link');
  assert(link, 'an instrument link rendered');
  assertEqual(link.getAttribute('href'), '#/e/epoch-1/instrument/refl-xyz', 'links to the reflection lens');
});

test('suggestions inbox: "stage to draft" drives the suggestion op; a rubric revision has no op', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const buttons = host.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-sugstage'));
  // two staged ops (entry + judge); the rubric revision is recommendation-only.
  assertEqual(buttons.length, 2, 'only the two op-bearing suggestions get a stage button');
  buttons.find((b) => b.getAttribute('aria-label').includes('sug-entry01')).dispatchEvent(makeEvent('click'));
  await tick();
  const staged = OP_CALLS.find((c) => c.op === 'add_board_entry');
  assert(staged, 'staging the entry suggestion posted add_board_entry');
  assertEqual(staged.args.entry.id, 'entryA_reg', 'the drafted entry rode into the op');
});

test('suggestions inbox: an empty feed degrades to an honest empty state', async () => {
  SUGGESTIONS = { epoch_id: 'epoch-1', reflection_id: null, suggestions: [] };
  const host = await mountBoard();
  assertEqual(byClass(host, 'dn-bld-sugrow').length, 0, 'no rows');
  const inbox = firstClass(host, 'dn-bld-suggestions');
  assert(/run .?zicato reflect suggest/.test(inbox.textContent), 'honest empty state names the command');
});

test('suggestions inbox: a re-render over the same feed churns no DOM (digest no-op)', async () => {
  SUGGESTIONS = feed();
  const host = await mountBoard();
  const inbox1 = firstClass(host, 'dn-bld-suggestions');
  // a second board render over identical state — the gated center must reuse.
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  const inbox2 = firstClass(host, 'dn-bld-suggestions');
  assert(inbox1 === inbox2, 'the inbox DOM node is reused (digest-gated, anti-flash)');
});

await run();
