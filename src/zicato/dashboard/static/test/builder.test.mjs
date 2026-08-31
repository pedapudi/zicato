// test/builder.test.mjs — the tournament-builder view (B2).
//
// Exercises the slice of frontend behaviour the structural / parsing tests
// can't reach: the view renders each contract section; a form edit calls
// POST /builder/op and applies the returned {draft,patch,cost,warnings,diff}
// to the SHARED draft so the form + preview re-render; an SSE `patch` frame
// applied through the chat path updates the same shared draft; the chat pane
// resize persists + clamps and the layout reflows without overlap; the
// graceful-degrade path disables the chat input. Same harness style as the
// rest of the console suite.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

// ── a route-keyed fetch mock for the builder endpoints ───────────────
//
// The view loads /builder/config + /builder/draft once, then POSTs ops. We
// record the op calls and return a mutated draft so the apply-to-shared-draft
// path is observable. A chat-disabled config is the default so the degrade
// path is the baseline; a test flips `CONFIG.chat_enabled` before mount.

const CONFIG = {
  chat_enabled: false, agent: { model: '' }, skills: ['zicato-build-tournament'],
  // the server-derived board-authoring vocabulary (the fixed-row mapping editors
  // render their rows from these enums; the JS never hardcodes them).
  vocab: {
    kinds: ['single_turn', 'multi_turn_scripted', 'multi_turn_emulated', 'synthetic_adversarial', 'synthetic_clean'],
    expectation_kinds: ['expected_text', 'regex', 'json_schema', 'predicate', 'rubric'],
    reads: ['final_output', 'conversation_end'],
    judge_modes: ['inline', 'python'],
    severities: ['info', 'warning', 'critical'],
    drift_kinds: ['tone', 'format'],
  },
};

function freshDraft() {
  return {
    scoring: {
      // The backend serializes the tournament block under the `tournament`
      // key (zicato.epoch.lifecycle._scoring_to_dict), NOT `tournament_structure`
      // (that is the Python ScoringWeights attribute name). The view must read
      // this exact key — the stuck-on-Gauntlet bug was reading the wrong one.
      tournament: { structure: 'gauntlet', params: { field_size: 2, replicates: 1 } },
      overfitting: {
        enabled: true, holdout_fraction: 0.2, min_board_size_for_split: 8,
        rotate_holdout: true, restrict_proposer_visibility: true,
        random_baseline_every_n: 0, max_generations_per_contract: null,
        ladder: { enabled: true, threshold: null, budget: 16, noise_scale: 0 },
      },
      promote_margin: 0, pass_rate_monotonicity: false,
      pass_rate_monotonicity_scope: 'per_entry',
      pass_weight: 1, diff_complexity_weight: 0, diff_complexity_ceiling: 0,
      default_judge_weight: 1, plan_revision_weight: 0.5,
      task_failure_weight: 10, not_completed_weight: 50,
      severity_weights: { info: 1, warning: 3, critical: 10 },
      per_kind_weights: {}, per_judge_weights: {},
      namespace_weights: { 'drift:': 1, 'rubric:': -1 },
      namespace_monotonicity: { 'rubric:': true },
      block_on_containment_violation: false, block_on_gate_contradiction: false,
      regression_gate_enabled: false, regression_test_command: ['pytest', 'tests/', '-q'],
      regression_timeout_s: 600,
      proposer_quality: { best_of_n: 3, critique_enabled: true, screen_entries: 0, screen_veto_only: false },
      experiment_memory: { cross_epoch: false },
      telemetry_dialect: 'goldfive',
    },
    board: [
      { id: 'waffles', kind: 'single_turn', judges: [{ name: 'tone' }] },
      { id: 'picky', kind: 'multi_turn_emulated' },
    ],
    brief: 'be crisper',
    proposer_path: null,
    proposer: { agent_id: 'default', tools: ['edit'], skills: [{ name: 'compose', description: 'compose a proposer' }], has_custom_agent: false },
    holdout: { train_ids: ['waffles', 'picky'], holdout_ids: [] },
  };
}

let DRAFT = freshDraft();
const OP_CALLS = [];
let SLOTS = [];
let UNDO_HAS_HISTORY = false;

// The `preflight` op's canned result. Default is the REFUSE case with a report
// that carries no probed_points or window keys, so the read that tolerates
// their absence stays exercised; a test replaces it to reach the inert and
// margin-window branches.
const PREFLIGHT_REFUSE = {
  available: true, verdict: 'refuse', reason: '',
  report: {
    verdict: 'refuse', signal: 0.02, noise_floor_max_abs_delta: 0.14,
    noise_floor_runs: 5, degraded_mutation_id: 'style_rules',
  },
  noise_floor: { max_abs_delta: 0.14, runs: 5 },
};
let PREFLIGHT_RESULT = PREFLIGHT_REFUSE;

function envelope(patch) {
  return {
    draft: DRAFT,
    patch: patch || { op: 'noop', changed: {} },
    cost: { structure: DRAFT.scoring.tournament.structure, board_size: 2, holdout_size: 0,
      board_runs_per_round: 4, breakdown: [{ label: 'duel runs', runs: 4, detail: 'field 2 × board 2' }] },
    warnings: [],
    diff: { components: [], changed_components: [], rolls_epoch: false },
  };
}

function installBuilderFetch() {
  OP_CALLS.length = 0;
  DRAFT = freshDraft();
  SLOTS = [];
  UNDO_HAS_HISTORY = false;
  PREFLIGHT_RESULT = PREFLIGHT_REFUSE;
  globalThis.fetch = async (path, init) => {
    const body = init && init.body ? JSON.parse(init.body) : {};
    if (path === '/builder/config') return jsonRes(CONFIG);
    if (path.startsWith('/builder/draft')) {
      return jsonRes({ session: 'dashboard', draft: DRAFT, cost: envelope().cost, warnings: [], diff: envelope().diff, drafts: SLOTS.slice(),
        proposer_dirs: [{ name: 'critic-v2', path: '/ws/proposers/critic-v2' }] });
    }
    if (path === '/builder/op') {
      OP_CALLS.push(body);
      // the fork/compare lifecycle ops: maintain the slot list + return the
      // envelope with `drafts` (and `compare` for the compare op).
      if (body.op === 'fork' || body.op === 'switch' || body.op === 'list_drafts' || body.op === 'compare') {
        if (body.op === 'fork' && !SLOTS.includes(body.args.name)) SLOTS.push(body.args.name);
        const env = envelope({ op: body.op, changed: body.args.name ? { name: body.args.name } : body.args });
        env.drafts = SLOTS.slice();
        if (body.op === 'compare') {
          env.compare = {
            a: body.args.name_a, b: body.args.name_b,
            changed_components: ['scoring'],
            scoring: { promote_margin: { a: 0.01, b: 0.07 } },
            board: { added: [], removed: [], changed: [] },
            brief: { changed: false, a_chars: 3, b_chars: 3 },
            proposer: { changed: false, a: null, b: null },
          };
        }
        return jsonRes(env);
      }
      // the revert/undo lifecycle ops. `undo` reports "nothing to undo" via the
      // patch note when the mock's (empty) history has nothing to pop; a test
      // sets UNDO_HAS_HISTORY to exercise the restore path.
      if (body.op === 'revert_to_live') {
        return jsonRes(envelope({ op: 'revert_to_live', changed: { scoring: {} } }));
      }
      if (body.op === 'undo') {
        const env = envelope(UNDO_HAS_HISTORY
          ? { op: 'undo', changed: { scoring: {} } }
          : { op: 'undo', changed: {}, note: 'nothing to undo' });
        return jsonRes(env);
      }
      // the preflight READ op: the normal envelope plus the `preflight` result
      // (PREFLIGHT_RESULT — the REFUSE case by default) and the
      // just-measured-floor refuse warning.
      if (body.op === 'preflight') {
        const env = envelope({ op: 'preflight', changed: {} });
        env.preflight = PREFLIGHT_RESULT;
        env.warnings = [{
          code: 'margin_below_noise_floor', severity: 'refuse',
          message: 'promote_margin 0 does not clear the measured A/A noise floor 0.14…',
        }];
        return jsonRes(env);
      }
      // mutate the shared draft so the applied envelope is observably different.
      if (body.op === 'set_structure') DRAFT.scoring.tournament.structure = body.args.structure;
      if (body.op === 'set_telemetry_dialect' && body.args.dialect) DRAFT.scoring.telemetry_dialect = body.args.dialect;
      if (body.op === 'set_param') DRAFT.scoring.tournament.params[body.args.key] = body.args.value;
      if (body.op === 'set_gate' && body.args.promote_margin != null) DRAFT.scoring.promote_margin = body.args.promote_margin;
      if (body.op === 'set_holdout' && Array.isArray(body.args.tags)) {
        const held = new Set(body.args.tags);
        DRAFT.holdout.holdout_ids = DRAFT.board.map((b) => b.id).filter((id) => held.has(id));
        DRAFT.holdout.train_ids = DRAFT.board.map((b) => b.id).filter((id) => !held.has(id));
      }
      const env = envelope({ op: body.op, changed: body.args });
      if (body.op === 'set_structure') env.diff = { changed_components: ['structure', 'scoring'], rolls_epoch: true };
      return jsonRes(env);
    }
    if (path === '/builder/apply') {
      return jsonRes({ confirmed: !!body.confirm, rolled: !!body.confirm, components_changed: ['structure'],
        new_contract_hash: 'abc123def456ghi789', cost: envelope().cost, diff: envelope().diff, warnings: [] });
    }
    return jsonRes({});
  };
}

function jsonRes(obj) {
  return {
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  };
}

// install the minimal globals the view + chat touch (the shared console setup).
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

const builder = await import('../js/builder/model.js');
const popover = await import('../js/builder/popover.js');
const stream = await import('../js/builder/stream.js');
const { BuilderChat } = await import('../js/builder/chat.js');

// the view module holds shared module-level draft state; re-import is cached
// across tests, so each test re-mounts and drives through the public render().
const view = await import('../js/views/builder.js');

// helper: find descendants by class via the harness querySelectorAll.
function byClass(root, cls) { return root.querySelectorAll('[class]').filter((n) => n.classList.contains(cls)); }
function firstClass(root, cls) { return byClass(root, cls)[0] || null; }

// ── tests ─────────────────────────────────────────────────────────────

test('builder view: renders the left-rail sections + the structure section', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const railItems = byClass(host, 'dn-bld-railitem');
  assertEqual(railItems.length, 8, 'eight contract sections in the left rail (incl. Overfitting + Weights)');
  // the structure section leads with the five structure cards.
  const cards = byClass(host, 'dn-bld-card');
  assertEqual(cards.length, 5, 'five structure picker cards');
  // the live preview painted a cost meter from the op envelope's cost.
  assert(firstClass(host, 'dn-bld-cost'), 'the live preview shows a cost meter');
});

test('builder view: a structure pick calls /builder/op and applies the returned draft + diff', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const cards = byClass(host, 'dn-bld-card');
  // pick "swiss" (4th card) — click drives set_structure.
  const swiss = cards.find((c) => c.textContent.toLowerCase().includes('swiss'));
  assert(swiss, 'the swiss card is present');
  swiss.dispatchEvent(makeEvent('click'));
  await tick();
  assertEqual(OP_CALLS.length, 1, 'exactly one op call');
  assertEqual(OP_CALLS[0].op, 'set_structure', 'the op was set_structure');
  assertEqual(OP_CALLS[0].args.structure, 'swiss', 'the structure arg was swiss');
  // the applied diff rolls the epoch — the preview impact pill reflects it.
  const impact = firstClass(host, 'dn-bld-impact');
  assert(impact && impact.textContent.includes('rolls epoch'), 'the contract-impact pill shows the epoch roll');
  // ── the VISUAL selected state follows the picked structure ─────────────
  // After the set_structure result is applied, the highlighted card must be
  // the picked one — derived from the draft's `tournament.structure`, never
  // stuck on the first (Gauntlet) card. The assertion reads the DOM class
  // rather than the data.
  const onCards = byClass(host, 'dn-bld-card').filter((c) => c.classList.contains('dn-bld-card-on'));
  assertEqual(onCards.length, 1, 'exactly one structure card carries the selected class');
  assert(onCards[0].textContent.toLowerCase().includes('swiss'), 'the SELECTED card is Swiss, not Gauntlet');
  assertEqual(onCards[0].getAttribute('aria-pressed'), 'true', 'the selected card reports aria-pressed=true');
});

test('builder view: a draft whose structure is racing highlights the Racing card (not Gauntlet)', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  // Drive the draft to racing through the real op/apply path, then RE-RENDER
  // the mounted view. The data layer ends up racing (envelope applied); the
  // assertion is purely on the selected-state render — it must follow the
  // draft's `tournament.structure`, never stay pinned to the first card.
  const racing = byClass(host, 'dn-bld-card').find((c) => c.textContent.toLowerCase().includes('racing'));
  assert(racing, 'the racing card is present');
  racing.dispatchEvent(makeEvent('click'));
  await tick();
  // a second render() re-mounts the view against the now-racing shared draft,
  // mirroring a persisted racing draft re-opened — the highlight must persist.
  await view.render(host);
  const onCards = byClass(host, 'dn-bld-card').filter((c) => c.classList.contains('dn-bld-card-on'));
  assertEqual(onCards.length, 1, 'exactly one structure card is selected for the racing draft');
  assert(onCards[0].textContent.toLowerCase().includes('racing'), 'the racing draft highlights Racing, not Gauntlet');
});

test('builder view: a field param edit posts set_param with the numeric value', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  // go to the Field & noise section.
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Field'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  const num = byClass(host, 'dn-bld-num')[0];
  assert(num, 'a numeric field-param control is present');
  num.value = '3';
  num.dispatchEvent(makeEvent('change', { target: num }));
  await tick();
  const setParam = OP_CALLS.find((c) => c.op === 'set_param');
  assert(setParam, 'set_param was posted');
  assertEqual(setParam.args.value, 3, 'the value was sent as a number');
});

test('builder view: the telemetry-dialect select posts set_telemetry_dialect + shows the tier caption', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  // go to the Weights section (where the telemetry-dialect select lives).
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Weights'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  // the dialect control (its own row wrapper, distinct from the draft-slot
  // picker which also uses dn-bld-select) starts on goldfive; its quiet caption
  // states the goldfive tier.
  const wrap = byClass(host, 'dn-bld-dialect')[0];
  assert(wrap, 'the telemetry-dialect control is present in the Weights section');
  const sel = byClass(wrap, 'dn-bld-select')[0];
  assert(sel, 'the dialect select is present');
  const tier0 = byClass(wrap, 'dn-bld-dialect-tier')[0];
  assert(tier0 && tier0.textContent.includes('goldfive'), 'the caption states the goldfive tier');
  // pick adk_events — change drives set_telemetry_dialect { dialect: 'adk_events' }.
  sel.value = 'adk_events';
  sel.dispatchEvent(makeEvent('change', { target: sel }));
  await tick();
  const dl = OP_CALLS.find((c) => c.op === 'set_telemetry_dialect' && c.args.dialect);
  assert(dl, 'set_telemetry_dialect was posted with a dialect arg');
  assertEqual(dl.args.dialect, 'adk_events', 'the dialect arg was adk_events');
  // the applied draft re-renders the caption to the adk_events capability tier.
  const tier1 = byClass(host, 'dn-bld-dialect-tier')[0];
  assert(tier1 && tier1.textContent.includes('no in-process drift instruments'),
    'the caption re-renders to the adk_events tier after the change');
});

test('builder view: a holdout toggle posts set_holdout tags and re-renders the split strip', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Board'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  const toggle = byClass(host, 'dn-bld-holdtoggle')[0];
  assert(toggle, 'a holdout toggle is present');
  toggle.dispatchEvent(makeEvent('click'));
  await tick();
  const setHoldout = OP_CALLS.find((c) => c.op === 'set_holdout');
  assert(setHoldout && Array.isArray(setHoldout.args.tags), 'set_holdout posted with tags');
  assertEqual(setHoldout.args.tags.length, 1, 'one entry held out');
  // The hash-derived split's own size rides the SAME op, from the same page.
  const frac = byAria(host, 'dn-bld-num', 'Holdout fraction');
  assert(frac, 'the holdout-fraction control renders');
  frac.value = '0.4';
  frac.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.fraction === 0.4),
    'the holdout-fraction control posts set_holdout {fraction}');
});

test('builder view: the Review section dry-run + apply call /builder/apply with the right confirm', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Review'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  let applyBody = null;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (path === '/builder/apply') applyBody = JSON.parse(init.body);
    return realFetch(path, init);
  };
  // dry-run → confirm:false
  const dry = byClass(host, 'dn-bld-btn-dry')[0];
  dry.dispatchEvent(makeEvent('click'));
  await tick();
  assertEqual(applyBody.confirm, false, 'dry-run sends confirm:false');
  // apply needs an explicit second click (the confirm gesture) → confirm:true
  const apply = byClass(host, 'dn-bld-btn-apply')[0];
  apply.dispatchEvent(makeEvent('click'));   // arms the confirm
  await tick();
  apply.dispatchEvent(makeEvent('click'));   // confirms
  await tick();
  assertEqual(applyBody.confirm, true, 'the confirmed apply sends confirm:true');
});

test('info popover: opens on focus, exposes aria, closes on Escape', () => {
  const pop = popover.infoPopover({ title: 'Field size', def: '2', body: 'how many challengers' });
  const trigger = pop.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-info'))[0];
  const panel = pop.querySelectorAll('[role]').filter((n) => n.getAttribute('role') === 'note')[0];
  assert(trigger && panel, 'the popover has a trigger + a note panel');
  assertEqual(trigger.getAttribute('aria-expanded'), 'false', 'starts collapsed');
  assertEqual(panel.getAttribute('aria-hidden'), 'true', 'panel hidden initially');
  trigger.dispatchEvent(makeEvent('focus'));
  assertEqual(trigger.getAttribute('aria-expanded'), 'true', 'focus opens it');
  assertEqual(panel.getAttribute('aria-hidden'), 'false', 'panel shown on open');
  trigger.dispatchEvent(makeEvent('keydown', { key: 'Escape' }));
  assertEqual(panel.getAttribute('aria-hidden'), 'true', 'Escape closes it');
});

test('chat pane: graceful-degrade disables the input when chat_enabled is false', () => {
  const chat = new BuilderChat({ config: { chat_enabled: false, agent: { model: '' } } });
  const input = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-input'))[0];
  const send = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-send'))[0];
  assert(input.hasAttribute('disabled'), 'the input is disabled');
  assert(send.hasAttribute('disabled'), 'the send button is disabled');
  assert(chat.node.textContent.includes('models.builder'), 'the degrade hint points at the builder role');
});

test('chat pane: header identifies the configured role without engine details', () => {
  const chat = new BuilderChat({ config: { chat_enabled: true, agent: { model: 'house-model-x' } } });
  const m = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-model'))[0];
  assertEqual(m.textContent, 'builder role', 'the role is shown');
});

test('chat pane: resize clamps to [min,max] + persists + reflows the host via onWidthChange', () => {
  globalThis.window.localStorage.clear();
  let lastWidth = null;
  const chat = new BuilderChat({
    config: { chat_enabled: true, agent: { model: 'm' } },
    min: builder.CHAT_MIN, max: builder.CHAT_MAX, initialWidth: 340,
    onWidthChange: (w) => { lastWidth = w; builder.persistChatWidth(w); },
  });
  const handle = chat.node.querySelectorAll('[role]').filter((n) => n.getAttribute('role') === 'separator')[0];
  // a keyboard nudge widens (ArrowLeft) — the host gets the reflow callback.
  handle.dispatchEvent(makeEvent('keydown', { key: 'ArrowLeft' }));
  assert(lastWidth > 340, 'ArrowLeft widened the pane');
  // Home jumps to max; a beyond-max value clamps.
  handle.dispatchEvent(makeEvent('keydown', { key: 'Home' }));
  assertEqual(lastWidth, builder.CHAT_MAX, 'Home clamps to max');
  assertEqual(chat.width(), builder.CHAT_MAX, 'the pane width is the clamped max');
  // persisted + re-readable through the model helpers (clamped on read too).
  assertEqual(builder.readChatWidth(), builder.CHAT_MAX, 'the width persisted to localStorage');
  // a synthetic drag of the LEFT edge: moving left widens, capped at max.
  handle.dispatchEvent(makeEvent('pointerdown', { clientX: 500, pointerId: 1 }));
  handle.dispatchEvent(makeEvent('pointermove', { clientX: 200 }));
  handle.dispatchEvent(makeEvent('pointerup', { clientX: 200 }));
  assertEqual(chat.width(), builder.CHAT_MAX, 'a wide drag stays clamped at max (no overflow)');
});

test('chat pane: collapse toggles the strip + persists', () => {
  globalThis.window.localStorage.clear();
  let collapsedSeen = null;
  const chat = new BuilderChat({
    config: { chat_enabled: true, agent: { model: 'm' } },
    onCollapse: (c) => { collapsedSeen = c; builder.persistChatCollapsed(c); },
  });
  assertEqual(chat.collapsed(), false, 'starts expanded');
  const btn = chat.node.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-bld-chat-collapse'))[0];
  btn.dispatchEvent(makeEvent('click'));
  assertEqual(chat.collapsed(), true, 'collapses on click');
  assertEqual(collapsedSeen, true, 'the host was told to reflow');
  assert(chat.node.classList.contains('dn-bld-chat-collapsed'), 'the collapsed class is applied (thin strip)');
  assertEqual(builder.readChatCollapsed(), true, 'collapse state persisted');
});

test('chat stream: a patch frame is dispatched to onPatch (the shared-draft update)', async () => {
  const frames = [
    { type: 'token', text: 'I will ' },
    { type: 'token', text: 'set racing.' },
    { type: 'tool', name: 'set_structure', args: { structure: 'racing' } },
    { type: 'patch', patch: { op: 'set_structure', changed: { structure: { from: 'gauntlet', to: 'racing' } } },
      cost: { board_runs_per_round: 9, breakdown: [] }, warnings: [], diff: { changed_components: ['structure'], rolls_epoch: true } },
    { type: 'done' },
  ];
  let tokens = '';
  let tool = null;
  let patch = null;
  await stream.streamChat('/builder/chat', { message: 'use racing' }, {
    frames,
    onToken: (t) => { tokens += t; },
    onTool: (n) => { tool = n; },
    onPatch: (p) => { patch = p; },
  });
  assertEqual(tokens, 'I will set racing.', 'token deltas streamed in order');
  assertEqual(tool, 'set_structure', 'the tool step was surfaced');
  assert(patch && patch.diff.rolls_epoch, 'the patch frame carried the contract diff');
  assertEqual(patch.patch.op, 'set_structure', 'the patch op is the structure change');
});

test('chat stream: an error frame stops the stream and calls onError', async () => {
  const frames = [
    { type: 'token', text: 'hmm' },
    { type: 'error', message: 'configure builder.json to enable chat' },
    { type: 'token', text: 'should-not-arrive' },
  ];
  let tokens = '';
  let err = null;
  await stream.streamChat('/builder/chat', { message: 'x' }, {
    frames, onToken: (t) => { tokens += t; }, onError: (m) => { err = m; },
  });
  assertEqual(tokens, 'hmm', 'streaming halts at the error frame');
  assert(err && err.includes('builder.json'), 'the graceful-degrade error message surfaced');
});

test('chat pane: is a 3-row flex column with the message log flex:1 and the composer pinned at the BOTTOM', () => {
  // The pane must be a docked full-height column: header pinned top, message
  // log the single growable (flex:1) middle child, composer pinned bottom — so
  // even an empty conversation puts the input at the bottom edge, never a short
  // content-sized stub. Assert the structural layout (DOM order + the classes
  // the CSS flex rules key on), since the mock DOM does not compute styles.
  const chat = new BuilderChat({ config: { chat_enabled: true, agent: { model: 'm' } } });
  // the pane → [handle, body, strip]
  const body = chat.node.children.find((n) => n.classList.contains('dn-bld-chat-body'));
  assert(body, 'the pane has a flex body column');
  // the body's three rows, in order: head · log · composer.
  const rows = body.children;
  assertEqual(rows.length, 3, 'the body is a 3-row flex column (header · log · composer)');
  assert(rows[0].classList.contains('dn-bld-chat-head'), 'row 1 is the header (pinned top)');
  assert(rows[1].classList.contains('dn-bld-chat-log'), 'row 2 is the scrollable message log');
  assert(rows[2].classList.contains('dn-bld-chat-composer'), 'row 3 is the composer, LAST → pinned at the bottom');
  // the message log is the growable child the CSS gives flex:1 + overflow-y:auto,
  // so a long conversation scrolls INSIDE the pane rather than moving the page.
  assertEqual(chat._log, rows[1], 'the streamed message log is the flex:1 middle row');
  // the composer holds the input + send (the bottom-pinned 3rd row).
  assert(rows[2].children.some((n) => n.classList.contains('dn-bld-chat-input')), 'the composer carries the input');
  assert(rows[2].children.some((n) => n.classList.contains('dn-bld-chat-send')), 'the composer carries the send button');
});

test('chat pane: the chat-disabled (degrade) state keeps the full-height frame — composer still pinned at the bottom', () => {
  // The "configure builder.json" degrade state must NOT collapse the pane: the
  // degrade notice sits INSIDE the flex:1 log row, so the composer stays pinned
  // at the bottom of the full-height frame just like the enabled state.
  const chat = new BuilderChat({ config: { chat_enabled: false, agent: { model: '' } } });
  const body = chat.node.children.find((n) => n.classList.contains('dn-bld-chat-body'));
  const rows = body.children;
  assertEqual(rows.length, 3, 'the disabled pane keeps the 3-row frame');
  assert(rows[2].classList.contains('dn-bld-chat-composer'), 'the composer is still the LAST row (bottom-pinned)');
  // the degrade notice rides inside the (flex:1) log rather than replacing the frame.
  const log = rows[1];
  assert(log.classList.contains('dn-bld-chat-log'), 'the middle row is still the flex:1 log');
  assert(log.children.some((n) => n.classList.contains('dn-bld-chat-degrade')), 'the degrade notice sits inside the growable log');
});

test('builder view: the work column reflows the chat width as a CSS var (no overlap)', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const root = host.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-builder'))[0];
  assert(root, 'the builder root is present');
  // the chat-width CSS var is stamped so the grid template reflows the work
  // column to the remaining width (real layout, no overlap/clipping).
  const varVal = root.style._props['--dn-bld-chat'];
  assert(varVal && /px$/.test(varVal), 'the chat-width CSS var is stamped in px on the root');
});

// ── structure-card glyphs (ported from the approved mockup) ───────────

test('structure glyphs: each of the five structures renders a crisp theme-token SVG (currentColor only)', () => {
  for (const id of ['gauntlet', 'swiss', 'single_elim', 'double_elim', 'racing']) {
    const svg = builder.structureGlyphSvg(id);
    assert(svg && svg.localName === 'svg', `${id} → an svg glyph`);
    assertEqual(svg.getAttribute('viewBox'), '0 0 24 24', `${id} glyph is a crisp 24×24 viewBox`);
    assertEqual(svg.getAttribute('class'), 'dn-bld-cardglyph', `${id} glyph carries the card-glyph class`);
    // theme tokens ONLY — every stroke/fill is currentColor, never a hex colour.
    for (const n of allDesc(svg)) {
      const stroke = n.getAttribute('stroke');
      const fill = n.getAttribute('fill');
      if (stroke && stroke !== 'none') assertEqual(stroke, 'currentColor', `${id}: stroke is the currentColor token`);
      if (fill && fill !== 'none') assertEqual(fill, 'currentColor', `${id}: fill is the currentColor token`);
      assert(!/#[0-9a-fA-F]{3,6}/.test(`${stroke} ${fill}`), `${id}: no hardcoded hex colour`);
    }
  }
});

test('structure glyphs: gauntlet is ●—● (two dots + a join), racing is a 3→2→1 staggered funnel', () => {
  // gauntlet: two duel dots joined by a short line.
  const g = builder.structureGlyphSvg('gauntlet');
  const gDots = allDesc(g).filter((n) => n.localName === 'circle');
  const gPaths = allDesc(g).filter((n) => n.localName === 'path');
  assertEqual(gDots.length, 2, 'gauntlet draws the two duel dots');
  assertEqual(gPaths.length, 1, 'gauntlet draws the single joining line');

  // racing: a staggered funnel of dots — 3 + 2 + 1 = 6 dots, two cut arms faded.
  const r = builder.structureGlyphSvg('racing');
  const rDots = allDesc(r).filter((n) => n.localName === 'circle');
  assertEqual(rDots.length, 6, 'racing draws a 3→2→1 funnel of dots');
  const faded = rDots.filter((n) => n.getAttribute('fill-opacity') != null);
  assertEqual(faded.length, 2, 'racing fades the two cut arms');

  // swiss: three stacked ranking lines (no dots).
  const s = builder.structureGlyphSvg('swiss');
  const sPaths = allDesc(s).filter((n) => n.localName === 'path');
  assertEqual(sPaths.length, 3, 'swiss draws three stacked ranking lines');

  // double_elim carries the extra losers'-lane line over single_elim.
  const se = allDesc(builder.structureGlyphSvg('single_elim')).filter((n) => n.localName === 'path').length;
  const de = allDesc(builder.structureGlyphSvg('double_elim')).filter((n) => n.localName === 'path').length;
  assert(de > se, 'double-elim adds the extra losers-lane line over single-elim');
});

function allDesc(node) {
  const out = [];
  const walk = (n) => { for (const c of n.children) { out.push(c); walk(c); } };
  walk(node);
  return out;
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

// ── full-height docked-workspace layout (CSS-level guard) ─────────────
//
// The mock DOM does not compute styles, so the structural tests above assert
// the DOM (3-row flex, composer last). These read the CSS source to lock the
// rules that make the workspace full-height: the builder root gets a DEFINITE
// height from the viewport (so the chat pane stretches to a docked column, not
// a content-sized stub) and the message log is the flex:1 growable child.

const _builderCss = (await import('node:fs'))
  .readFileSync(new URL('../css/console.css', import.meta.url), 'utf8')
  .replace(/\n/g, ' ');
function readBuilderCss() { return _builderCss; }

test('builder CSS: the builder root is a DEFINITE full-height frame (viewport − top bar − viewhost padding)', () => {
  const css = readBuilderCss();
  const block = /\.dn-builder\s*\{([^}]*)\}/.exec(css);
  assert(block, 'the .dn-builder rule exists');
  const decl = block[1];
  // a definite height computed from the viewport (NOT content-driven) so the
  // grid children stretch to a docked full-height column.
  assert(/height:\s*calc\(100vh/.test(decl), 'the builder root takes a definite viewport-derived height');
  assert(/align-items:\s*stretch/.test(decl), 'the grid stretches its children (work column + chat) to that height');
});

test('builder CSS: the chat log is the flex:1 scrollable middle row + the composer is a bottom border-top row', () => {
  const css = readBuilderCss();
  const log = /\.dn-bld-chat-log\s*\{([^}]*)\}/.exec(css);
  assert(log && /flex:\s*1/.test(log[1]), 'the message log is the flex:1 growable child');
  assert(log && /overflow-y:\s*auto/.test(log[1]), 'the message log scrolls internally (long chats scroll in-pane)');
  const composer = /\.dn-bld-chat-composer\s*\{([^}]*)\}/.exec(css);
  assert(composer && /border-top/.test(composer[1]), 'the composer is the bottom-pinned row (border-top divider)');
  // the work column children scroll internally so the full-height frame never clips.
  const cols = /\.dn-bld-center,\s*\.dn-bld-preview\s*\{([^}]*)\}/.exec(css);
  assert(cols && /overflow-y:\s*auto/.test(cols[1]), 'the center + preview columns scroll internally');
});

// ── estimateCost: the per-structure default-replicates twin ───────────
//
// The JS estimateCost is the exact twin of operations.py's estimate_cost. The
// under-reporting bug class: when `replicates` is UNSET the meter must default
// to the STRUCTURE's own default (swiss / elim default to 2) rather than to a
// flat 1, so
// the dashboard preview matches the Python estimator. These mirror the Python
// cost tests (test_cost_swiss_unset_replicates_uses_strategy_default_two, the
// explicit-override test, and the per-structure-default pin) number-for-number.

test('estimateCost: swiss with UNSET replicates uses the strategy default of 2 (not a flat 1)', () => {
  // field_size 4, board 8, holdout 0, replicates UNSET. The Python estimator
  // reports rounds_n 4 × pairings 2 × replicates 2 × board 8 = 128 — the SAME
  // number this JS twin must produce. A flat-1 default would report 64 (half).
  const est = builder.estimateCost('swiss', { field_size: 4 }, 8, 0);
  assertEqual(est.board_runs_per_round, 128, 'swiss unset replicates → 128 (the py estimator value, not 64)');
  const swissLine = est.breakdown.find((l) => l.label === 'swiss-pairing runs');
  assert(swissLine && /replicates 2/.test(swissLine.detail), 'the breakdown detail shows replicates 2');
});

test('estimateCost: an explicit replicates is honored verbatim over the structure default', () => {
  const est1 = builder.estimateCost('swiss', { field_size: 4, replicates: 1 }, 8, 0);
  assertEqual(est1.board_runs_per_round, 64, 'explicit replicates=1 → 64 (4 × 2 × 1 × 8)');
  const est3 = builder.estimateCost('swiss', { field_size: 4, replicates: 3 }, 8, 0);
  assertEqual(est3.board_runs_per_round, 192, 'explicit replicates=3 → 192 (4 × 2 × 3 × 8)');
});

// ── full knob coverage: every rail section drives its ops ──────────────

// helper: mount the view and switch to the rail section whose label matches.
async function mountAt(label) {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes(label));
  assert(rail, `the ${label} rail item exists`);
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  return host;
}

function byAria(host, cls, aria) {
  return byClass(host, cls).find((n) => n.getAttribute('aria-label') === aria);
}

test('builder view: the Overfitting section drives set_holdout (ladder, placebo, rotation)', async () => {
  const host = await mountAt('Overfitting');
  // the placebo cadence numeric posts random_baseline_every_n.
  const placebo = byAria(host, 'dn-bld-num', 'Random baseline every N rounds');
  assert(placebo, 'the placebo-cadence control renders');
  placebo.value = '5';
  placebo.dispatchEvent(makeEvent('change'));
  await tick();
  let call = OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.random_baseline_every_n === 5);
  assert(call, 'the placebo cadence posts set_holdout {random_baseline_every_n}');
  // the ladder budget numeric posts a PARTIAL ladder mapping.
  const budget = byAria(host, 'dn-bld-num', 'Ladder budget');
  budget.value = '8';
  budget.dispatchEvent(makeEvent('change'));
  await tick();
  call = OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.ladder && c.args.ladder.budget === 8);
  assert(call, 'the ladder budget posts set_holdout {ladder:{budget}}');
  // the rotation checkbox posts rotate_holdout.
  const rotate = byAria(host, 'dn-bld-check', 'Rotate holdout');
  rotate.checked = false;
  rotate.dispatchEvent(makeEvent('change'));
  await tick();
  call = OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.rotate_holdout === false);
  assert(call, 'the rotation toggle posts set_holdout {rotate_holdout:false}');
});

// The remaining set_holdout knobs, one EXPLICIT assertion each — the master
// switch, the split floor, the visibility restriction, and the two ladder
// subkeys the budget row does not cover. A ladder sibling's row can never
// stand in for another: each subkey posts its own partial mapping.
test('builder view: the Overfitting section covers the split floor, visibility, and the remaining ladder subkeys', async () => {
  const host = await mountAt('Overfitting');
  const guard = byAria(host, 'dn-bld-check', 'Overfitting guard enabled');
  guard.checked = false;
  guard.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.enabled === false),
    'the master switch posts set_holdout {enabled:false}');

  const floor = byAria(host, 'dn-bld-num', 'Min board size for split');
  floor.value = '9';
  floor.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.min_board_size_for_split === 9),
    'the split floor posts set_holdout {min_board_size_for_split}');

  const vis = byAria(host, 'dn-bld-check', 'Restrict proposer visibility');
  vis.checked = false;
  vis.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.restrict_proposer_visibility === false),
    'the visibility toggle posts set_holdout {restrict_proposer_visibility:false}');

  const gov = byAria(host, 'dn-bld-check', 'Ladder governor enabled');
  gov.checked = false;
  gov.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.ladder && c.args.ladder.enabled === false),
    'the ladder switch posts set_holdout {ladder:{enabled:false}}');

  const noise = byAria(host, 'dn-bld-num', 'Ladder noise scale');
  noise.value = '0.05';
  noise.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.ladder && c.args.ladder.noise_scale === 0.05),
    'the noise-scale row posts set_holdout {ladder:{noise_scale}}');

  // The release threshold: a pinned float, then the NEGATIVE reset that the
  // op reads as the mapping's real null (auto — derive from promote_margin).
  const thr = byAria(host, 'dn-bld-num', 'Ladder release threshold');
  assert(thr, 'the ladder release-threshold control renders');
  thr.value = '0.08';
  thr.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.ladder && c.args.ladder.threshold === 0.08),
    'the release threshold posts set_holdout {ladder:{threshold}}');
  thr.value = '-1';
  thr.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.ladder && c.args.ladder.threshold === null),
    'a negative release threshold posts the null that resets it to auto');
});

test('builder view: the Weights section drives set_weights + set_namespace_weights (full mapping)', async () => {
  const host = await mountAt('Weights');
  // Every measured channel — drift included — is a namespace weight; the
  // section's own scalar rows are the pass term and the within-channel shapes.
  assert(!byAria(host, 'dn-bld-num', 'Drift weight'), 'drift has no scalar row of its own');
  // a namespace weight edit posts the WHOLE mapping with the one key changed.
  const rubric = byAria(host, 'dn-bld-num', 'Namespace weight rubric:');
  assert(rubric, 'per-namespace weight controls render');
  rubric.value = '-2';
  rubric.dispatchEvent(makeEvent('change'));
  await tick();
  const nsCall = OP_CALLS.find((c) => c.op === 'set_namespace_weights' && c.args.namespace_weights);
  assert(nsCall, 'a namespace edit posts set_namespace_weights');
  assertEqual(nsCall.args.namespace_weights['rubric:'], -2, 'the edited key carries the new value');
  assertEqual(nsCall.args.namespace_weights['drift:'], 1, 'the untouched keys ride along (wholesale mapping)');
  // the parsimony term posts diff_complexity_weight.
  const mdl = byAria(host, 'dn-bld-num', 'Diff complexity weight');
  mdl.value = '0.01';
  mdl.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_namespace_weights' && c.args.diff_complexity_weight === 0.01),
    'the MDL term posts set_namespace_weights {diff_complexity_weight}');
  // the paired parsimony CEILING posts diff_complexity_ceiling.
  const ceil = byAria(host, 'dn-bld-num', 'Diff complexity ceiling');
  ceil.value = '10';
  ceil.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_namespace_weights' && c.args.diff_complexity_ceiling === 10),
    'the parsimony ceiling posts set_namespace_weights {diff_complexity_ceiling}');
});

test('builder view: the Proposer section drives set_proposer_quality + set_experiment_memory', async () => {
  const host = await mountAt('Proposer');
  const bestOf = byAria(host, 'dn-bld-num', 'Best of N');
  assert(bestOf, 'the best-of-N control renders');
  bestOf.value = '5';
  bestOf.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_proposer_quality' && c.args.best_of_n === 5),
    'best-of-N posts set_proposer_quality');
  const critique = byAria(host, 'dn-bld-check', 'Critique enabled');
  critique.checked = false;
  critique.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_proposer_quality' && c.args.critique_enabled === false),
    'the critique toggle posts set_proposer_quality');
  const exemplars = byAria(host, 'dn-bld-num', 'Process exemplars');
  assert(exemplars, 'the process-exemplars control renders');
  exemplars.value = '2';
  exemplars.dispatchEvent(makeEvent('change'));
  await tick();
  const exCall = OP_CALLS.find((c) => c.op === 'set_proposer_quality' && 'process_exemplars' in c.args);
  assert(exCall && exCall.args.process_exemplars === 2,
    'the process-exemplars count posts set_proposer_quality {process_exemplars:2} — exact op+args');
  const recombine = byAria(host, 'dn-bld-check', 'Recombination slot');
  assert(recombine, 'the recombination-slot control renders');
  assert(!recombine.checked, 'the recombination slot is unchecked at the False default');
  recombine.checked = true;
  recombine.dispatchEvent(makeEvent('change'));
  await tick();
  const recCall = OP_CALLS.find((c) => c.op === 'set_proposer_quality' && 'recombine' in c.args);
  assert(recCall && recCall.args.recombine === true,
    'the recombination toggle posts set_proposer_quality {recombine:true} — exact op+args');
  const merge = byAria(host, 'dn-bld-check', 'LLM-guided merge');
  assert(merge, 'the LLM-guided-merge control renders');
  assert(!merge.checked, 'the merge mode is unchecked at the "mechanical" default');
  merge.checked = true;
  merge.dispatchEvent(makeEvent('change'));
  await tick();
  const mergeCall = OP_CALLS.find((c) => c.op === 'set_proposer_quality' && 'recombine_merge' in c.args);
  assert(mergeCall && mergeCall.args.recombine_merge === 'llm',
    'the merge toggle posts set_proposer_quality {recombine_merge:"llm"} — exact op+args');
  const genealogy = byAria(host, 'dn-bld-num', 'Genealogy');
  assert(genealogy, 'the genealogy control renders');
  genealogy.value = '4';
  genealogy.dispatchEvent(makeEvent('change'));
  await tick();
  const genCall = OP_CALLS.find((c) => c.op === 'set_proposer_quality' && 'genealogy' in c.args);
  assert(genCall && genCall.args.genealogy === 4,
    'the genealogy count posts set_proposer_quality {genealogy:4} — exact op+args');
  const calibration = byAria(host, 'dn-bld-num', 'Calibration feedback');
  assert(calibration, 'the calibration-feedback control renders');
  calibration.value = '5';
  calibration.dispatchEvent(makeEvent('change'));
  await tick();
  const calCall = OP_CALLS.find((c) => c.op === 'set_proposer_quality' && 'calibration_feedback' in c.args);
  assert(calCall && calCall.args.calibration_feedback === 5,
    'the calibration count posts set_proposer_quality {calibration_feedback:5} — exact op+args');
  const mem = byAria(host, 'dn-bld-check', 'Cross-epoch experiment memory');
  mem.checked = true;
  mem.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_experiment_memory' && c.args.cross_epoch === true),
    'the memory toggle posts set_experiment_memory');
});

test('builder view: a no-op re-render of the Proposer section rebuilds ZERO DOM (digest-gated identity)', async () => {
  const host = await mountAt('Proposer');
  const center = firstClass(host, 'dn-bld-center');
  assert(center, 'the center pane is present');
  const secBefore = firstClass(center, 'dn-section');
  const writes = center.innerHTMLWriteCount();
  // re-click the ALREADY-active Proposer rail item → a full render pass with an
  // IDENTICAL center digest must not rebuild the section (the render-discipline
  // no-op guardrail — the recombination row must not perturb the digest).
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Proposer'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  const secAfter = firstClass(center, 'dn-section');
  assert(secBefore === secAfter, 'a no-op section re-render preserves node identity (zero rebuild)');
  assertEqual(center.innerHTMLWriteCount(), writes, 'a no-op section re-render writes ZERO additional DOM');
});

test('builder view: the Field & noise section drives set_screening (tryout knobs)', async () => {
  const host = await mountAt('Field & noise');
  // the candidate-screen numeric posts set_screening {entries} (the field is
  // screen_entries but the op arg is `entries`).
  const screen = byAria(host, 'dn-bld-num', 'Candidate screen entries');
  assert(screen, 'the candidate-screen control renders');
  screen.value = '3';
  screen.dispatchEvent(makeEvent('change'));
  await tick();
  const entriesCall = OP_CALLS.find((c) => c.op === 'set_screening' && 'entries' in c.args);
  assert(entriesCall && entriesCall.args.entries === 3,
    'the candidate-screen count posts set_screening {entries:3} — exact op+args');
  // the veto-only checkbox posts set_screening {veto_only}.
  const veto = byAria(host, 'dn-bld-check', 'Screen veto-only');
  assert(veto, 'the screen veto-only control renders');
  veto.checked = true;
  veto.dispatchEvent(makeEvent('change'));
  await tick();
  const vetoCall = OP_CALLS.find((c) => c.op === 'set_screening' && 'veto_only' in c.args);
  assert(vetoCall && vetoCall.args.veto_only === true,
    'the veto-only toggle posts set_screening {veto_only:true} — exact op+args');
});

test('builder view: the Gate section gains scope + blocking + regression controls', async () => {
  const host = await mountAt('Gate');
  // the monotonicity-scope select posts set_gate {monotonicity_scope}.
  const scope = byAria(host, 'dn-bld-select', 'Monotonicity scope');
  assert(scope, 'the scope select renders');
  scope.value = 'aggregate';
  scope.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.monotonicity_scope === 'aggregate'),
    'the scope select posts set_gate');
  const contain = byAria(host, 'dn-bld-check', 'Block on containment violation');
  contain.checked = true;
  contain.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.block_on_containment_violation === true),
    'the containment block posts set_gate');
  const contradiction = byAria(host, 'dn-bld-check', 'Block on gate contradiction');
  contradiction.checked = true;
  contradiction.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.block_on_gate_contradiction === true),
    'the gate-contradiction block posts set_gate {block_on_gate_contradiction:true} — exact op+args');
  const regTimeout = byAria(host, 'dn-bld-num', 'Regression timeout seconds');
  regTimeout.value = '90';
  regTimeout.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.regression_timeout_s === 90),
    'the regression timeout posts set_gate');
  // the regression command splits into an argv list.
  const regCmd = byAria(host, 'dn-bld-text', 'Regression test command');
  regCmd.value = 'python -m unittest discover';
  regCmd.dispatchEvent(makeEvent('change'));
  await tick();
  const cmdCall = OP_CALLS.find((c) => c.op === 'set_gate' && Array.isArray(c.args.regression_test_command));
  assert(cmdCall, 'the regression command posts set_gate');
  assertEqual(cmdCall.args.regression_test_command.join('|'), 'python|-m|unittest|discover', 'whitespace-split argv');
});

// The holdout CONFIRMATION's own bounds (issue #118) — a working gate rule
// that had no builder path at all until the registry's exemption guard
// named it. Plus the two gate switches the section never asserted.
test('builder view: the Gate section drives the holdout confirmation bounds', async () => {
  const host = await mountAt('Gate');
  const mono = byAria(host, 'dn-bld-check', 'Pass-rate monotonicity');
  mono.checked = true;
  mono.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.monotonicity === true),
    'the monotonicity switch posts set_gate {monotonicity:true}');

  const regOn = byAria(host, 'dn-bld-check', 'Regression gate enabled');
  regOn.checked = true;
  regOn.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.regression_gate_enabled === true),
    'the regression-gate switch posts set_gate {regression_gate_enabled:true}');

  const budget = byAria(host, 'dn-bld-num', 'Holdout entry regression budget');
  assert(budget, 'the holdout entry-regression-budget control renders');
  budget.value = '1';
  budget.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.holdout_entry_regression_budget === 1),
    'the budget row posts set_gate {holdout_entry_regression_budget}');

  // A pinned bound, then the NEGATIVE reset to auto (reuse promote_margin) —
  // the op reserves null for "leave unchanged", so negative is the token.
  const margin = byAria(host, 'dn-bld-num', 'Holdout margin');
  assert(margin, 'the holdout-margin control renders');
  margin.value = '0.04';
  margin.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.holdout_margin === 0.04),
    'the margin row posts set_gate {holdout_margin}');
  margin.value = '-1';
  margin.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_gate' && c.args.holdout_margin === -1),
    'a negative holdout margin posts the reset the op reads as auto');
});

test('builder view: the evidence-gate params join Field & noise; threshold 0 REMOVES the key', async () => {
  const host = await mountAt('Field');
  const thr = byAria(host, 'dn-bld-num', 'Evidence-gate threshold');
  const budget = byAria(host, 'dn-bld-num', 'Evidence replicate budget');
  assert(thr, 'the evidence-gate threshold control renders');
  assert(budget, 'the evidence replicate budget control renders');
  thr.value = '0.8';
  thr.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_param' && c.args.key === 'promote_confidence_threshold' && c.args.value === 0.8),
    'a non-zero threshold posts set_param with the number');
  thr.value = '0';
  thr.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_param' && c.args.key === 'promote_confidence_threshold' && c.args.value === null),
    'threshold 0 posts value:null — the key is REMOVED so the unset gate hashes identically');
  budget.value = '32';
  budget.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_param' && c.args.key === 'promote_confidence_replicates' && c.args.value === 32),
    'the replicate budget posts set_param');
});

// ── the fork/compare lifecycle (slot picker + compare view) ────────────

test('builder view: the rail slot strip forks the working draft and switches slots', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  // the compact picker rides at the bottom of the rail.
  const strip = firstClass(host, 'dn-bld-slots');
  assert(strip, 'the draft-slot strip renders in the rail');
  // fork: type a name, click Fork → POST op fork {name}.
  const nameIn = byAria(host, 'dn-bld-slots-name', 'Fork name');
  assert(nameIn, 'the fork-name input renders');
  nameIn.value = 'variant-a';
  const forkBtn = firstClass(host, 'dn-bld-btn-fork');
  forkBtn.dispatchEvent(makeEvent('click'));
  await tick();
  const forkCall = OP_CALLS.find((c) => c.op === 'fork');
  assert(forkCall && forkCall.args.name === 'variant-a', 'fork posted with the typed name');
  // the slot select now lists the fork and marks it active.
  const pick = byAria(host, 'dn-bld-select', 'Draft slot') || firstClass(host, 'dn-bld-slots-pick');
  assert(pick, 'the slot select renders');
  assert(pick.children.some((o) => o.getAttribute('value') === 'variant-a'), 'the fork appears in the picker');
  // fork made variant-a active already; add a second slot and switch back.
  const nameIn2 = byAria(host, 'dn-bld-slots-name', 'Fork name');
  nameIn2.value = 'variant-b';
  firstClass(host, 'dn-bld-btn-fork').dispatchEvent(makeEvent('click'));
  await tick();
  const pick2 = byAria(host, 'dn-bld-select', 'Draft slot') || firstClass(host, 'dn-bld-slots-pick');
  pick2.value = 'variant-a';
  pick2.dispatchEvent(makeEvent('change'));
  await tick();
  const switchCall = OP_CALLS.find((c) => c.op === 'switch');
  assert(switchCall && switchCall.args.name === 'variant-a', 'the picker posts op switch');
});

test('builder view: the Review compare panel posts op compare and renders the keyed diff', async () => {
  const host = await mountAt('Review');
  // seed a slot so the selects have a named operand.
  const nameIn = byAria(host, 'dn-bld-slots-name', 'Fork name');
  nameIn.value = 'tuned';
  firstClass(host, 'dn-bld-btn-fork').dispatchEvent(makeEvent('click'));
  await tick();
  const selB = byAria(host, 'dn-bld-select', 'Compare draft B');
  assert(selB, 'the compare operand selects render');
  selB.value = 'tuned';
  selB.dispatchEvent(makeEvent('change'));
  await tick();
  const cmpBtn = firstClass(host, 'dn-bld-btn-compare');
  cmpBtn.dispatchEvent(makeEvent('click'));
  await tick();
  const cmpCall = OP_CALLS.find((c) => c.op === 'compare');
  assert(cmpCall, 'the Compare button posts op compare');
  assertEqual(cmpCall.args.name_a, 'session', 'operand A defaults to the working draft');
  assertEqual(cmpCall.args.name_b, 'tuned', 'operand B follows the select');
  // the keyed diff renders: the changed scoring key with both values.
  const result = firstClass(host, 'dn-bld-cmp-result');
  assert(result, 'the compare result panel renders');
  assert(result.textContent.includes('promote_margin'), 'the differing scoring key is named');
  assert(result.textContent.includes('0.01') && result.textContent.includes('0.07'), 'both values are shown');
});

// ── the build-time statistical pre-flight (Review pane) ────────────────

test('builder view: the Review pane runs the preflight op and renders the verdict chip + refuse warnings', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Review'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  // the preflight control renders BEFORE apply, with no verdict yet.
  const pfBtn = byClass(host, 'dn-bld-btn-preflight')[0];
  assert(pfBtn, 'the Review pane offers a Run-preflight control');
  assertEqual(byClass(host, 'dn-bld-pf-chip').length, 0, 'no verdict chip before a measurement');
  pfBtn.dispatchEvent(makeEvent('click'));
  await tick();
  const pfCall = OP_CALLS.find((c) => c.op === 'preflight');
  assert(pfCall, 'the preflight op was posted through the same /builder/op dispatch');
  // the REFUSE verdict chip + reasons render from the returned envelope.
  const chip = byClass(host, 'dn-bld-pf-chip')[0];
  assert(chip, 'a verdict chip rendered');
  assert(chip.classList.contains('dn-bld-pf-refuse'), 'the chip carries the refuse class');
  assert(chip.textContent.includes('REFUSE'), 'the chip names the verdict');
  const reasons = firstClass(host, 'dn-bld-pf-reasons');
  assert(reasons && reasons.textContent.includes('noise floor'), 'the reasons name the measured floor');
  assert(reasons.textContent.includes('degradation signal'), 'the reasons name what was measured');
  // the REFUSE-severity validate warning surfaces in the Review pane itself.
  const refuse = byClass(host, 'dn-bld-warn-refuse')[0];
  assert(refuse && refuse.textContent.includes('noise floor'), 'the margin-vs-floor refuse warning renders beside apply');
  // A report with no probed_points must read as the one probe it took, in the
  // singular.
  assert(reasons.textContent.includes('best of 1 probed point:'), 'a report with no probed_points reads as one probe, singular');
  assertEqual(byClass(host, 'dn-bld-pf-chip').length, 1, 'no window chip when the report carries no window failure');
});

// The Review pane must not head a guaranteed-null contract with a green OK.
// The verdict chip answers signal-vs-noise only (issue #112), so the margin
// window gets its own chip, exactly as `zicato board preflight` prints its own
// `window:` line.
async function _renderPreflight(host, result) {
  installBuilderFetch();
  PREFLIGHT_RESULT = result;
  globalThis.window.localStorage.clear();
  await view.render(host);
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Review'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  byClass(host, 'dn-bld-btn-preflight')[0].dispatchEvent(makeEvent('click'));
  await tick();
}

test('builder view: a preflight whose margin exceeds the measured degradation signal chips the WINDOW failure', async () => {
  const host = globalThis.document.createElement('div');
  await _renderPreflight(host, {
    available: true, verdict: 'ok', reason: '',
    report: {
      verdict: 'ok', signal: 0.041, noise_floor_max_abs_delta: 0.02, noise_floor_runs: 5,
      degraded_mutation_id: 'style_rules', promote_margin: 0.10,
      window_verdict: 'refuse', window_failure: 'margin_above_achievable',
      probed_points: [
        { mutation_id: 'pal', skipped: 'no_op_patch' },
        { mutation_id: 'docs_tone', signal: 0.0, skipped: '' },
        { mutation_id: 'style_rules', signal: 0.041, skipped: '' },
      ],
    },
    noise_floor: { max_abs_delta: 0.02, runs: 5 },
  });
  const chips = byClass(host, 'dn-bld-pf-chip');
  assertEqual(chips.length, 2, 'the window failure gets its own chip beside the verdict chip');
  assert(chips[0].textContent.includes('OK'), 'the verdict chip still reports signal-vs-noise');
  assert(chips[1].textContent.includes('WINDOW'), 'the second chip names the window');
  assert(chips[1].textContent.includes('MARGIN ABOVE ACHIEVABLE'), 'and which bound failed');
  assert(chips[1].classList.contains('dn-bld-pf-refuse'), 'a window refusal carries the refuse class, so OK is not the headline');
  const reasons = firstClass(host, 'dn-bld-pf-reasons');
  assert(reasons.textContent.includes('improvement headroom is unmeasured'), 'the reasons say what the comparison is worth, not that the run is null');
  // Two of the three points were drawn; the third was dropped for free and must
  // not be counted as evidence.
  assert(reasons.textContent.includes('best of 2 probed points:'), 'skipped points are not counted as probes');
});

test('builder view: an inert verdict chips as unmeasured, not as a broken board', async () => {
  const host = globalThis.document.createElement('div');
  await _renderPreflight(host, {
    available: true, verdict: 'inert', reason: '',
    report: {
      verdict: 'inert', signal: 0.0, noise_floor_max_abs_delta: 0.08, noise_floor_runs: 5,
      degraded_mutation_id: 'docs_tone', promote_margin: 0.10,
      window_verdict: 'warn', window_failure: 'empty_window',
      probed_points: [{ mutation_id: 'docs_tone', signal: 0.0, skipped: '' }],
    },
    noise_floor: { max_abs_delta: 0.08, runs: 5 },
  });
  const chips = byClass(host, 'dn-bld-pf-chip');
  assert(chips[0].textContent.includes('INERT'), 'the chip names the inert verdict');
  assert(chips[0].classList.contains('dn-bld-pf-inert'), 'and carries a styled class rather than falling back to the bare chip');
  assert(chips[1].classList.contains('dn-bld-pf-warn'), 'an empty window warns rather than refuses');
  const reasons = firstClass(host, 'dn-bld-pf-reasons');
  assert(reasons.textContent.includes('UNMEASURED'), 'the reasons say the achievable signal is unmeasured');
  assert(reasons.textContent.includes('no promote_margin is defensible'), 'and that no margin is defensible on this board');
});

test('validateContract twin: margin at/below a measured floor with the evidence gate off → refuse', () => {
  // Gate off, margin 0.01 <= floor 0.05 → the refuse-severity rule fires.
  const warns = builder.validateContract('gauntlet', {}, 6, 0, {}, { promoteMargin: 0.01, noiseFloor: 0.05 });
  const hit = warns.find((w) => w.code === 'margin_below_noise_floor');
  assert(hit, 'the margin_below_noise_floor rule fired');
  assertEqual(hit.severity, 'refuse', 'the rule is refuse-severity');
  // Evidence gate ON (threshold in (0,1)) silences it.
  const gated = builder.validateContract('gauntlet', { promote_confidence_threshold: 0.8 }, 6, 0, {}, { promoteMargin: 0.01, noiseFloor: 0.05 });
  assert(!gated.find((w) => w.code === 'margin_below_noise_floor'), 'the evidence gate silences the rule');
  // Margin clearing the floor silences it.
  const clear = builder.validateContract('gauntlet', {}, 6, 0, {}, { promoteMargin: 0.06, noiseFloor: 0.05 });
  assert(!clear.find((w) => w.code === 'margin_below_noise_floor'), 'a clearing margin is silent');
  // No floor known → silent (never guess).
  const unknown = builder.validateContract('gauntlet', {}, 6, 0, {}, { promoteMargin: 0.01 });
  assert(!unknown.find((w) => w.code === 'margin_below_noise_floor'), 'no measured floor, no rule');
});

// ── estimateCost: the honest-meter twins (evidence gate, best-of-N aux,
//    placebo) — mirror the Python estimator's numbers exactly ───────────

test('estimateCost twin: the evidence-gate crowning-confirm budget is priced (budget × 2 × board)', () => {
  // Gate off: no line.
  const off = builder.estimateCost('gauntlet', { field_size: 1, replicates: 1 }, 10, 0, { best_of_n: 1 });
  assert(!off.breakdown.find((l) => l.label.includes('crowning-confirm')), 'no confirm line with the gate off');
  // The scaffold gate: 32 × 2 × 10 = 640, added to the headline, largest term.
  const on = builder.estimateCost('gauntlet', {
    field_size: 1, replicates: 1,
    promote_confidence_threshold: 0.8, promote_confidence_replicates: 32,
  }, 10, 0, { best_of_n: 1 });
  const confirm = on.breakdown.find((l) => l.label.includes('crowning-confirm'));
  assert(confirm, 'the confirm line renders');
  assertEqual(confirm.runs, 640, '32 × 2 × 10 = 640 (the py estimator value)');
  assert(/crowning/.test(confirm.detail), 'the detail says it applies per confirmed crowning');
  assertEqual(on.board_runs_per_round, off.board_runs_per_round + 640, 'the headline includes it');
  // Unset budget defaults to 3 (the gate module default).
  const def = builder.estimateCost('gauntlet', { field_size: 1, replicates: 1, promote_confidence_threshold: 0.8 }, 10, 0, { best_of_n: 1 });
  assertEqual(def.breakdown.find((l) => l.label.includes('crowning-confirm')).runs, 60, 'unset budget → 3 × 2 × 10');
});

test('estimateCost twin: best-of-N propose calls are listed as auxiliary and EXCLUDED from the headline', () => {
  const est = builder.estimateCost('gauntlet', { field_size: 1, replicates: 1 }, 10, 0, { best_of_n: 3 });
  const aux = est.breakdown.find((l) => l.label === 'best-of-N propose calls');
  assert(aux, 'the auxiliary line renders when best_of_n > 1');
  assertEqual(aux.runs, 3, 'proposes 1 × best_of_n 3');
  assert(/auxiliary/.test(aux.detail), 'labelled as auxiliary LLM calls');
  assertEqual(est.board_runs_per_round, 10, 'the board-runs headline excludes the calls');
  // A wide field proposes field_size challengers.
  const wide = builder.estimateCost('racing', { field_size: 4 }, 10, 0, { best_of_n: 3 });
  assertEqual(wide.breakdown.find((l) => l.label === 'best-of-N propose calls').runs, 12, '4 × 3');
});

test('estimateCost twin: the placebo cadence adds an amortized per-round line', () => {
  const off = builder.estimateCost('gauntlet', { field_size: 1, replicates: 2 }, 10, 0, { best_of_n: 1 }, {});
  assert(!off.breakdown.find((l) => l.label.includes('placebo')), 'no placebo line at cadence 0');
  const on = builder.estimateCost('gauntlet', { field_size: 1, replicates: 2 }, 10, 0, { best_of_n: 1 },
    { random_baseline_every_n: 4 });
  const placebo = on.breakdown.find((l) => l.label.includes('placebo'));
  assert(placebo, 'the placebo line renders');
  assertEqual(placebo.runs, 5, 'ceil(2 × 10 / 4) = 5 (the py estimator value)');
  assertEqual(on.board_runs_per_round, off.board_runs_per_round + 5, 'the headline includes it');
});

test('estimateCost: the per-structure default-replicates twin matches the Python map for every structure', () => {
  // The JS default-replicates map is the twin of the Python
  // STRUCTURE_DEFAULT_REPLICATES (derived from each strategy's
  // _default_replicates). Pin every structure so the two can never drift.
  // Base default 2 (noise-aware); racing pins 1 (intrinsic replication).
  const expected = { gauntlet: 2, single_elim: 2, double_elim: 2, swiss: 2, racing: 1 };
  for (const [structure, def] of Object.entries(expected)) {
    assertEqual(builder.defaultReplicatesFor(structure), def, `${structure} default replicates`);
    // The UNSET-default estimate equals the explicit-default estimate.
    const unset = builder.estimateCost(structure, { field_size: 4 }, 8, 0);
    const explicit = builder.estimateCost(structure, { field_size: 4, replicates: def }, 8, 0);
    assertEqual(unset.board_runs_per_round, explicit.board_runs_per_round, `${structure}: unset == explicit-default`);
  }
});

// ── B3: the remaining knob GUI (weights scalars + mapping editors, gate
//    namespace-monotonicity, overfitting ceiling, proposer picker, the
//    revert/undo lifecycle, the rung0 param spec) ───────────────────────

// One EXPLICIT assertion per knob rather than a table loop: the knob-registry
// pin (tests/test_knob_registry.py) scans this file for a line naming the
// quoted op AND the arg, so a table-driven `c.args[key]` reads as no coverage
// at all. Explicit lines keep the pin honest without weakening its scan.
test('builder view: Weights adds the default-judge / plan-revision / runtime scalar rows (set_weights)', async () => {
  const host = await mountAt('Weights');
  const setNum = async (aria, val) => {
    const input = byAria(host, 'dn-bld-num', aria);
    assert(input, `the ${aria} control renders`);
    input.value = String(val);
    input.dispatchEvent(makeEvent('change'));
    await tick();
  };
  await setNum('Pass weight', 3);
  assert(OP_CALLS.find((c) => c.op === 'set_weights' && c.args.pass_weight === 3),
    'the pass-weight row posts set_weights {pass_weight}');
  await setNum('Default judge weight', 2);
  assert(OP_CALLS.find((c) => c.op === 'set_weights' && c.args.default_judge_weight === 2),
    'the default-judge row posts set_weights {default_judge_weight}');
  await setNum('Plan revision weight', 0.25);
  assert(OP_CALLS.find((c) => c.op === 'set_weights' && c.args.plan_revision_weight === 0.25),
    'the plan-revision row posts set_weights {plan_revision_weight}');
  await setNum('Task failure weight', 8);
  assert(OP_CALLS.find((c) => c.op === 'set_weights' && c.args.task_failure_weight === 8),
    'the task-failure row posts set_weights {task_failure_weight}');
  await setNum('Not completed weight', 25);
  assert(OP_CALLS.find((c) => c.op === 'set_weights' && c.args.not_completed_weight === 25),
    'the not-completed row posts set_weights {not_completed_weight}');
});

test('builder view: Weights severity_weights editor renders FIXED vocab rows + posts the WHOLE mapping', async () => {
  const host = await mountAt('Weights');
  // one fixed row per vocab severity (info/warning/critical).
  for (const sev of ['info', 'warning', 'critical']) {
    assert(byAria(host, 'dn-bld-num', 'Severity weight ' + sev), 'severity row ' + sev + ' renders from vocab');
  }
  const warning = byAria(host, 'dn-bld-num', 'Severity weight warning');
  warning.value = '5';
  warning.dispatchEvent(makeEvent('change'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'set_weights' && c.args.severity_weights);
  assert(call, 'a severity edit posts set_weights {severity_weights}');
  assertEqual(call.args.severity_weights.warning, 5, 'the edited key carries the new value');
  assertEqual(call.args.severity_weights.info, 1, 'the untouched info rides along (WHOLE mapping)');
  assertEqual(call.args.severity_weights.critical, 10, 'the untouched critical rides along (WHOLE mapping)');
});

test('builder view: Weights per_kind_weights editor renders FIXED vocab rows + posts set_weights', async () => {
  const host = await mountAt('Weights');
  for (const kind of ['single_turn', 'synthetic_clean']) {
    assert(byAria(host, 'dn-bld-num', 'Per-kind weight ' + kind), 'per-kind row ' + kind + ' renders from vocab');
  }
  const st = byAria(host, 'dn-bld-num', 'Per-kind weight single_turn');
  st.value = '1.5';
  st.dispatchEvent(makeEvent('change'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'set_weights' && c.args.per_kind_weights);
  assert(call, 'a per-kind edit posts set_weights {per_kind_weights}');
  assertEqual(call.args.per_kind_weights.single_turn, 1.5, 'the edited kind carries the new value');
});

test('builder view: Weights per_judge_weights is SEEDED from board judges + an add-key row creates a NEW key', async () => {
  const host = await mountAt('Weights');
  // the board declares a "tone" judge → the per-judge editor seeds a row for it.
  const tone = byAria(host, 'dn-bld-num', 'Per-judge weight tone');
  assert(tone, 'a per-judge row is seeded from the board judge "tone"');
  tone.value = '0';
  tone.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_weights' && c.args.per_judge_weights && c.args.per_judge_weights.tone === 0),
    'editing the seeded judge posts set_weights {per_judge_weights}');
  // the add-key row introduces a judge the mapping never carried.
  const k = byAria(host, 'dn-bld-text', 'New per-judge weight name');
  const v = byAria(host, 'dn-bld-num', 'New per-judge weight value');
  const add = byAria(host, 'dn-bld-btn', 'Add per-judge weight');
  assert(k && v && add, 'the per-judge add-key row renders');
  k.value = 'terseness';
  v.value = '2';
  add.dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'set_weights' && c.args.per_judge_weights && 'terseness' in c.args.per_judge_weights);
  assert(call, 'the add-key row posts a NEW per-judge key');
  assertEqual(call.args.per_judge_weights.terseness, 2, 'the new key carries the typed value');
});

test('builder view: Weights namespace ADD-KEY row creates a NEW namespace key (iterate-existing gap closed)', async () => {
  const host = await mountAt('Weights');
  const k = byAria(host, 'dn-bld-text', 'New namespace weight key');
  const v = byAria(host, 'dn-bld-num', 'New namespace weight value');
  const add = byAria(host, 'dn-bld-btn', 'Add namespace weight');
  assert(k && v && add, 'the namespace add-key row renders');
  // blank key is inert (never posts).
  add.dispatchEvent(makeEvent('click'));
  await tick();
  assert(!OP_CALLS.find((c) => c.op === 'set_namespace_weights' && c.args.namespace_weights && 'cost:' in c.args.namespace_weights),
    'a blank add-key never posts');
  k.value = 'cost:';
  v.value = '0.4';
  add.dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'set_namespace_weights' && c.args.namespace_weights && 'cost:' in c.args.namespace_weights);
  assert(call, 'the add-key row posts a NEW namespace key through set_namespace_weights');
  assertEqual(call.args.namespace_weights['cost:'], 0.4, 'the new key carries the typed value');
  assertEqual(call.args.namespace_weights['drift:'], 1, 'the existing keys ride along (WHOLE mapping)');
});

test('builder view: the Gate namespace_monotonicity editor posts the WHOLE mapping + an add-key row adds a key', async () => {
  const host = await mountAt('Gate');
  // the existing rubric: row toggles.
  const rubric = byAria(host, 'dn-bld-check', 'Namespace monotonicity rubric:');
  assert(rubric, 'the seeded namespace-monotonicity row renders');
  rubric.checked = false;
  rubric.dispatchEvent(makeEvent('change'));
  await tick();
  let call = OP_CALLS.find((c) => c.op === 'set_gate' && c.args.namespace_monotonicity);
  assert(call, 'toggling a namespace posts set_gate {namespace_monotonicity}');
  assertEqual(call.args.namespace_monotonicity['rubric:'], false, 'the edited key flips');
  // the add-key row adds a new namespace with the checkbox value.
  const k = byAria(host, 'dn-bld-text', 'New namespace monotonicity key');
  const add = byAria(host, 'dn-bld-btn', 'Add namespace monotonicity key');
  assert(k && add, 'the namespace-monotonicity add-key row renders');
  k.value = 'schema:';
  add.dispatchEvent(makeEvent('click'));
  await tick();
  call = OP_CALLS.find((c) => c.op === 'set_gate' && c.args.namespace_monotonicity && 'schema:' in c.args.namespace_monotonicity);
  assert(call, 'the add-key row posts a new namespace-monotonicity key');
  assertEqual(call.args.namespace_monotonicity['schema:'], true, 'the new key defaults to strict (may not regress)');
});

test('builder view: the Overfitting section drives max_generations_per_contract (set_holdout)', async () => {
  const host = await mountAt('Overfitting');
  const ceiling = byAria(host, 'dn-bld-num', 'Max generations per contract');
  assert(ceiling, 'the board-refresh ceiling control renders');
  ceiling.value = '12';
  ceiling.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.max_generations_per_contract === 12),
    'a positive ceiling posts set_holdout {max_generations_per_contract}');
  // 0 CLEARS the ceiling (the op reserves None for "unchanged", so the form
  // always sends an explicit integer — 0 is a real clear, never a no-send).
  ceiling.value = '0';
  ceiling.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_holdout' && c.args.max_generations_per_contract === 0),
    '0 posts an explicit set_holdout {max_generations_per_contract: 0} (clears the ceiling)');
});

test('builder view: the Proposer picker lists discovered dirs + builtin default + a free-text path (set_proposer)', async () => {
  const host = await mountAt('Proposer');
  const pick = byAria(host, 'dn-bld-select', 'Proposer dir');
  assert(pick, 'the proposer picker renders');
  // the builtin default + the discovered dir are both options.
  assert(pick.children.some((o) => o.getAttribute('value') === ''), 'the builtin default is an option');
  assert(pick.children.some((o) => o.getAttribute('value') === '/ws/proposers/critic-v2'), 'the discovered dir is an option');
  pick.value = '/ws/proposers/critic-v2';
  pick.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_proposer' && c.args.proposer_path === '/ws/proposers/critic-v2'),
    'picking a dir posts set_proposer with the path');
  // selecting the builtin default posts null.
  pick.value = '';
  pick.dispatchEvent(makeEvent('change'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_proposer' && c.args.proposer_path === null),
    'the builtin default posts set_proposer {proposer_path: null}');
  // the free-text path row sets an arbitrary path.
  const pathIn = byAria(host, 'dn-bld-text', 'Proposer path');
  const setBtn = byAria(host, 'dn-bld-btn', 'Set proposer path');
  assert(pathIn && setBtn, 'the free-text proposer-path row renders');
  pathIn.value = '/custom/prop';
  setBtn.dispatchEvent(makeEvent('click'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'set_proposer' && c.args.proposer_path === '/custom/prop'),
    'the free-text row posts set_proposer with the typed path');
});

test('builder view: declaring a file type posts the WHOLE mutation_surface table', async () => {
  const host = await mountAt('Proposer');
  const suffixIn = byAria(host, 'dn-bld-text', 'File suffix');
  const leadersIn = byAria(host, 'dn-bld-text', 'Comment leaders');
  const trailersIn = byAria(host, 'dn-bld-text', 'Comment trailers');
  const addBtn = byAria(host, 'dn-bld-btn', 'Declare file type');
  assert(suffixIn && leadersIn && trailersIn && addBtn, 'the declare-a-file-type row renders');
  // a suffix with no leader is not a declarable surface — nothing is posted.
  suffixIn.value = '.sql';
  addBtn.dispatchEvent(makeEvent('click'));
  await tick();
  assert(!OP_CALLS.find((c) => c.op === 'set_mutation_surface'), 'a leaderless suffix posts nothing');
  leadersIn.value = '//, /*';
  trailersIn.value = '*/';
  suffixIn.value = '.ts';
  addBtn.dispatchEvent(makeEvent('click'));
  await tick();
  const call = OP_CALLS.find((c) => c.op === 'set_mutation_surface' && c.args.mutation_surface);
  assert(call, 'set_mutation_surface was posted with the mutation_surface table');
  assertEqual(JSON.stringify(call.args.mutation_surface['.ts']),
    JSON.stringify({ leaders: ['//', '/*'], trailers: ['*/'] }),
    'the declared entry carries the split leaders and trailers');
});

test('builder view: the Proposer lede no longer claims a read-only summary', async () => {
  const host = await mountAt('Proposer');
  const lede = firstClass(host, 'dn-lede');
  assert(lede, 'the proposer section has a lede');
  assert(!/read-only summary/i.test(lede.textContent), 'the misleading "Read-only summary here" lede is rewritten');
});

test('builder view: the slot strip Reset-to-live is TWO-click and posts revert_to_live', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const reset = byAria(host, 'dn-bld-btn', 'Reset to live');
  assert(reset, 'the reset-to-live button renders in the slot strip');
  // first click ARMS (no op posted yet).
  reset.dispatchEvent(makeEvent('click'));
  await tick();
  assert(!OP_CALLS.find((c) => c.op === 'revert_to_live'), 'the first click only arms — never one-click');
  // the armed button now shows the confirm affordance.
  const armed = byAria(host, 'dn-bld-btn', 'Reset to live');
  assert(armed.classList.contains('dn-bld-btn-confirm'), 'the armed reset shows the confirm class');
  armed.dispatchEvent(makeEvent('click'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'revert_to_live'), 'the confirming click posts revert_to_live');
});

test('builder view: the slot strip Undo posts undo; an empty history renders the "nothing to undo" note', async () => {
  installBuilderFetch();
  globalThis.window.localStorage.clear();
  const host = globalThis.document.createElement('div');
  await view.render(host);
  const undo = byAria(host, 'dn-bld-btn', 'Undo');
  assert(undo, 'the undo button renders in the slot strip');
  undo.dispatchEvent(makeEvent('click'));
  await tick();
  assert(OP_CALLS.find((c) => c.op === 'undo'), 'Undo posts the undo lifecycle op');
  // the mock has no history → the "nothing to undo" note renders.
  const note = firstClass(host, 'dn-bld-slots-undonote');
  assert(note && /nothing to undo/i.test(note.textContent), 'the nothing-to-undo note renders when the history is empty');
  // with history, a second undo clears the note (a restore happened).
  UNDO_HAS_HISTORY = true;
  byAria(host, 'dn-bld-btn', 'Undo').dispatchEvent(makeEvent('click'));
  await tick();
  assert(!firstClass(host, 'dn-bld-slots-undonote'), 'a successful undo clears the note');
});

test('builder view: a no-op re-render of the Weights section rebuilds ZERO DOM (digest-gated identity)', async () => {
  const host = await mountAt('Weights');
  const center = firstClass(host, 'dn-bld-center');
  assert(center, 'the center pane is present');
  const secBefore = firstClass(center, 'dn-section');
  const writes = center.innerHTMLWriteCount();
  // re-click the ALREADY-active Weights rail item → a full render pass with an
  // IDENTICAL center digest must not rebuild the section (the render-discipline
  // no-op guardrail).
  const rail = byClass(host, 'dn-bld-railitem').find((r) => r.textContent.includes('Weights'));
  rail.dispatchEvent(makeEvent('click'));
  await tick();
  const secAfter = firstClass(center, 'dn-section');
  assert(secBefore === secAfter, 'a no-op section re-render preserves node identity (zero rebuild)');
  assertEqual(center.innerHTMLWriteCount(), writes, 'a no-op section re-render writes ZERO additional DOM');
});

// ── model.js: the rung0_board_size racing param SPEC (L3 — spec only) ────

test('paramSpecsFor: racing carries the rung0_board_size override spec (both estimators already read it)', () => {
  const racing = builder.paramSpecsFor('racing');
  const rung0 = racing.find((s) => s.key === 'rung0_board_size');
  assert(rung0, 'the racing block declares a rung0_board_size spec');
  assert(rung0.removeAtZero === true, '0 removes the key (unset hashes identically) — like the evidence-gate threshold');
  assert(rung0.int === true, 'the rung-0 override is an integer entry count');
  // a non-racing structure never carries it.
  assert(!builder.paramSpecsFor('gauntlet').find((s) => s.key === 'rung0_board_size'),
    'gauntlet does not carry the racing-only rung0 spec');
});

test('paramSpecsFor: racing carries an explicit, contract-visible slice schedule', () => {
  const racing = builder.paramSpecsFor('racing');
  const schedule = racing.find((s) => s.key === 'slice_schedule');
  assert(schedule, 'the racing block declares the slice schedule');
  assertEqual(schedule.def, 'prefix', 'contracts remain prefix-scheduled when the key is absent');
  assertEqual(JSON.stringify(schedule.options.map((o) => o.value)), JSON.stringify(['prefix', 'shuffled_v1']),
    'the builder offers both the authored-order and the opt-in shuffled schedule');
  // Picking the default must REMOVE the key rather than stamp it: an explicit
  // "prefix" rolls the epoch while changing nothing about how a rung slices.
  assert(schedule.removeAtDefault === true,
    'selecting the default removes the key so an untouched contract hashes identically');
});

test('paramSpecsFor: the rung0 override does not perturb the cost twin (spec-only, arithmetic unchanged)', () => {
  // both estimators already read rung0_board_size, so an explicit value moves
  // the meter identically on both sides — the spec only surfaces the control.
  const base = builder.estimateCost('racing', { field_size: 4, eta: 2, board_fraction: 0.25 }, 12, 0);
  const withOverride = builder.estimateCost('racing', { field_size: 4, eta: 2, rung0_board_size: 6 }, 12, 0);
  assert(base.board_runs_per_round !== withOverride.board_runs_per_round,
    'the rung0 override moves the meter (the key was already read) — a live, not cosmetic, param');
});

await run();
